# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import AccessError, ValidationError
from odoo.addons.nsp_core.utils import new_management_code


class NspParkingArea(models.Model):
    """Read-only Edge runtime copy of one published Cloud Parking Layout."""

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
    runtime_snapshot_revision = fields.Integer(
        string="Runtime Snapshot Revision", default=0, readonly=True, copy=False, index=True,
        help=(
            "Top-level Edge runtime snapshot revision that last applied this Parking Layout. "
            "This is distinct from the Parking Layout published revision."
        ),
    )
    runtime_synced_at = fields.Datetime(
        string="Synchronized At", readonly=True, copy=False, index=True,
    )
    is_published = fields.Boolean(compute="_compute_is_published")

    # Parking Layout owns only contextual Lane Configuration rows. Lane Master is
    # independent and survives Layout replacement/removal.
    layout_lane_ids = fields.One2many(
        "nsp.parking.layout.lane", "parking_area_id", string="Lane Configurations",
    )

    edge_server_ids = fields.Many2many(
        "nsp.edge.server", string="Servers", compute="_compute_topology",
    )
    controller_ids = fields.Many2many(
        "nsp.controller", string="Controllers", compute="_compute_topology",
        search="_search_controllers",
    )
    reader_ids = fields.Many2many(
        "nsp.device", string="Readers", compute="_compute_topology",
    )
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
        "layout_lane_ids.active",
        "layout_lane_ids.edge_server_id",
        "layout_lane_ids.controller_id",
        "layout_lane_ids.reader_config_ids.reader_id",
        "layout_lane_ids.reader_config_ids.port_ids.port_no",
    )
    def _compute_topology(self):
        for record in self:
            configurations = record.layout_lane_ids.filtered("active")
            record.edge_server_ids = configurations.mapped("edge_server_id")
            record.controller_ids = configurations.mapped("controller_id")
            record.reader_ids = configurations.mapped("reader_config_ids.reader_id")

    @api.model
    def _search_controllers(self, operator, value):
        return [("layout_lane_ids.controller_id", operator, value)]

    @api.depends(
        "edge_server_ids", "controller_ids", "reader_ids", "layout_lane_ids.active",
    )
    def _compute_counts(self):
        for record in self:
            record.edge_server_count = len(record.edge_server_ids)
            record.controller_count = len(record.controller_ids)
            record.reader_count = len(record.reader_ids)
            record.lane_count = len(record.layout_lane_ids.filtered("active"))

    @api.depends("layout_lane_ids.active", "layout_lane_ids.edge_server_id.edge_server_code")
    def _compute_published_edge_server_codes(self):
        for record in self:
            record.published_edge_server_codes = ", ".join(sorted(
                code for code in record.edge_server_ids.mapped("edge_server_code") if code
            ))

    @api.depends(
        "layout_lane_ids.active",
        "layout_lane_ids.configuration_state",
        "layout_lane_ids.configuration_issue",
        "layout_lane_ids.antenna_sequence_ids",
    )
    def _compute_configuration_health(self):
        for record in self:
            active_configurations = record.layout_lane_ids.filtered("active")
            ready = active_configurations.filtered(
                lambda row: row.configuration_state == "ready"
            )
            incomplete = active_configurations - ready
            record.ready_lane_count = len(ready)
            record.incomplete_lane_count = len(incomplete)
            if not active_configurations:
                record.configuration_state = "empty"
                record.configuration_summary = _("No active Lane Configuration is synchronized.")
            elif incomplete:
                record.configuration_state = "incomplete"
                record.configuration_summary = _(
                    "%(ready)s ready · %(incomplete)s need attention"
                ) % {"ready": len(ready), "incomplete": len(incomplete)}
            else:
                record.configuration_state = "ready"
                record.configuration_summary = _(
                    "All %(count)s active Lane Configurations match the synchronized runtime snapshot."
                ) % {"count": len(active_configurations)}

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
        logs = self.env["nsp.parking.log"].search(
            [("parking_area_id", "=", area.id)],
            order="event_time desc, id desc", limit=limit,
        )
        return {
            "found": True,
            "parking_area_id": area.id,
            "parking_area_name": area.name,
            "branch_name": area.branch_id.name or "",
            "state": area.state,
            "items": [log._live_monitor_payload() for log in logs[::-1]],
        }

    def _lane_payload(self):
        self.ensure_one()
        configurations = self.layout_lane_ids.filtered("active").sorted(
            key=lambda row: (
                (row.lane_id.name or "").casefold(), row.lane_id.code or "", row.id
            )
        )
        return [row._runtime_payload() for row in configurations]

    def _operational_issues(self):
        self.ensure_one()
        issues = []
        configurations = self.layout_lane_ids.filtered("active")
        if not configurations:
            return [_('Configure at least one active Lane Configuration.')]
        for configuration in configurations:
            try:
                configuration._validate_runtime_configuration()
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
        context = {
            "default_controller_id": self.controller_ids.id
        } if len(self.controller_ids) == 1 else {}
        return self._open_related_action(
            "nsp_business_gatekeeper.nsp_device_action",
            self.reader_ids,
            _("Readers"),
            context,
        )

    def action_open_lanes(self):
        self.ensure_one()
        self.check_access("read")
        action = self.env.ref(
            "nsp_business_gatekeeper.action_nsp_parking_layout_lane"
        ).read()[0]
        action.update({
            "name": _("Lane Configurations"),
            "domain": [("parking_area_id", "=", self.id)],
            "context": {"create": False, "edit": False, "delete": False},
        })
        return action


class NspParkingLane(models.Model):
    """Stable Lane Master identity, independent from every Parking Layout."""

    _name = "nsp.parking.lane"
    _description = "NSP Parking Lane Master"
    _order = "branch_id, name, code, id"
    _rec_name = "display_name"

    name = fields.Char(string="Lane Name", required=True, default="Lane")
    code = fields.Char(
        string="Lane Code", required=True, readonly=True, copy=False, index=True,
        default=lambda self: new_management_code("LANE"),
    )
    branch_id = fields.Many2one(
        "nsp.branch", string="Branch", required=True,
        ondelete="restrict", index=True,
    )
    active = fields.Boolean(default=True, index=True)
    display_name = fields.Char(compute="_compute_display_name", store=True)
    layout_lane_ids = fields.One2many(
        "nsp.parking.layout.lane", "lane_id", string="Parking Layout References",
        readonly=True,
    )
    layout_count = fields.Integer(string="Parking Layouts", compute="_compute_layout_count")

    _sql_constraints = [
        ("parking_lane_code_unique", "unique(code)", "Parking Lane Code must be unique."),
    ]

    @api.model
    def _normalize_code(self, value):
        return str(value or "").strip().upper()

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for source in vals_list:
            values = dict(source)
            values["code"] = self._normalize_code(
                values.get("code") or new_management_code("LANE")
            )
            prepared.append(values)
        return super().create(prepared)

    def write(self, vals):
        values = dict(vals)
        if "code" in values:
            values["code"] = self._normalize_code(values.get("code"))
        if values.get("branch_id"):
            target_branch = self.env["nsp.branch"].browse(int(values["branch_id"])).exists()
            for record in self:
                mismatched = record.layout_lane_ids.filtered(
                    lambda item: item.parking_area_id.branch_id != target_branch
                )
                if mismatched:
                    raise ValidationError(_(
                        "Lane Branch cannot be changed while the Lane is referenced by Parking Layout(s) from another Branch: %s"
                    ) % ", ".join(mismatched.mapped("parking_area_id.display_name")))
        return super().write(values)

    @api.depends("name", "code", "branch_id.name")
    def _compute_display_name(self):
        for record in self:
            label = record.name or record.code or _("Lane")
            record.display_name = (
                "%s / %s" % (record.branch_id.name, label)
                if record.branch_id else label
            )

    @api.depends("layout_lane_ids")
    def _compute_layout_count(self):
        for record in self:
            record.layout_count = len(record.layout_lane_ids)


class NspParkingLayoutLane(models.Model):
    """Contextual runtime Lane Configuration owned by one Parking Layout."""

    _name = "nsp.parking.layout.lane"
    _description = "NSP Edge Parking Layout Lane Configuration"
    _order = "parking_area_id, sequence, lane_id, id"
    _rec_name = "display_name"

    parking_area_id = fields.Many2one(
        "nsp.parking.area", string="Parking Layout", required=True,
        ondelete="cascade", index=True,
    )
    lane_id = fields.Many2one(
        "nsp.parking.lane", string="Lane", required=True,
        ondelete="restrict", index=True,
    )
    sequence = fields.Integer(default=10)
    display_name = fields.Char(compute="_compute_display_name", store=True)
    lane_name = fields.Char(related="lane_id.name", string="Lane Name", readonly=True)
    lane_code = fields.Char(related="lane_id.code", string="Lane Code", readonly=True)
    branch_id = fields.Many2one(
        related="parking_area_id.branch_id", store=True, readonly=True,
    )

    # Infrastructure and Reader settings belong to the Layout context, not Lane Master.
    edge_server_id = fields.Many2one(
        "nsp.edge.server", string="Server", required=True,
        ondelete="restrict", index=True,
    )
    controller_id = fields.Many2one(
        "nsp.controller", string="Controller", required=True,
        ondelete="restrict", index=True,
    )
    edge_server_name = fields.Char(
        related="edge_server_id.name", string="Server Name", readonly=True,
    )
    edge_server_status = fields.Selection(
        related="edge_server_id.status", string="Server Status", readonly=True,
    )
    controller_name = fields.Char(
        related="controller_id.controller_name", string="Controller Name", readonly=True,
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

    antenna_sequence_ids = fields.One2many(
        "nsp.parking.layout.lane.sequence", "layout_lane_id", string="Antenna Sequence",
    )
    reader_config_ids = fields.One2many(
        "nsp.parking.layout.lane.reader.config", "layout_lane_id",
        string="Device Configuration",
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
        (
            "parking_layout_lane_unique", "unique(parking_area_id, lane_id)",
            "A Lane can be referenced only once in one Parking Layout.",
        ),
        (
            "parking_layout_lane_tolerance_nonnegative", "CHECK(tolerance_value >= 0)",
            "Timing Tolerance cannot be negative.",
        ),
    ]

    @api.constrains("parking_area_id", "lane_id")
    def _check_branch_scope(self):
        for record in self:
            if (
                record.parking_area_id and record.lane_id
                and record.parking_area_id.branch_id != record.lane_id.branch_id
            ):
                raise ValidationError(_(
                    "Lane %(lane)s belongs to another Branch and cannot be used by Parking Layout %(layout)s."
                ) % {
                    "lane": record.lane_id.display_name,
                    "layout": record.parking_area_id.display_name,
                })

    @api.depends("parking_area_id.name", "lane_id.name")
    def _compute_display_name(self):
        for record in self:
            record.display_name = "%s / %s" % (
                record.parking_area_id.name or _("Parking Layout"),
                record.lane_id.name or _("Lane"),
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
            record.sequence_point_count = len(record.antenna_sequence_ids)

    @api.depends("reader_config_ids")
    def _compute_reader_config_count(self):
        for record in self:
            record.reader_config_count = len(record.reader_config_ids)

    @api.depends(
        "active", "lane_id.active", "edge_server_id", "controller_id",
        "antenna_sequence_ids", "antenna_sequence_ids.sequence",
        "antenna_sequence_ids.reader_id", "antenna_sequence_ids.port_no",
        "antenna_sequence_ids.duration_from_previous",
        "reader_config_ids", "reader_config_ids.reader_id",
        "reader_config_ids.port_ids.port_no",
        "reader_config_ids.power_dbm", "reader_config_ids.read_interval_ms",
        "reader_config_ids.tid_start_address", "reader_config_ids.tid_length",
    )
    def _compute_configuration_health(self):
        for configuration in self:
            if not configuration.active:
                configuration.configuration_state = "disabled"
                configuration.configuration_issue = _("Lane Configuration is disabled.")
                continue
            issues = []
            if not configuration.lane_id or not configuration.lane_id.active:
                issues.append(_("Lane Master is missing or inactive"))
            controller = configuration.controller_id
            if not configuration.edge_server_id:
                issues.append(_("Server is missing"))
            if not controller:
                issues.append(_("Controller is missing"))

            sequence = configuration.antenna_sequence_ids.sorted(
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

            config_by_reader = {
                config.reader_id.id: config for config in configuration.reader_config_ids
            }
            for config in configuration.reader_config_ids:
                if not config.port_ids:
                    issues.append(_("Configured Reader has no Reader Ports"))
            for point in sequence:
                config = config_by_reader.get(point.reader_id.id)
                if not config:
                    issues.append(_("Antenna Sequence Reader is missing Device Configuration"))
                    continue
                if int(point.port_no or 0) not in set(config.port_ids.mapped("port_no")):
                    issues.append(_("Antenna Sequence uses a Port not declared in Device Configuration"))

            configuration.configuration_state = "incomplete" if issues else "ready"
            configuration.configuration_issue = "; ".join(dict.fromkeys(issues)) if issues else _(
                "Antenna Sequence and Device Configuration are synchronized."
            )

    def _validate_reader_configs(self):
        for configuration in self:
            config_by_reader = {
                config.reader_id.id: config for config in configuration.reader_config_ids
            }
            missing = configuration.antenna_sequence_ids.mapped("reader_id").filtered(
                lambda reader: reader.id not in config_by_reader
            )
            if missing:
                raise ValidationError(_(
                    "Lane Configuration %(lane)s is missing Device Configuration for: %(readers)s"
                ) % {
                    "lane": configuration.display_name,
                    "readers": ", ".join(missing.mapped("display_name")),
                })
            configuration.reader_config_ids._validate_parameter_ranges()
            missing_ports = configuration.reader_config_ids.filtered(lambda config: not config.port_ids)
            if missing_ports:
                raise ValidationError(_(
                    "Lane Configuration %(lane)s requires at least one Port for every configured Reader: %(readers)s"
                ) % {
                    "lane": configuration.display_name,
                    "readers": ", ".join(missing_ports.mapped("reader_id.display_name")),
                })
            for point in configuration.antenna_sequence_ids:
                config = config_by_reader.get(point.reader_id.id)
                if config and int(point.port_no or 0) not in set(config.port_ids.mapped("port_no")):
                    raise ValidationError(_(
                        "Antenna Sequence Reader/Antenna must exist in Device Configuration."
                    ))
        return True

    def allowed_duration_for_step(self, sequence):
        self.ensure_one()
        line = self.antenna_sequence_ids.filtered(
            lambda item: item.sequence == sequence
        )[:1]
        base = float(line.duration_from_previous or 0.0) if line else 0.0
        if self.tolerance_type == "seconds":
            return base + float(self.tolerance_value or 0.0)
        return base * (1.0 + float(self.tolerance_value or 0.0) / 100.0)

    def max_sequence_window(self):
        self.ensure_one()
        ordered = self.antenna_sequence_ids.sorted("sequence")
        if len(ordered) < 2:
            return 1.0
        return max(1.0, sum(
            self.allowed_duration_for_step(row.sequence) for row in ordered[1:]
        ))

    def _validate_runtime_configuration(self):
        for configuration in self:
            controller = configuration.controller_id
            if (
                not controller or not controller.active or controller.cloud_removed
                or not configuration.edge_server_id
            ):
                raise ValidationError(_(
                    "Lane Configuration %(lane)s requires active Server and Controller context."
                ) % {"lane": configuration.display_name})
            sequence = configuration.antenna_sequence_ids.sorted(
                lambda row: (row.sequence or 0, row.id)
            )
            if len(sequence) < 2:
                raise ValidationError(_(
                    "Lane Configuration %(lane)s requires at least two Antenna Sequence points."
                ) % {"lane": configuration.display_name})
            if sequence.mapped("sequence") != list(range(1, len(sequence) + 1)):
                raise ValidationError(_("Antenna Sequence Order must be contiguous and start at 1."))
            keys = [(row.reader_id.id, int(row.port_no or 0)) for row in sequence]
            if len(keys) != len(set(keys)):
                raise ValidationError(_("A Reader Port can appear only once in an Antenna Sequence."))
            for index, row in enumerate(sequence):
                if int(row.port_no or 0) < 1 or int(row.port_no or 0) > 16:
                    raise ValidationError(_("Antenna/Port must be an integer from 1 to 16."))
                duration = float(row.duration_from_previous or 0.0)
                if index == 0 and duration != 0.0:
                    raise ValidationError(_("The first Antenna must use 0 seconds Max Duration."))
                if index > 0 and duration <= 0.0:
                    raise ValidationError(_("Every Antenna after the first requires a positive Max Duration."))
            configuration._validate_reader_configs()
        return True

    def _runtime_payload(self):
        self.ensure_one()
        sequence_payload = []
        for order, row in enumerate(self.antenna_sequence_ids.sorted("sequence"), start=1):
            sequence_payload.append({
                "sequence": order,
                "reader_code": row.reader_id.device_code or "",
                "reader_serial_number": row.reader_id.serial_number or "",
                "port_no": int(row.port_no or 0),
                "duration_from_previous_seconds": (
                    0.0 if order == 1 else float(row.duration_from_previous or 0.0)
                ),
            })
        readers = []
        for config in self.reader_config_ids.sorted(
            key=lambda row: (
                row.reader_id.serial_number or "", row.reader_id.device_code or "", row.id
            )
        ):
            readers.append({
                "technical_code": config.reader_id.device_code or "",
                "serial_number": config.reader_id.serial_number or "",
                "reader_name": config.reader_id.name or config.reader_id.serial_number or "",
                "reader_parameters": {
                    "power_dbm": int(config.power_dbm or 0),
                    "read_interval_ms": int(config.read_interval_ms or 200),
                    "tid_start_address": int(config.tid_start_address or 0),
                    "tid_length": int(config.tid_length or 4),
                },
                "ports": [
                    {"port_no": int(port.port_no or 0)}
                    for port in config.port_ids.sorted("port_no")
                ],
            })
        return {
            "lane_code": self.lane_id.code or "",
            "lane_name": self.lane_id.name or "",
            "server_code": self.edge_server_id.edge_server_code or "",
            "controller_code": self.controller_id.controller_id or "",
            "antenna_sequence": sequence_payload,
            "timing_tolerance": {
                "type": self.tolerance_type,
                "value": float(self.tolerance_value or 0.0),
            },
            "readers": readers,
        }


class NspParkingLayoutLaneReaderConfig(models.Model):
    _name = "nsp.parking.layout.lane.reader.config"
    _description = "NSP Edge Parking Layout Lane Reader Configuration"
    _order = "layout_lane_id, reader_id, id"
    _rec_name = "reader_id"

    layout_lane_id = fields.Many2one(
        "nsp.parking.layout.lane", required=True, ondelete="cascade", index=True,
    )
    reader_id = fields.Many2one(
        "nsp.device", string="Reader", required=True, ondelete="restrict", index=True,
    )
    reader_name = fields.Char(related="reader_id.name", string="Reader Name", readonly=True)
    reader_serial_number = fields.Char(
        related="reader_id.serial_number", string="Serial", readonly=True,
    )
    reader_status = fields.Selection(
        [("online", "Online"), ("offline", "Offline"), ("degraded", "Degraded")],
        string="Reader Status", compute="_compute_reader_status", readonly=True,
    )
    power_dbm = fields.Integer(string="Power (dBm)", required=True)
    read_interval_ms = fields.Integer(string="Read Interval (ms)", required=True)
    tid_start_address = fields.Integer(string="TID Start Address (Words)", required=True)
    tid_length = fields.Integer(string="TID Length (Words)", required=True)
    source_type = fields.Selection(
        [("published_layout", "Published Layout")],
        string="Source", required=True, default="published_layout", readonly=True,
    )
    source_revision = fields.Integer(
        string="Layout Revision", related="layout_lane_id.parking_area_id.published_revision",
        readonly=True,
    )
    port_ids = fields.One2many(
        "nsp.parking.layout.lane.reader.port", "reader_config_id", string="Reader Ports",
    )
    port_summary = fields.Char(string="Ports", compute="_compute_port_summary")

    _sql_constraints = [
        (
            "edge_layout_lane_reader_config_unique",
            "unique(layout_lane_id, reader_id)",
            "A Reader can have only one Applied Configuration per Lane Configuration.",
        ),
        (
            "edge_layout_lane_reader_power_range",
            "CHECK(power_dbm >= 0 AND power_dbm <= 40)",
            "Reader Power must be between 0 and 40 dBm.",
        ),
        (
            "edge_layout_lane_reader_interval_range",
            "CHECK(read_interval_ms > 0 AND read_interval_ms <= 60000)",
            "Read Interval must be between 1 and 60000 ms.",
        ),
        (
            "edge_layout_lane_reader_tid_addr_nonnegative",
            "CHECK(tid_start_address >= 0)",
            "TID Start Address cannot be negative.",
        ),
        (
            "edge_layout_lane_reader_tid_length_positive",
            "CHECK(tid_length > 0)",
            "TID Length must be greater than zero.",
        ),
    ]

    def _compute_reader_status(self):
        Observation = self.env["nsp.reader.observation"].sudo()
        for config in self:
            controller = config.layout_lane_id.controller_id
            serial = str(config.reader_id.serial_number or "").strip().upper()
            observation = Observation.search([
                ("controller_id", "=", controller.id),
                ("serial_number", "=", serial),
            ], limit=1) if controller and serial else Observation.browse()
            config.reader_status = observation.status if observation else "offline"

    @api.depends("port_ids.port_no")
    def _compute_port_summary(self):
        for config in self:
            config.port_summary = ", ".join(
                "P%s" % port for port in sorted(config.port_ids.mapped("port_no"))
            )

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

    @api.constrains("power_dbm", "read_interval_ms", "tid_start_address", "tid_length")
    def _check_parameter_ranges(self):
        self._validate_parameter_ranges()


class NspParkingLayoutLaneReaderPort(models.Model):
    _name = "nsp.parking.layout.lane.reader.port"
    _description = "NSP Edge Parking Layout Lane Reader Port"
    _order = "reader_config_id, port_no, id"

    reader_config_id = fields.Many2one(
        "nsp.parking.layout.lane.reader.config",
        required=True, ondelete="cascade", index=True,
    )
    layout_lane_id = fields.Many2one(
        related="reader_config_id.layout_lane_id", store=True, readonly=True, index=True,
    )
    reader_id = fields.Many2one(
        related="reader_config_id.reader_id", store=True, readonly=True, index=True,
    )
    port_no = fields.Integer(required=True, index=True)

    _sql_constraints = [
        (
            "edge_layout_lane_reader_port_unique",
            "unique(reader_config_id, port_no)",
            "Reader Port must be unique per Lane Reader Configuration.",
        ),
        (
            "edge_layout_lane_reader_port_range",
            "CHECK(port_no >= 1 AND port_no <= 16)",
            "Reader Port must be between 1 and 16.",
        ),
    ]


class NspParkingLayoutLaneSequencePoint(models.Model):
    _name = "nsp.parking.layout.lane.sequence"
    _description = "NSP Edge Parking Layout Lane Antenna Sequence"
    _order = "layout_lane_id, sequence, id"

    layout_lane_id = fields.Many2one(
        "nsp.parking.layout.lane", required=True, ondelete="cascade", index=True,
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
            "edge_layout_lane_sequence_order_unique",
            "unique(layout_lane_id, sequence)",
            "Antenna Sequence Order must be unique per Lane Configuration.",
        ),
        (
            "edge_layout_lane_sequence_reader_port_unique",
            "unique(layout_lane_id, reader_id, port_no)",
            "A Reader Port can appear only once in a Lane Configuration Antenna Sequence.",
        ),
        (
            "edge_layout_lane_sequence_positive",
            "CHECK(sequence > 0)",
            "Antenna Sequence Order must be greater than zero.",
        ),
        (
            "edge_layout_lane_sequence_port_range",
            "CHECK(port_no >= 1 AND port_no <= 16)",
            "Antenna Sequence Reader Port must be between 1 and 16.",
        ),
        (
            "edge_layout_lane_sequence_duration_nonnegative",
            "CHECK(duration_from_previous >= 0)",
            "Antenna Sequence Duration cannot be negative.",
        ),
    ]

    @api.constrains("layout_lane_id", "reader_id", "port_no", "sequence")
    def _check_reader_port(self):
        for record in self:
            configuration = record.layout_lane_id
            configs = configuration.reader_config_ids.filtered(
                lambda config: config.reader_id == record.reader_id
            )
            if not configs or int(record.port_no or 0) not in set(configs.port_ids.mapped("port_no")):
                raise ValidationError(_(
                    "Antenna Sequence Reader/Antenna must be declared in Device Configuration."
                ))

    def _sync_payload(self):
        self.ensure_one()
        return {
            "sequence": int(self.sequence or 0),
            "reader_code": self.reader_id.device_code or "",
            "reader_serial_number": self.reader_id.serial_number or "",
            "port_no": int(self.port_no or 0),
            "duration_from_previous_seconds": float(self.duration_from_previous or 0.0),
            "cumulative_time_seconds": float(self.cumulative_time or 0.0),
        }
