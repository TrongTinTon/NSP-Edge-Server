# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import AccessError, ValidationError
from odoo.addons.nsp_core.utils import new_management_code


class NspParkingArea(models.Model):
    """Edge runtime copy of one published Cloud Parking Layout."""

    _name = "nsp.parking.area"
    _description = "NSP Parking Operation Configuration"
    _rec_name = "name"
    _order = "branch_id, name, id"

    name = fields.Char(string="Parking Area Name", required=True)
    code = fields.Char(
        string="Parking Area Code", required=True, readonly=True, copy=False,
        index=True, default=lambda self: new_management_code("PARK"),
    )
    branch_id = fields.Many2one(
        "nsp.branch", string="Branch", required=True, ondelete="restrict", index=True,
    )
    state = fields.Selection(
        [
            ("draft", "Draft / Configuring"),
            ("operational", "Operational"),
            ("maintenance", "Maintenance"),
            ("blocked", "Blocked"),
        ],
        string="State", default="draft", required=True, index=True,
    )
    published_revision = fields.Integer(default=0, readonly=True, copy=False, index=True)
    runtime_synced_at = fields.Datetime(
        string="Synchronized At", readonly=True, copy=False, index=True,
    )
    is_published = fields.Boolean(compute="_compute_is_published")
    lane_ids = fields.One2many("nsp.parking.lane", "parking_area_id", string="Parking Lanes")
    edge_server_ids = fields.Many2many(
        "nsp.edge.server", string="Servers", compute="_compute_topology",
    )
    controller_ids = fields.Many2many(
        "nsp.controller", string="Controllers", compute="_compute_topology",
        search="_search_controllers",
    )
    reader_ids = fields.Many2many("nsp.device", string="Readers", compute="_compute_topology")
    edge_server_count = fields.Integer(compute="_compute_counts")
    controller_count = fields.Integer(compute="_compute_counts")
    reader_count = fields.Integer(compute="_compute_counts")
    lane_count = fields.Integer(compute="_compute_counts")
    whitelist_count = fields.Integer(compute="_compute_whitelist_count")
    ready_lane_count = fields.Integer(
        string="Ready Lanes", compute="_compute_configuration_health",
    )
    incomplete_lane_count = fields.Integer(
        string="Incomplete Lanes", compute="_compute_configuration_health",
    )
    configuration_state = fields.Selection(
        [
            ("empty", "No Lanes"),
            ("incomplete", "Needs Attention"),
            ("ready", "Ready"),
        ],
        string="Configuration", compute="_compute_configuration_health",
    )
    configuration_summary = fields.Char(
        string="Configuration Summary", compute="_compute_configuration_health",
    )
    published_edge_server_codes = fields.Char(
        string="Published Edge Servers", compute="_compute_published_edge_server_codes",
    )

    _sql_constraints = [
        ("code_unique", "unique(code)", "Parking Area Code must be unique."),
    ]

    @api.depends("published_revision")
    def _compute_is_published(self):
        for record in self:
            record.is_published = bool(record.published_revision)

    @api.depends(
        "lane_ids.active",
        "lane_ids.edge_server_id",
        "lane_ids.controller_id",
        "lane_ids.antenna_sequence_ids.reader_id",
        "lane_ids.antenna_sequence_ids.port_no",
    )
    def _compute_topology(self):
        for record in self:
            lanes = record.lane_ids.filtered("active")
            record.edge_server_ids = lanes.mapped("edge_server_id")
            record.controller_ids = lanes.mapped("controller_id")
            record.reader_ids = lanes.mapped("antenna_sequence_ids.reader_id")

    @api.model
    def _search_controllers(self, operator, value):
        return [("lane_ids.controller_id", operator, value)]

    @api.depends("edge_server_ids", "controller_ids", "reader_ids", "lane_ids.active")
    def _compute_counts(self):
        for record in self:
            record.edge_server_count = len(record.edge_server_ids)
            record.controller_count = len(record.controller_ids)
            record.reader_count = len(record.reader_ids)
            record.lane_count = len(record.lane_ids.filtered("active"))

    @api.depends(
        "lane_ids.active",
        "lane_ids.controller_id.edge_server_id.edge_server_code",
    )
    def _compute_published_edge_server_codes(self):
        for record in self:
            record.published_edge_server_codes = ", ".join(sorted(
                code for code in record.edge_server_ids.mapped("edge_server_code") if code
            ))

    @api.depends(
        "lane_ids.active",
        "lane_ids.configuration_state",
        "lane_ids.configuration_issue",
        "lane_ids.antenna_sequence_ids",
    )
    def _compute_configuration_health(self):
        for record in self:
            active_lanes = record.lane_ids.filtered("active")
            ready_lanes = active_lanes.filtered(
                lambda lane: lane.configuration_state == "ready"
            )
            incomplete_lanes = active_lanes - ready_lanes
            record.ready_lane_count = len(ready_lanes)
            record.incomplete_lane_count = len(incomplete_lanes)
            if not active_lanes:
                record.configuration_state = "empty"
                record.configuration_summary = _("No active Lane is synchronized.")
            elif incomplete_lanes:
                record.configuration_state = "incomplete"
                record.configuration_summary = _(
                    "%(ready)s ready · %(incomplete)s need attention"
                ) % {
                    "ready": len(ready_lanes),
                    "incomplete": len(incomplete_lanes),
                }
            else:
                record.configuration_state = "ready"
                record.configuration_summary = _(
                    "All %(count)s active Lanes match the synchronized runtime snapshot."
                ) % {"count": len(active_lanes)}

    def _compute_whitelist_count(self):
        count = self.env["nsp.device.whitelist"].sudo().search_count([])
        for record in self:
            record.whitelist_count = count

    @api.model
    def _normalize_code(self, value):
        return str(value or "").strip().upper()

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for source in vals_list:
            values = dict(source)
            values["code"] = self._normalize_code(
                values.get("code") or new_management_code("PARK")
            )
            prepared.append(values)
        return super().create(prepared)

    def write(self, vals):
        values = dict(vals)
        if "code" in values:
            values["code"] = self._normalize_code(values.get("code"))
        return super().write(values)

    def action_open_live_monitor(self):
        self.ensure_one()
        self.check_access("read")
        return {
            "type": "ir.actions.client",
            "name": _("Parking Live Monitor"),
            "tag": "nsp_parking_live_monitor",
            "target": "fullscreen",
            "params": {"parking_area_id": self.id},
        }

    @api.model
    def get_live_monitor_snapshot(self, parking_area_id, limit=16):
        if not (
            self.env.user.has_group("nsp_core.group_nsp_operator")
            or self.env.user.has_group("nsp_core.group_nsp_it_parking")
            or self.env.user.has_group("base.group_system")
        ):
            raise AccessError(_("You do not have access to the Parking Live Monitor."))
        try:
            parking_area_id = int(parking_area_id or 0)
            limit = min(max(int(limit or 12), 3), 50)
        except (TypeError, ValueError):
            parking_area_id, limit = 0, 12
        area = self.browse(parking_area_id).exists()
        if not area:
            return {"found": False}
        area.check_access("read")
        transactions = self.env["nsp.parking.transaction"].search(
            [
                "|",
                ("parking_area_id", "=", area.id),
                ("lane_id.parking_area_id", "=", area.id),
            ],
            order="event_time desc, id desc", limit=limit,
        )
        return {
            "found": True,
            "parking_area_id": area.id,
            "parking_area_name": area.name,
            "branch_name": area.branch_id.name or "",
            "state": area.state,
            "items": [transaction._live_monitor_payload() for transaction in transactions[::-1]],
        }

    def _lane_payload(self):
        self.ensure_one()
        return [lane._runtime_payload() for lane in self.lane_ids.filtered("active").sorted(
            key=lambda item: ((item.name or "").casefold(), item.code or "", item.id)
        )]

    def _operational_issues(self):
        self.ensure_one()
        issues = []
        lanes = self.lane_ids.filtered("active")
        if not lanes:
            return [_('Configure at least one active Parking Lane.')]
        for lane in lanes:
            try:
                lane._validate_runtime_configuration()
            except ValidationError as exc:
                issues.append(str(exc))
        return issues

    def prepare_sync_payload(self):
        self.ensure_one()
        return {
            "parking_area_code": self.code,
            "parking_area_name": self.name,
            "branch_code": self.branch_id.code or "",
            "state": self.state,
            "published_revision": int(self.published_revision or 0),
            "lanes": self._lane_payload(),
        }

    def _open_related_action(self, action_xmlid, records, name, context=None):
        self.ensure_one()
        self.check_access("read")
        action = self.env.ref(action_xmlid).read()[0]
        action.update({
            "name": name,
            "domain": [("id", "in", records.ids)] if records else [],
            "context": dict(context or {}),
        })
        return action

    def action_open_controllers(self):
        self.ensure_one()
        return self._open_related_action(
            "nsp_business_gatekeeper.action_nsp_controllers",
            self.controller_ids,
            _("Controllers"),
        )

    def action_open_readers(self):
        self.ensure_one()
        context = {"default_controller_id": self.controller_ids.id} if len(self.controller_ids) == 1 else {}
        return self._open_related_action(
            "nsp_business_gatekeeper.nsp_device_action", self.reader_ids, _("Readers"), context
        )

    def action_open_lanes(self):
        self.ensure_one()
        self.check_access("read")
        action = self.env.ref("nsp_business_gatekeeper.action_nsp_parking_lane").read()[0]
        action.update({
            "name": _("Parking Lanes"),
            "domain": [("parking_area_id", "=", self.id)],
            "context": {"default_parking_area_id": self.id},
        })
        return action


class NspParkingLane(models.Model):
    """Read-only Edge projection of one physical Lane published by Cloud.

    Runtime crossing detection uses exactly one Antenna Sequence. Check-in or
    Check-out is resolved from the Vehicle parking state after that sequence
    matches; direction is never encoded by reversing or duplicating the path.
    """

    _name = "nsp.parking.lane"
    _description = "NSP Parking Lane"
    _order = "parking_area_id, name, id"
    _rec_name = "display_name"

    name = fields.Char(string="Lane Name", required=True, default="Lane")
    code = fields.Char(
        string="Lane Code", required=True, readonly=True, copy=False, index=True,
        default=lambda self: new_management_code("LANE"),
    )
    display_name = fields.Char(compute="_compute_display_name", store=True)
    parking_area_id = fields.Many2one(
        "nsp.parking.area", string="Parking Area", required=True,
        ondelete="cascade", index=True,
    )
    controller_id = fields.Many2one(
        "nsp.controller", string="Controller", required=True,
        ondelete="restrict", index=True,
    )
    edge_server_id = fields.Many2one(
        "nsp.edge.server", string="Server",
        related="controller_id.edge_server_id", readonly=True,
    )
    edge_server_status = fields.Selection(
        related="edge_server_id.status", string="Server Status", readonly=True,
    )
    controller_status = fields.Selection(
        related="controller_id.status", string="Controller Status", readonly=True,
    )
    parking_area_state = fields.Selection(
        related="parking_area_id.state", string="Layout State", readonly=True,
    )
    active = fields.Boolean(default=True, index=True)
    tolerance_type = fields.Selection(
        [("percent", "Percentage (%)"), ("seconds", "Seconds")],
        string="Tolerance Type", default="percent", required=True,
    )
    tolerance_value = fields.Float(string="Tolerance Value", default=30.0, required=True)

    # Current NSP contract: one ordered Antenna Sequence per Lane.  The legacy
    # timeline_line_ids alias points to the same rows for rolling upgrades and
    # old internal callers; it is no longer a separate business concept.
    antenna_sequence_ids = fields.One2many(
        "nsp.parking.lane.timeline", "lane_id", string="Antenna Sequence",
    )
    timeline_line_ids = fields.One2many(
        "nsp.parking.lane.timeline", "lane_id", string="Deprecated Timeline",
    )
    reader_config_ids = fields.One2many(
        "nsp.parking.lane.reader.config", "lane_id", string="Device Configuration",
    )
    # Legacy rows are kept only so an existing Edge database can upgrade
    # without dropping a table. New snapshots clear them and runtime ignores them.
    event_sequence_ids = fields.One2many(
        "nsp.parking.lane.event.sequence", "lane_id", string="Deprecated Event Sequences",
    )
    checkin_sequence_ids = fields.One2many(
        "nsp.parking.lane.event.sequence", "lane_id",
        domain=[("sequence_type", "=", "check_in")], string="Deprecated Check-in Sequence",
    )
    checkout_sequence_ids = fields.One2many(
        "nsp.parking.lane.event.sequence", "lane_id",
        domain=[("sequence_type", "=", "check_out")], string="Deprecated Check-out Sequence",
    )

    device_tree_anchor = fields.Boolean(
        string="NSP Device Tree", compute="_compute_presentation_anchors",
    )
    antenna_sequence_preview_anchor = fields.Boolean(
        string="Antenna Sequence Preview", compute="_compute_presentation_anchors",
    )
    reader_config_count = fields.Integer(
        string="Configured Readers", compute="_compute_reader_config_count",
    )
    sequence_point_count = fields.Integer(
        string="Sequence Points", compute="_compute_sequence_point_count",
    )
    timeline_point_count = fields.Integer(
        string="Timeline Points", compute="_compute_sequence_point_count",
    )
    total_path_duration = fields.Float(
        string="Max Window (s)", compute="_compute_total_path_duration", digits=(8, 3),
    )
    configuration_state = fields.Selection(
        [("disabled", "Disabled"), ("incomplete", "Needs Attention"), ("ready", "Ready")],
        string="Configuration", compute="_compute_configuration_health",
    )
    configuration_issue = fields.Char(
        string="Configuration Check", compute="_compute_configuration_health",
    )

    _sql_constraints = [
        ("parking_lane_code_unique", "unique(code)", "Parking Lane Code must be unique."),
        ("lane_tolerance_nonnegative", "CHECK(tolerance_value >= 0)", "Timing Tolerance cannot be negative."),
    ]

    @api.depends("parking_area_id.name", "name")
    def _compute_display_name(self):
        for record in self:
            record.display_name = "%s / %s" % (
                record.parking_area_id.name or _("Parking"),
                record.name or _("Lane"),
            )

    def _compute_presentation_anchors(self):
        for record in self:
            record.device_tree_anchor = True
            record.antenna_sequence_preview_anchor = True

    @api.depends("antenna_sequence_ids.duration_from_previous")
    def _compute_total_path_duration(self):
        for record in self:
            record.total_path_duration = sum(
                record.antenna_sequence_ids.mapped("duration_from_previous")
            )

    @api.depends("antenna_sequence_ids")
    def _compute_sequence_point_count(self):
        for record in self:
            count = len(record.antenna_sequence_ids)
            record.sequence_point_count = count
            record.timeline_point_count = count

    @api.depends("reader_config_ids")
    def _compute_reader_config_count(self):
        for record in self:
            record.reader_config_count = len(record.reader_config_ids)

    @api.depends(
        "active", "controller_id", "controller_id.edge_server_id",
        "antenna_sequence_ids", "antenna_sequence_ids.sequence",
        "antenna_sequence_ids.reader_id", "antenna_sequence_ids.port_no",
        "antenna_sequence_ids.duration_from_previous",
        "reader_config_ids", "reader_config_ids.reader_id",
        "reader_config_ids.power_dbm", "reader_config_ids.read_interval_ms",
        "reader_config_ids.tid_start_address", "reader_config_ids.tid_length",
    )
    def _compute_configuration_health(self):
        for lane in self:
            if not lane.active:
                lane.configuration_state = "disabled"
                lane.configuration_issue = _("Lane is disabled.")
                continue
            issues = []
            controller = lane.controller_id
            if not controller:
                issues.append(_("Controller is missing"))
            elif not controller.edge_server_id:
                issues.append(_("Server is missing"))

            sequence = lane.antenna_sequence_ids.sorted(
                lambda row: (row.sequence or 0, row.id)
            )
            if len(sequence) < 2:
                issues.append(_("Antenna Sequence needs at least 2 points"))
            elif sequence.mapped("sequence") != list(range(1, len(sequence) + 1)):
                issues.append(_("Antenna Sequence order is not contiguous"))
            keys = [(row.reader_id.id, int(row.port_no or 0)) for row in sequence]
            if len(keys) != len(set(keys)):
                issues.append(_("Antenna Sequence contains duplicate Reader Ports"))
            if sequence and float(sequence[0].duration_from_previous or 0.0) != 0.0:
                issues.append(_("First Antenna must use 0 seconds duration"))
            if any(float(row.duration_from_previous or 0.0) <= 0.0 for row in sequence[1:]):
                issues.append(_("Every Antenna after the first requires a positive Max Duration"))
            if controller and any(row.reader_id.controller_id != controller for row in sequence):
                issues.append(_("Sequence Reader does not belong to the Lane Controller"))
            sequence_reader_ids = set(sequence.mapped("reader_id").ids)
            configured_reader_ids = set(lane.reader_config_ids.mapped("reader_id").ids)
            if sequence_reader_ids != configured_reader_ids:
                issues.append(_("Device Configuration does not match Antenna Sequence Readers"))

            lane.configuration_state = "incomplete" if issues else "ready"
            lane.configuration_issue = "; ".join(issues) if issues else _(
                "Antenna Sequence and Device Configuration are synchronized."
            )

    def _validate_reader_configs(self):
        for lane in self:
            sequence_readers = lane.antenna_sequence_ids.mapped("reader_id")
            config_readers = lane.reader_config_ids.mapped("reader_id")
            if set(sequence_readers.ids) != set(config_readers.ids):
                raise ValidationError(
                    _("Lane %(lane)s Device Configuration does not match its Antenna Sequence Readers.")
                    % {"lane": lane.display_name}
                )
            lane.reader_config_ids._validate_parameter_ranges()
        return True

    def _backfill_reader_configs_from_runtime_devices(self):
        Config = self.env["nsp.parking.lane.reader.config"].sudo()
        for lane in self:
            existing_reader_ids = set(lane.reader_config_ids.mapped("reader_id").ids)
            sequence_readers = lane.antenna_sequence_ids.mapped("reader_id")
            for reader in sequence_readers.filtered(lambda item: item.id not in existing_reader_ids):
                Config.create({
                    "lane_id": lane.id, "reader_id": reader.id,
                    "power_dbm": int(reader.power_dbm or 0),
                    "read_interval_ms": int(reader.read_interval_ms or 200),
                    "tid_start_address": int(reader.tid_addr or 0),
                    "tid_length": int(reader.tid_len or 4),
                    "source_type": "published_layout",
                })
            stale = lane.reader_config_ids.filtered(
                lambda config: config.reader_id not in sequence_readers
            )
            if stale:
                stale.unlink()
        return True

    @api.model
    def _normalize_code(self, value):
        return str(value or "").strip().upper()

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for source in vals_list:
            values = dict(source)
            values["code"] = self._normalize_code(values.get("code") or new_management_code("LANE"))
            prepared.append(values)
        return super().create(prepared)

    def write(self, vals):
        values = dict(vals)
        if "code" in values:
            values["code"] = self._normalize_code(values.get("code"))
        return super().write(values)

    def allowed_duration_for_step(self, sequence):
        self.ensure_one()
        line = self.antenna_sequence_ids.filtered(lambda item: item.sequence == sequence)[:1]
        base = float(line.duration_from_previous or 0.0) if line else 0.0
        if self.tolerance_type == "seconds":
            return base + float(self.tolerance_value or 0.0)
        return base * (1.0 + float(self.tolerance_value or 0.0) / 100.0)

    def allowed_duration_for_direction_step(self, sequence_row):
        # Compatibility alias for old callers during rolling upgrade.
        self.ensure_one()
        return self.allowed_duration_for_step(sequence_row.sequence)

    def max_sequence_window(self):
        self.ensure_one()
        ordered = self.antenna_sequence_ids.sorted("sequence")
        if len(ordered) < 2:
            return 1.0
        return max(1.0, sum(
            self.allowed_duration_for_step(row.sequence) for row in ordered[1:]
        ))

    def _validate_runtime_configuration(self):
        for lane in self:
            controller = lane.controller_id
            if not controller or not controller.active or controller.cloud_removed:
                raise ValidationError(
                    _("Lane %(lane)s requires an active Controller.")
                    % {"lane": lane.display_name}
                )
            sequence = lane.antenna_sequence_ids.sorted(
                lambda row: (row.sequence or 0, row.id)
            )
            if len(sequence) < 2:
                raise ValidationError(
                    _("Lane %(lane)s requires at least two Antenna Sequence points.")
                    % {"lane": lane.display_name}
                )
            if sequence.mapped("sequence") != list(range(1, len(sequence) + 1)):
                raise ValidationError(_("Antenna Sequence Order must be contiguous and start at 1."))
            keys = [(row.reader_id.id, int(row.port_no or 0)) for row in sequence]
            if len(keys) != len(set(keys)):
                raise ValidationError(_("A Reader Port can appear only once in an Antenna Sequence."))
            for index, row in enumerate(sequence):
                if row.reader_id.controller_id != controller:
                    raise ValidationError(_("Every Sequence Reader must belong to the Lane Controller."))
                if int(row.port_no or 0) < 1 or int(row.port_no or 0) > 16:
                    raise ValidationError(_("Antenna/Port must be an integer from 1 to 16."))
                duration = float(row.duration_from_previous or 0.0)
                if index == 0 and duration != 0.0:
                    raise ValidationError(_("The first Antenna must use 0 seconds Max Duration."))
                if index > 0 and duration <= 0.0:
                    raise ValidationError(_("Every Antenna after the first requires a positive Max Duration."))
            lane._validate_reader_configs()
        return True

    def _runtime_payload(self):
        self.ensure_one()
        configs = {config.reader_id.id: config for config in self.reader_config_ids}
        readers = {}
        sequence_payload = []
        cumulative = 0.0
        for order, row in enumerate(self.antenna_sequence_ids.sorted("sequence"), start=1):
            config = configs.get(row.reader_id.id)
            if not config:
                continue
            duration = 0.0 if order == 1 else float(row.duration_from_previous or 0.0)
            cumulative += duration
            sequence_payload.append({
                "sequence": order,
                "reader_code": row.reader_id.device_code or "",
                "reader_serial_number": row.reader_id.serial_number or "",
                "port_no": int(row.port_no or 0),
                "duration_from_previous_seconds": duration,
            })
            entry = readers.setdefault(row.reader_id.id, {
                "technical_code": row.reader_id.device_code or "",
                "serial_number": row.reader_id.serial_number or "",
                "reader_name": row.reader_id.name or row.reader_id.serial_number or "",
                "reader_parameters": {
                    "power_dbm": int(config.power_dbm or 0),
                    "read_interval_ms": int(config.read_interval_ms or 200),
                    "tid_start_address": int(config.tid_start_address or 0),
                    "tid_length": int(config.tid_length or 4),
                },
                "ports": set(),
            })
            entry["ports"].add(int(row.port_no or 0))
        return {
            "lane_code": self.code,
            "lane_name": self.name,
            "server_code": self.edge_server_id.edge_server_code or "",
            "controller_code": self.controller_id.controller_id or "",
            "antenna_sequence": sequence_payload,
            "timing_tolerance": {
                "type": self.tolerance_type,
                "value": float(self.tolerance_value or 0.0),
            },
            "readers": [
                {**entry, "ports": [{"port_no": p} for p in sorted(entry["ports"])]}
                for entry in sorted(
                    readers.values(), key=lambda item: (item["serial_number"], item["technical_code"])
                )
            ],
        }


class NspParkingLaneReaderConfig(models.Model):
    _name = "nsp.parking.lane.reader.config"
    _description = "NSP Edge Parking Lane Applied Reader Configuration"
    _order = "lane_id, reader_id, id"
    _rec_name = "reader_id"

    lane_id = fields.Many2one(
        "nsp.parking.lane", required=True, ondelete="cascade", index=True,
    )
    reader_id = fields.Many2one(
        "nsp.device", string="Reader", required=True, ondelete="restrict", index=True,
    )
    reader_name = fields.Char(related="reader_id.name", string="Reader Name", readonly=True)
    reader_serial_number = fields.Char(related="reader_id.serial_number", string="Serial", readonly=True)
    reader_status = fields.Selection(related="reader_id.status", string="Reader Status", readonly=True)
    power_dbm = fields.Integer(string="Power (dBm)", required=True)
    read_interval_ms = fields.Integer(string="Read Interval (ms)", required=True)
    tid_start_address = fields.Integer(string="TID Start Address (Words)", required=True)
    tid_length = fields.Integer(string="TID Length (Words)", required=True)
    source_type = fields.Selection(
        [("published_layout", "Published Layout")],
        string="Source", required=True, default="published_layout", readonly=True,
    )
    source_revision = fields.Integer(
        string="Layout Revision", related="lane_id.parking_area_id.published_revision",
        readonly=True,
    )
    port_summary = fields.Char(string="Ports", compute="_compute_port_summary")

    _sql_constraints = [
        (
            "edge_lane_reader_config_unique",
            "unique(lane_id, reader_id)",
            "A Reader can have only one Applied Configuration per Lane.",
        ),
        (
            "edge_lane_reader_power_range",
            "CHECK(power_dbm >= 0 AND power_dbm <= 40)",
            "Reader Power must be between 0 and 40 dBm.",
        ),
        (
            "edge_lane_reader_interval_range",
            "CHECK(read_interval_ms > 0 AND read_interval_ms <= 60000)",
            "Read Interval must be between 1 and 60000 ms.",
        ),
        (
            "edge_lane_reader_tid_addr_nonnegative",
            "CHECK(tid_start_address >= 0)",
            "TID Start Address cannot be negative.",
        ),
        (
            "edge_lane_reader_tid_length_positive",
            "CHECK(tid_length > 0)",
            "TID Length must be greater than zero.",
        ),
    ]

    @api.depends(
        "lane_id.antenna_sequence_ids.reader_id",
        "lane_id.antenna_sequence_ids.port_no",
        "reader_id",
    )
    def _compute_port_summary(self):
        for config in self:
            ports = sorted({
                int(line.port_no or 0)
                for line in config.lane_id.antenna_sequence_ids
                if line.reader_id == config.reader_id and int(line.port_no or 0) > 0
            })
            config.port_summary = ", ".join("P%s" % port for port in ports)

    def _validate_parameter_ranges(self):
        for config in self:
            if config.power_dbm < 0 or config.power_dbm > 40:
                raise ValidationError(_("Reader Power must be between 0 and 40 dBm."))
            if config.read_interval_ms <= 0 or config.read_interval_ms > 60000:
                raise ValidationError(_("Read Interval must be between 1 and 60000 ms."))
            if config.tid_start_address < 0:
                raise ValidationError(_("TID Start Address cannot be negative."))
            if config.tid_length <= 0:
                raise ValidationError(_("TID Length must be greater than zero."))
        return True

    @api.constrains(
        "power_dbm", "read_interval_ms", "tid_start_address", "tid_length",
    )
    def _check_parameter_ranges(self):
        self._validate_parameter_ranges()


class NspParkingLaneTimeline(models.Model):
    _name = "nsp.parking.lane.timeline"
    _description = "NSP Edge Parking Lane Antenna Sequence"
    _order = "lane_id, sequence, id"

    lane_id = fields.Many2one(
        "nsp.parking.lane", required=True, ondelete="cascade", index=True,
    )
    sequence = fields.Integer(required=True, index=True)
    reader_id = fields.Many2one(
        "nsp.device", required=True, ondelete="restrict", index=True,
    )
    port_no = fields.Integer(required=True, index=True)
    duration_from_previous = fields.Float(
        required=True, digits=(8, 3), default=0.0,
    )
    cumulative_time = fields.Float(digits=(8, 3), default=0.0)
    is_first_point = fields.Boolean(
        string="First Point", compute="_compute_is_first_point", readonly=True,
    )

    @api.depends("sequence")
    def _compute_is_first_point(self):
        for record in self:
            record.is_first_point = int(record.sequence or 0) == 1

    _sql_constraints = [
        (
            "edge_lane_timeline_order_unique",
            "unique(lane_id, sequence)",
            "Timeline Order must be unique per Lane.",
        ),
        (
            "edge_lane_timeline_reader_port_unique",
            "unique(lane_id, reader_id, port_no)",
            "A Reader Port can appear only once in a Lane Timeline.",
        ),
        (
            "edge_lane_timeline_sequence_positive",
            "CHECK(sequence > 0)",
            "Timeline Order must be greater than zero.",
        ),
        (
            "edge_lane_timeline_port_range",
            "CHECK(port_no >= 1 AND port_no <= 16)",
            "Timeline Reader Port must be between 1 and 16.",
        ),
        (
            "edge_lane_timeline_duration_nonnegative",
            "CHECK(duration_from_previous >= 0)",
            "Timeline Duration cannot be negative.",
        ),
    ]

    @api.constrains("lane_id", "reader_id", "port_no", "sequence")
    def _check_reader_port(self):
        for record in self:
            if record.reader_id.controller_id != record.lane_id.controller_id:
                raise ValidationError(
                    _("Timeline Reader must belong to the Lane Controller.")
                )
            if int(record.port_no or 0) < 1 or int(record.port_no or 0) > 16:
                raise ValidationError(
                    _("Timeline Reader Port must be between 1 and 16.")
                )

    def _sync_payload(self):
        self.ensure_one()
        return {
            "sequence": int(self.sequence or 0),
            "reader_code": self.reader_id.device_code or "",
            "reader_serial_number": self.reader_id.serial_number or "",
            "port_no": int(self.port_no or 0),
            "duration_from_previous_seconds": float(
                self.duration_from_previous or 0.0
            ),
            "cumulative_time_seconds": float(self.cumulative_time or 0.0),
        }


class NspParkingLaneEventSequence(models.Model):
    _name = "nsp.parking.lane.event.sequence"
    _description = "NSP Edge Parking Lane Event Sequence"
    _order = "lane_id, sequence_type, sequence, id"

    lane_id = fields.Many2one(
        "nsp.parking.lane", required=True, ondelete="cascade", index=True,
    )
    sequence_type = fields.Selection(
        [("check_in", "Check-in"), ("check_out", "Check-out")],
        required=True,
        index=True,
    )
    sequence = fields.Integer(required=True)
    reader_id = fields.Many2one(
        "nsp.device", required=True, ondelete="restrict", index=True,
    )
    port_no = fields.Integer(required=True, index=True)
    duration_from_previous = fields.Float(
        string="Max Duration from Previous (s)",
        required=True,
        digits=(8, 3),
        default=0.0,
        help="Direction-specific maximum time from the previous Antenna.",
    )

    _sql_constraints = [
        (
            "edge_lane_event_sequence_order_unique",
            "unique(lane_id, sequence_type, sequence)",
            "Event Sequence Order must be unique.",
        ),
        (
            "edge_lane_event_sequence_reader_port_unique",
            "unique(lane_id, sequence_type, reader_id, port_no)",
            "A Reader Port can appear only once in one Event Sequence.",
        ),
        (
            "edge_lane_event_sequence_positive",
            "CHECK(sequence > 0)",
            "Event Sequence Order must be greater than zero.",
        ),
        (
            "edge_lane_event_sequence_port_range",
            "CHECK(port_no >= 1 AND port_no <= 16)",
            "Event Sequence Reader Port must be between 1 and 16.",
        ),
        (
            "edge_lane_event_sequence_duration_nonnegative",
            "CHECK(duration_from_previous >= 0)",
            "Lane Direction Duration cannot be negative.",
        ),
    ]

    @api.constrains("lane_id", "reader_id", "port_no", "sequence", "duration_from_previous")
    def _check_reader_port(self):
        for record in self:
            allowed = {
                (line.reader_id.id, int(line.port_no or 0))
                for line in record.lane_id.timeline_line_ids
            }
            key = (record.reader_id.id, int(record.port_no or 0))
            if key not in allowed:
                raise ValidationError(
                    _("Lane Direction can use only Reader Ports from Lane Calibration.")
                )
            if record.sequence == 1 and float(record.duration_from_previous or 0.0) != 0.0:
                raise ValidationError(_("The first Lane Direction Antenna must use 0 ms Max Duration."))
            if record.sequence > 1 and float(record.duration_from_previous or 0.0) <= 0.0:
                raise ValidationError(_("Lane Direction Antennas after the first require a positive Max Duration."))

    def _sync_payload(self):
        self.ensure_one()
        return {
            "reader_code": self.reader_id.device_code or "",
            "port_no": int(self.port_no or 0),
            "duration_from_previous_seconds": float(self.duration_from_previous or 0.0),
        }

