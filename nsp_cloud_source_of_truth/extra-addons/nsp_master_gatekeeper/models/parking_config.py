# -*- coding: utf-8 -*-
import json

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError, UserError
from odoo.addons.nsp_core.utils import new_management_code


class NspParkingArea(models.Model):
    """Cloud working layout plus immutable published runtime snapshot."""

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
        "nsp.branch", string="Branch", required=True,
        ondelete="restrict", index=True,
    )
    state = fields.Selection(
        [
            ("draft", "Draft / Configuring"),
            ("operational", "Operational"),
            ("maintenance", "Maintenance"),
            ("blocked", "Blocked"),
        ],
        string="Working State", default="draft", required=True, index=True,
    )
    lane_ids = fields.One2many(
        "nsp.parking.lane", "parking_area_id", string="Parking Lanes",
    )

    # Publication is separate from the editable working state. Returning to Draft
    # does not remove the last published snapshot from Edge.
    published_revision = fields.Integer(
        string="Published Revision", default=0, readonly=True, copy=False, index=True,
    )
    published_at = fields.Datetime(readonly=True, copy=False)
    published_payload_json = fields.Text(readonly=True, copy=False)
    published_edge_server_codes = fields.Char(readonly=True, copy=False)
    is_published = fields.Boolean(compute="_compute_is_published")

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
            ("ready", "Ready to Publish"),
        ],
        string="Configuration Readiness",
        compute="_compute_configuration_health",
    )
    configuration_summary = fields.Char(
        string="Readiness Summary", compute="_compute_configuration_health",
    )

    _sql_constraints = [
        ("code_unique", "unique(code)", "Parking Area Code must be unique."),
    ]

    @api.depends("published_payload_json")
    def _compute_is_published(self):
        for record in self:
            record.is_published = bool(record.published_payload_json)

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

    @api.depends(
        "edge_server_ids", "controller_ids", "reader_ids",
        "lane_ids.active",
    )
    def _compute_counts(self):
        for record in self:
            record.edge_server_count = len(record.edge_server_ids)
            record.controller_count = len(record.controller_ids)
            record.reader_count = len(record.reader_ids)
            record.lane_count = len(record.lane_ids.filtered("active"))

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
                record.configuration_summary = _("No active Parking Lane is configured.")
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
                    "All %(count)s active Lanes have a valid Antenna Sequence."
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
            from odoo.exceptions import AccessError
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
            "items": [tx._live_monitor_payload() for tx in transactions[::-1]],
        }

    def _lane_payload(self):
        self.ensure_one()
        payloads = []
        lanes = self.lane_ids.filtered("active").sorted(
            key=lambda item: ((item.name or "").casefold(), item.code or "", item.id)
        )
        for lane in lanes:
            readers = {}
            antenna_sequence = []
            config_by_reader = {
                config.reader_id.id: config for config in lane.reader_config_ids
            }
            ordered_points = lane.antenna_sequence_ids.sorted(
                lambda row: (row.sequence or 0, row.id)
            )
            for business_order, point in enumerate(ordered_points, start=1):
                reader = point.reader_id
                config = config_by_reader.get(reader.id)
                if not config:
                    raise ValidationError(
                        _("Lane %(lane)s has no Device Configuration for %(reader)s.")
                        % {"lane": lane.display_name, "reader": reader.display_name}
                    )
                reader_payload = readers.setdefault(reader.id, {
                    "technical_code": reader.device_code or "",
                    "serial_number": reader.serial_number or "",
                    "reader_name": reader.name or reader.serial_number or "",
                    "physical_connection": reader.connection_type or False,
                    "reader_parameters": {
                        "power_dbm": int(config.power_dbm or 0),
                        "read_interval_ms": int(config.read_interval_ms or 200),
                        "tid_start_address": int(config.tid_start_address or 0),
                        "tid_length": int(config.tid_length or 4),
                    },
                    "ports": set(),
                })
                reader_payload["ports"].add(int(point.port_no or 0))
                antenna_sequence.append({
                    # ``sequence`` on the Odoo model is only a UI sort priority
                    # (handle widgets legitimately use gaps such as 10, 20, 30).
                    # The published contract owns a dense business order 1..N.
                    "sequence": business_order,
                    "reader_code": reader.device_code or "",
                    "reader_serial_number": reader.serial_number or "",
                    "port_no": int(point.port_no or 0),
                    "duration_from_previous_seconds": (
                        0.0
                        if business_order == 1
                        else float(point.duration_from_previous or 0.0)
                    ),
                })
            payloads.append({
                "lane_code": lane.code,
                "lane_name": lane.name,
                "server_code": lane.edge_server_id.edge_server_code or "",
                "controller_code": lane.controller_id.controller_id or "",
                "antenna_sequence": antenna_sequence,
                "timing_tolerance": {
                    "type": lane.tolerance_type or "percent",
                    "value": float(lane.tolerance_value or 0.0),
                },
                "readers": [
                    {
                        **reader_payload,
                        "ports": [
                            {"port_no": port_no}
                            for port_no in sorted(reader_payload["ports"])
                        ],
                    }
                    for reader_payload in sorted(
                        readers.values(),
                        key=lambda row: (row["serial_number"], row["technical_code"]),
                    )
                ],
            })
        return payloads

    def _build_sync_payload(self, published_state="operational", revision=False):
        self.ensure_one()
        return {
            "parking_area_code": self.code,
            "parking_area_name": self.name,
            "branch_code": self.branch_id.code or "",
            "state": published_state,
            "published_revision": int(revision or self.published_revision or 1),
            "lanes": self._lane_payload(),
        }

    def _validate_sync_payload_contract(self, payload):
        """Reject legacy published snapshots instead of sending mixed schemas."""
        self.ensure_one()
        if not isinstance(payload, dict):
            raise ValidationError(_("Published Parking Layout snapshot must be an object."))
        allowed_root = {
            "parking_area_code", "parking_area_name", "branch_code",
            "state", "published_revision", "lanes",
        }
        unsupported_root = set(payload) - allowed_root
        if unsupported_root:
            raise ValidationError(
                _("Published Parking Layout contains unsupported field(s): %s")
                % ", ".join(sorted(unsupported_root))
            )
        lanes = payload.get("lanes")
        if not isinstance(lanes, list):
            raise ValidationError(_("Published Parking Layout Lanes must be an array."))
        allowed_lane = {
            "lane_code", "lane_name", "server_code", "controller_code",
            "antenna_sequence", "timing_tolerance", "readers",
        }
        required_lane = allowed_lane
        for lane in lanes:
            if not isinstance(lane, dict):
                raise ValidationError(_("Published Parking Layout Lanes must contain objects."))
            unsupported = set(lane) - allowed_lane
            missing = required_lane - set(lane)
            if unsupported or missing:
                details = []
                if unsupported:
                    details.append(_("unsupported: %s") % ", ".join(sorted(unsupported)))
                if missing:
                    details.append(_("missing: %s") % ", ".join(sorted(missing)))
                raise ValidationError(
                    _("Published Parking Layout uses a legacy Lane contract; revise and publish it again (%s).")
                    % "; ".join(details)
                )

            readers = lane.get("readers")
            if not isinstance(readers, list):
                raise ValidationError(_("Published Lane Readers must be an array."))
            allowed_reader = {
                "technical_code", "serial_number", "reader_name",
                "physical_connection", "reader_parameters", "ports",
            }
            for reader in readers:
                if not isinstance(reader, dict):
                    raise ValidationError(_("Published Lane Readers must contain objects."))
                unsupported_reader = set(reader) - allowed_reader
                if unsupported_reader:
                    raise ValidationError(
                        _("Published Lane Reader contains unsupported field(s): %s")
                        % ", ".join(sorted(unsupported_reader))
                    )

            sequence = lane.get("antenna_sequence")
            if not isinstance(sequence, list):
                raise ValidationError(_("Published Antenna Sequence must be an array."))
            allowed_point = {
                "sequence", "reader_code", "reader_serial_number",
                "port_no", "duration_from_previous_seconds",
            }
            for point in sequence:
                if not isinstance(point, dict):
                    raise ValidationError(_("Published Antenna Sequence must contain objects."))
                unsupported_point = set(point) - allowed_point
                if unsupported_point:
                    raise ValidationError(
                        _("Published Antenna Sequence contains unsupported field(s): %s")
                        % ", ".join(sorted(unsupported_point))
                    )
        return True

    def _operational_issues(self):
        self.ensure_one()
        issues = []
        lanes = self.lane_ids.filtered("active")
        if not lanes:
            return [_('Configure at least one active Parking Lane.')]
        lane_by_reader_port = {}
        for lane in lanes:
            try:
                lane._validate_lane_assembly()
                lane._validate_antenna_sequence()
                lane._validate_reader_configs()
            except ValidationError as exc:
                issues.append(str(exc))
                continue
            for point in lane.antenna_sequence_ids:
                key = (point.reader_id.id, int(point.port_no or 0))
                previous_lane = lane_by_reader_port.get(key)
                if previous_lane:
                    issues.append(
                        _("Reader/Antenna %(reader)s:%(port)s is assigned to both %(first)s and %(second)s.")
                        % {
                            "reader": point.reader_id.device_code or point.reader_id.serial_number,
                            "port": point.port_no,
                            "first": previous_lane.display_name,
                            "second": lane.display_name,
                        }
                    )
                else:
                    lane_by_reader_port[key] = lane
        return issues

    def _publish(self, target_state):
        self.ensure_one()
        self.check_access("write")
        record = self
        record._validate_parking_state_transition(target_state)
        revision = int(record.published_revision or 0) + 1
        previous_edge_codes = {
            item.strip().upper()
            for item in str(record.published_edge_server_codes or "").split(",")
            if item.strip()
        }

        if target_state == "operational":
            issues = record._operational_issues()
            if issues:
                raise UserError("\n".join(issues))
            payload = record._build_sync_payload(target_state, revision)
            record._validate_sync_payload_contract(payload)
            current_reader_ports = {
                (
                    str(point.get("reader_code") or "").strip().upper(),
                    int(point.get("port_no") or 0),
                )
                for lane_payload in payload.get("lanes", [])
                for point in lane_payload.get("antenna_sequence", [])
            }
            other_layouts = self.search([
                ("id", "!=", record.id),
                ("published_payload_json", "!=", False),
            ])
            conflicts = []
            for other in other_layouts:
                try:
                    other_payload = json.loads(other.published_payload_json)
                except (TypeError, json.JSONDecodeError) as exc:
                    raise ValidationError(
                        _("Published Parking Layout %(layout)s snapshot is invalid.")
                        % {"layout": other.display_name}
                    ) from exc
                if other_payload.get("state") != "operational":
                    continue
                other_reader_ports = {
                    (
                        str(point.get("reader_code") or "").strip().upper(),
                        int(point.get("port_no") or 0),
                    )
                    for lane_payload in other_payload.get("lanes", [])
                    for point in lane_payload.get("antenna_sequence", [])
                }
                overlap = sorted(current_reader_ports & other_reader_ports)
                if overlap:
                    conflicts.append("%s: %s" % (
                        other.display_name,
                        ", ".join("%s:%s" % item for item in overlap),
                    ))
            if conflicts:
                raise UserError(
                    _("Operational Parking Layout Reader Ports must be exclusive. Conflicts: %s")
                    % "; ".join(conflicts)
                )
            edge_codes = sorted({
                str(lane.get("server_code") or "").strip().upper()
                for lane in payload.get("lanes", [])
                if lane.get("server_code")
            })
        else:
            if not record.published_payload_json:
                raise UserError(_("Publish the Parking Layout before changing its runtime state."))
            payload = dict(record.prepare_sync_payload())
            payload["state"] = target_state
            payload["published_revision"] = revision
            edge_codes = sorted({
                str(lane.get("server_code") or "").strip().upper()
                for lane in payload.get("lanes", [])
                if lane.get("server_code")
            })

        record.with_context(nsp_publishing=True).write({
            "state": target_state,
            "published_revision": revision,
            "published_at": fields.Datetime.now(),
            "published_payload_json": json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
            "published_edge_server_codes": ",".join(edge_codes),
        })
        affected_codes = previous_edge_codes | set(edge_codes)
        if affected_codes:
            affected_edges = self.env["nsp.edge.server"].sudo().with_context(
                active_test=False
            ).search([("edge_server_code", "in", sorted(affected_codes))])
            affected_edges._bump_config_revision()
        return True

    def prepare_sync_payload(self):
        self.ensure_one()
        if not self.published_payload_json:
            return False
        try:
            payload = json.loads(self.published_payload_json)
        except Exception as exc:
            raise ValidationError(_("Published Parking Layout snapshot is invalid.")) from exc
        self._validate_sync_payload_contract(payload)
        return payload

    def is_published_for_edge(self, edge_code):
        self.ensure_one()
        codes = {
            item.strip().upper()
            for item in str(self.published_edge_server_codes or "").split(",")
            if item.strip()
        }
        return str(edge_code or "").strip().upper() in codes


class NspParkingLane(models.Model):
    """One contextual Server + Controller assembly for a physical lane."""

    _name = "nsp.parking.lane"
    _description = "NSP Parking Lane"
    _order = "parking_area_id, name, id"
    _rec_name = "display_name"

    name = fields.Char(string="Lane Name", required=True)
    code = fields.Char(
        string="Lane Code", required=True, readonly=True, copy=False, index=True,
        default=lambda self: new_management_code("LANE"),
    )
    display_name = fields.Char(compute="_compute_display_name", store=True)
    parking_area_id = fields.Many2one(
        "nsp.parking.area", string="Parking Area", required=True,
        ondelete="cascade", index=True,
    )
    edge_server_id = fields.Many2one(
        "nsp.edge.server", string="Server", required=True,
        ondelete="restrict", index=True,
    )
    controller_id = fields.Many2one(
        "nsp.controller", string="Controller", required=True,
        ondelete="restrict", index=True,
    )
    edge_server_status = fields.Selection(
        related="edge_server_id.status", string="Server Status", readonly=True,
    )
    controller_status = fields.Selection(
        related="controller_id.status", string="Controller Status", readonly=True,
    )
    device_tree_anchor = fields.Boolean(
        string="NSP Device Tree", compute="_compute_device_tree_anchor",
    )
    antenna_sequence_preview_anchor = fields.Boolean(
        string="Antenna Sequence Preview", compute="_compute_antenna_sequence_preview_anchor",
    )
    active = fields.Boolean(default=True, index=True)
    setup_state = fields.Selection(
        [("draft", "Draft"), ("applied", "Applied")],
        string="Lane Setup State",
        required=True,
        default="draft",
        copy=True,
        index=True,
        help="Draft Lane Setup cannot be published to Edge until it is applied.",
    )
    setup_applied_at = fields.Datetime(
        string="Lane Setup Applied At", readonly=True, copy=False,
    )
    sequence_point_count = fields.Integer(
        string="Sequence Points", compute="_compute_sequence_point_count"
    )
    timeline_point_count = fields.Integer(
        string="Legacy Timeline Points", compute="_compute_sequence_point_count"
    )
    antenna_sequence_ids = fields.One2many(
        "nsp.parking.lane.timeline", "lane_id", string="Antenna Sequence",
    )
    # NSP 19.x read-compatibility alias. Active code must use
    # ``antenna_sequence_ids``. Removal target: NSP 20.0.
    timeline_line_ids = fields.One2many(
        "nsp.parking.lane.timeline", "lane_id",
        string="Legacy Timeline", readonly=True,
    )
    reader_config_ids = fields.One2many(
        "nsp.parking.lane.reader.config", "lane_id", string="Applied Reader Configuration",
        copy=True,
    )
    reader_config_count = fields.Integer(
        string="Configured Readers", compute="_compute_reader_config_count",
    )
    tolerance_type = fields.Selection(
        [("percent", "Percentage (%)"), ("seconds", "Seconds")],
        string="Tolerance Type", default="percent", required=True,
    )
    tolerance_value = fields.Float(string="Tolerance Value", default=30.0, required=True)
    total_path_duration = fields.Float(
        string="Total Path Duration", compute="_compute_total_path_duration", digits=(8,3),
    )
    parking_area_state = fields.Selection(
        related="parking_area_id.state", string="Layout State", readonly=True,
    )
    configuration_state = fields.Selection(
        [
            ("disabled", "Disabled"),
            ("incomplete", "Needs Attention"),
            ("ready", "Ready"),
        ],
        string="Readiness", compute="_compute_configuration_health",
    )
    configuration_issue = fields.Char(
        string="Configuration Check", compute="_compute_configuration_health",
    )

    @api.depends("reader_config_ids", "edge_server_id", "controller_id")
    def _compute_device_tree_anchor(self):
        for lane in self:
            lane.device_tree_anchor = True

    @api.depends(
        "antenna_sequence_ids.sequence",
        "antenna_sequence_ids.reader_id",
        "antenna_sequence_ids.port_no",
        "antenna_sequence_ids.duration_from_previous",
    )
    def _compute_antenna_sequence_preview_anchor(self):
        for lane in self:
            lane.antenna_sequence_preview_anchor = True

    _sql_constraints = [
        (
            "parking_lane_code_unique", "unique(code)",
            "Parking Lane Code must be unique.",
        ),
        (
            "parking_lane_tolerance_nonnegative", "CHECK(tolerance_value >= 0)",
            "Timing Tolerance cannot be negative.",
        ),
    ]

    _DIRECT_CONFIGURATION_FIELDS = frozenset({
        "edge_server_id",
        "controller_id",
        "reader_config_ids",
        "antenna_sequence_ids",
        "tolerance_type",
        "tolerance_value",
    })

    @api.model_create_multi
    def create(self, vals_list):
        """Create a complete Lane atomically from the Parking Layout popup.

        One2many commands are persisted with Reader auto-sync temporarily disabled;
        after the parent and both child collections exist, Device Configuration is
        normalized once and the complete Lane contract is validated once. This avoids
        duplicate Reader snapshots and partial-validation side effects.
        """
        direct_configuration = bool(self.env.context.get("lane_configuration_form"))
        values_list = [dict(values) for values in vals_list]
        if direct_configuration:
            applied_at = fields.Datetime.now()
            for values in values_list:
                values["setup_state"] = "applied"
                values["setup_applied_at"] = applied_at

        records = super(
            NspParkingLane,
            self.with_context(skip_lane_reader_config_sync=True),
        ).create(values_list)
        records._normalize_first_sequence_duration()
        if direct_configuration:
            records._validate_lane_assembly()
            records._validate_antenna_sequence()
            records._validate_reader_configs()
        return records

    def write(self, vals):
        """Persist popup configuration as one validated Lane configuration change."""
        direct_configuration = bool(self.env.context.get("lane_configuration_form"))
        values = dict(vals)
        if direct_configuration and self._DIRECT_CONFIGURATION_FIELDS.intersection(values):
            values.update({
                "setup_state": "applied",
                "setup_applied_at": fields.Datetime.now(),
            })

        result = super(
            NspParkingLane,
            self.with_context(skip_lane_reader_config_sync=True),
        ).write(values)
        if "antenna_sequence_ids" in values:
            self._normalize_first_sequence_duration()
        if direct_configuration:
            self._validate_lane_assembly()
            self._validate_antenna_sequence()
            self._validate_reader_configs()
        return result

    @api.model
    def name_search(self, name="", domain=None, operator="ilike", limit=100):
        """Make Lane selection reliable in Lane Setup.

        Search the human Lane name/code/parent layout instead of relying only on
        the stored computed display_name. The caller-provided domain remains the
        authoritative scope (Lane Setup currently limits selection to active lanes
        in Draft Parking Layouts).
        """
        search_domain = list(domain or [])
        if name:
            search_domain = [
                "|", "|",
                ("name", operator, name),
                ("code", operator, name),
                ("parking_area_id.name", operator, name),
            ] + search_domain
        records = self.search(search_domain, limit=limit)
        return [(record.id, record.display_name) for record in records]

    @api.model
    def name_create(self, name):
        lane_name = str(name or "").strip()
        if not lane_name:
            raise UserError(_("Lane Name is required."))
        context = self.env.context
        try:
            parking_area_id = int(context.get("default_parking_area_id") or 0)
            edge_server_id = int(context.get("default_edge_server_id") or 0)
            controller_id = int(context.get("default_controller_id") or 0)
        except Exception as exc:
            raise UserError(_("Invalid quick-create Lane context.")) from exc
        if not edge_server_id or not controller_id:
            raise UserError(_(
                "Lane quick-create requires Server and Controller context from Detection Timeline."
            ))
        if not parking_area_id:
            parking_area_id = self._resolve_quick_create_parking_area(
                edge_server_id=edge_server_id,
                controller_id=controller_id,
            )
        if not parking_area_id:
            raise UserError(_(
                "A parent Parking Layout could not be resolved automatically for this new Lane. "
                "Use Create and Edit once to assign the Lane to a Parking Layout."
            ))
        record = self.create({
            "name": lane_name,
            "parking_area_id": parking_area_id,
            "edge_server_id": edge_server_id,
            "controller_id": controller_id,
        })
        return record.id, record.display_name

    def action_open_lane_setup(self):
        """Open Lane Setup from Parking Layout using contextual associations only."""
        self.ensure_one()
        self.check_access("write")
        if self.parking_area_id.state != "draft":
            raise ValidationError(_(
                "Lane Setup can be changed only while Parking Layout is Draft."
            ))

        device_lines = [
            (0, 0, {
                "reader_id": config.reader_id.id,
                "power_dbm": int(config.power_dbm or 0),
                "read_interval_ms": int(config.read_interval_ms or 200),
                "tid_start_address": int(config.tid_start_address or 0),
                "tid_length": int(config.tid_length or 4),
            })
            for config in self.reader_config_ids.sorted(
                lambda row: (row.reader_id.id, row.id)
            )
        ]
        sequence = self.antenna_sequence_ids.sorted(
            lambda row: (row.sequence or 0, row.id)
        )
        sequence_lines = [
            (0, 0, {
                "sequence": index,
                "reader_id": point.reader_id.id,
                "port_no": int(point.port_no or 0),
                "duration_ms": int(round(float(point.duration_from_previous or 0.0) * 1000.0)),
            })
            for index, point in enumerate(sequence, start=1)
        ]

        wizard = self.env["nsp.lane.setup.wizard"].create({
            "source_scope": "parking_layout",
            "lane_id": self.id,
            "edge_server_id": self.edge_server_id.id,
            "controller_id": self.controller_id.id,
            "device_line_ids": device_lines,
            "sequence_line_ids": sequence_lines,
        })
        return {
            "type": "ir.actions.act_window",
            "name": _("Lane Setup"),
            "res_model": "nsp.lane.setup.wizard",
            "res_id": wizard.id,
            "view_mode": "form",
            "views": [(
                self.env.ref(
                    "nsp_master_gatekeeper.view_nsp_lane_direction_setup_wizard_form"
                ).id,
                "form",
            )],
            "target": "new",
            "context": dict(self.env.context),
        }

    @api.model
    def _resolve_quick_create_parking_area(self, edge_server_id, controller_id):
        """Resolve an unambiguous Draft layout without exposing it in Lane Setup.

        Prefer the Draft layout already using the same Server + Controller. If no such
        topology exists yet, a single Draft layout in the system is still unambiguous.
        Never guess when multiple candidates exist.
        """
        ParkingArea = self.env["nsp.parking.area"]
        topology_candidates = ParkingArea.search([
            ("state", "=", "draft"),
            ("lane_ids.edge_server_id", "=", int(edge_server_id)),
            ("lane_ids.controller_id", "=", int(controller_id)),
        ], limit=2)
        if len(topology_candidates) == 1:
            return topology_candidates.id

        draft_candidates = ParkingArea.search([("state", "=", "draft")], limit=2)
        if len(draft_candidates) == 1:
            return draft_candidates.id
        return False

    @api.depends("name", "parking_area_id.name")
    def _compute_display_name(self):
        for record in self:
            record.display_name = "%s / %s" % (
                record.parking_area_id.name or _("Parking"),
                record.name or _("Lane"),
            )


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

    @api.model
    def _default_reader_config_values(self, reader):
        return {
            "reader_id": reader.id,
            "power_dbm": int(reader.power_dbm or 0),
            "read_interval_ms": int(reader.read_interval_ms or 200),
            "tid_start_address": int(reader.tid_addr or 0),
            "tid_length": int(reader.tid_len or 4),
            "source_type": "reader_defaults",
            "source_reference": False,
            "source_revision": 0,
            "applied_at": fields.Datetime.now(),
        }

    def _sync_reader_configs_from_sequence(self):
        """Keep exactly one stable Reader configuration per Antenna Sequence Reader.

        Existing snapshots are preserved so later changes on nsp.device do not
        silently alter a working Lane. Lane Calibration replaces the snapshots
        atomically when Lane Setup is saved.
        """
        Config = self.env["nsp.parking.lane.reader.config"]
        create_values = []
        stale_configs = Config.browse()
        for lane in self:
            readers = lane.antenna_sequence_ids.mapped("reader_id")
            existing_reader_ids = set(lane.reader_config_ids.mapped("reader_id").ids)
            for reader in readers.filtered(
                lambda item: item.id not in existing_reader_ids
            ):
                values = lane._default_reader_config_values(reader)
                values["lane_id"] = lane.id
                create_values.append(values)
            stale_configs |= lane.reader_config_ids.filtered(
                lambda config: config.reader_id not in readers
            )
        if create_values:
            Config.create(create_values)
        if stale_configs:
            stale_configs.unlink()
        return True

    def _sync_reader_configs_from_timeline(self):
        """NSP 19.x compatibility alias. Removal target: NSP 20.0."""
        return self._sync_reader_configs_from_sequence()

    def _validate_reader_configs(self):
        for lane in self:
            sequence_readers = lane.antenna_sequence_ids.mapped("reader_id")
            config_by_reader = {
                config.reader_id.id: config for config in lane.reader_config_ids
            }
            missing = sequence_readers.filtered(
                lambda reader: reader.id not in config_by_reader
            )
            if missing:
                raise ValidationError(
                    _("Lane %(lane)s is missing Device Configuration for: %(readers)s")
                    % {
                        "lane": lane.display_name,
                        "readers": ", ".join(missing.mapped("display_name")),
                    }
                )
            lane.reader_config_ids._validate_parameter_ranges()
        return True


    @api.depends(
        "active",
        "setup_state",
        "edge_server_id",
        "controller_id",
        "antenna_sequence_ids",
        "antenna_sequence_ids.sequence",
        "antenna_sequence_ids.reader_id",
        "antenna_sequence_ids.port_no",
        "antenna_sequence_ids.duration_from_previous",
        "reader_config_ids",
        "reader_config_ids.reader_id",
        "reader_config_ids.power_dbm",
        "reader_config_ids.read_interval_ms",
        "reader_config_ids.tid_start_address",
        "reader_config_ids.tid_length",
    )
    def _compute_configuration_health(self):
        for lane in self:
            if not lane.active:
                lane.configuration_state = "disabled"
                lane.configuration_issue = _("Lane is disabled.")
                continue
            issues = []
            if lane.setup_state == "draft":
                issues.append(_("Lane Setup is Draft; save Lane Setup before publishing"))
            if not lane.edge_server_id:
                issues.append(_("Server is missing"))
            if not lane.controller_id:
                issues.append(_("Controller is missing"))
            sequence = lane.antenna_sequence_ids.sorted(
                lambda row: (row.sequence or 0, row.id)
            )
            if len(sequence) < 2:
                issues.append(_("Antenna Sequence needs at least 2 points"))
            keys = [(row.reader_id.id, int(row.port_no or 0)) for row in sequence]
            if len(keys) != len(set(keys)):
                issues.append(_("Antenna Sequence contains duplicate Reader/Antenna points"))
            if any(float(row.duration_from_previous or 0.0) <= 0.0 for row in sequence[1:]):
                issues.append(_("Antenna Sequence points after the first require positive Max Duration"))

            sequence_reader_ids = set(sequence.mapped("reader_id").ids)
            configured_reader_ids = set(lane.reader_config_ids.mapped("reader_id").ids)
            if not sequence_reader_ids.issubset(configured_reader_ids):
                issues.append(_("Device Configuration is missing one or more Readers used by Antenna Sequence"))

            lane.configuration_state = "incomplete" if issues else "ready"
            lane.configuration_issue = "; ".join(issues) if issues else _("Antenna Sequence is ready.")

    @api.model
    def _validate_whitelist_identity(self, record, type_code, label):
        if (
            not record or not record.active or not record.whitelist_id
            or not record.whitelist_id.active
            or record.whitelist_id.device_type_code != type_code
        ):
            raise ValidationError(
                _("%(label)s must be an active device from Device Whitelist.")
                % {"label": label}
            )

    def _validate_lane_assembly(self):
        for record in self:
            if not record.active:
                continue
            self._validate_whitelist_identity(record.edge_server_id, "SERVER", _("Server"))
            self._validate_whitelist_identity(record.controller_id, "CONTROLLER", _("Controller"))
        return True

    def _normalize_first_sequence_duration(self):
        """Persist the first ordered Antenna point with a 0-second edge duration.

        ``duration_from_previous`` describes an edge between two consecutive points.
        The first point has no previous edge, so its value is system-owned and must
        never depend on user input or on the numeric value stored in ``sequence``.
        """
        first_points = self.env["nsp.parking.lane.timeline"].browse()
        for lane in self:
            ordered_points = lane.antenna_sequence_ids.sorted(
                lambda row: (row.sequence or 0, row.id)
            )
            if ordered_points:
                first_points |= ordered_points[:1]
        first_points = first_points.filtered(
            lambda point: float(point.duration_from_previous or 0.0) != 0.0
        )
        if first_points:
            first_points.with_context(
                skip_first_duration_normalization=True,
                skip_lane_reader_config_sync=True,
            ).write({"duration_from_previous": 0.0})
        return True

    def _validate_antenna_sequence(self):
        """Validate the single business-authoritative Antenna Sequence."""
        for lane in self:
            sequence = lane.antenna_sequence_ids.sorted(
                lambda row: (row.sequence or 0, row.id)
            )
            if len(sequence) < 2:
                raise ValidationError(_("Lane Antenna Sequence requires at least two Antennas."))
            keys = [(row.reader_id.id, int(row.port_no or 0)) for row in sequence]
            if len(keys) != len(set(keys)):
                raise ValidationError(_(
                    "A Reader/Antenna can appear only once in one Lane Antenna Sequence."
                ))
            for row in sequence:
                if not row.reader_id.active:
                    raise ValidationError(_("Every Reader used by Lane Setup must be active."))
                lane._validate_whitelist_identity(
                    row.reader_id, "RFID_READER", _("Reader")
                )
                if not 1 <= int(row.port_no or 0) <= 16:
                    raise ValidationError(_(
                        "Lane Setup Antenna/Port must be an integer from 1 to 16."
                    ))
            if any(
                float(row.duration_from_previous or 0.0) <= 0.0
                for row in sequence[1:]
            ):
                raise ValidationError(_(
                    "Every Antenna after the first requires a positive Max Duration."
                ))
        return True

    def _validate_timeline_and_sequences(self):
        """NSP 19.x compatibility alias. Removal target: NSP 20.0."""
        return self._validate_antenna_sequence()

    @api.constrains(
        "antenna_sequence_ids",
        "antenna_sequence_ids.sequence",
        "antenna_sequence_ids.reader_id",
        "antenna_sequence_ids.port_no",
        "antenna_sequence_ids.duration_from_previous",
    )
    def _check_antenna_sequence(self):
        self._validate_antenna_sequence()



class NspParkingLaneReaderConfig(models.Model):
    _name = "nsp.parking.lane.reader.config"
    _description = "NSP Parking Lane Applied Reader Configuration"
    _order = "lane_id, reader_id, id"
    _rec_name = "reader_id"

    lane_id = fields.Many2one(
        "nsp.parking.lane", required=True, ondelete="cascade", index=True,
    )
    reader_id = fields.Many2one(
        "nsp.device", string="Reader", required=True, ondelete="restrict", index=True,
    )
    reader_name = fields.Char(related="reader_id.name", string="Reader Name", readonly=True)
    reader_serial_number = fields.Char(
        related="reader_id.serial_number", string="Serial", readonly=True,
    )
    reader_status = fields.Selection(
        related="reader_id.status", string="Operational Status", readonly=True,
    )
    power_dbm = fields.Integer(string="Power (dBm)", required=True, default=30)
    read_interval_ms = fields.Integer(
        string="Read Interval (ms)", required=True, default=200,
    )
    tid_start_address = fields.Integer(
        string="TID Start Address (Words)", required=True, default=0,
    )
    tid_length = fields.Integer(
        string="TID Length (Words)", required=True, default=4,
    )
    source_type = fields.Selection(
        [
            ("reader_defaults", "Reader Defaults"),
            ("lane_calibration", "Lane Calibration"),
            ("manual", "Manual"),
        ],
        string="Source", required=True, default="reader_defaults", readonly=True,
    )
    source_reference = fields.Char(string="Source Reference", readonly=True)
    source_revision = fields.Integer(string="Source Revision", readonly=True)
    applied_at = fields.Datetime(string="Applied At", readonly=True)
    port_summary = fields.Char(string="Ports", compute="_compute_port_summary")

    _sql_constraints = [
        (
            "lane_reader_config_unique",
            "unique(lane_id, reader_id)",
            "A Reader can have only one Applied Configuration per Lane.",
        ),
        (
            "lane_reader_power_range",
            "CHECK(power_dbm >= 0 AND power_dbm <= 40)",
            "Reader Power must be between 0 and 40 dBm.",
        ),
        (
            "lane_reader_interval_range",
            "CHECK(read_interval_ms > 0 AND read_interval_ms <= 60000)",
            "Read Interval must be between 1 and 60000 ms.",
        ),
        (
            "lane_reader_tid_addr_nonnegative",
            "CHECK(tid_start_address >= 0)",
            "TID Start Address cannot be negative.",
        ),
        (
            "lane_reader_tid_length_positive",
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

    def write(self, vals):
        values = dict(vals)
        technical_fields = {
            "power_dbm", "read_interval_ms", "tid_start_address", "tid_length",
        }
        if technical_fields.intersection(values) and not (self.env.context.get("lane_calibration_apply") or self.env.context.get("lane_setup")):
            values.update({
                "source_type": "manual",
                "source_reference": False,
                "source_revision": 0,
                "applied_at": fields.Datetime.now(),
            })
        result = super().write(values)
        self._validate_parameter_ranges()
        return result



class NspParkingLaneSequencePoint(models.Model):
    _name = "nsp.parking.lane.timeline"
    _description = "NSP Parking Lane Antenna Sequence Point"
    _order = "lane_id, sequence, id"

    lane_id = fields.Many2one("nsp.parking.lane", required=True, ondelete="cascade", index=True)
    sequence = fields.Integer(
        string="Order",
        required=True,
        help=(
            "UI sort priority only. Values do not need to be contiguous; "
            "published Lane order is normalized to 1..N."
        ),
    )
    reader_id = fields.Many2one("nsp.device", string="Reader", required=True, ondelete="restrict")
    port_no = fields.Integer(string="Port", required=True)
    duration_from_previous = fields.Float(string="Duration from previous (s)", required=True, digits=(8, 3), default=0.0)
    is_first_point = fields.Boolean(string="First Point", compute="_compute_is_first_point")
    cumulative_time = fields.Float(string="Cumulative Time (s)", compute="_compute_cumulative_time", digits=(8, 3))

    _sql_constraints = [
        ("lane_timeline_order_unique", "unique(lane_id, sequence)", "Antenna Sequence Order must be unique per Lane."),
        ("lane_timeline_reader_port_unique", "unique(lane_id, reader_id, port_no)", "A Reader/Antenna can appear only once per Lane Antenna Sequence."),
        ("lane_timeline_sequence_positive", "CHECK(sequence > 0)", "Antenna Sequence Order must be greater than zero."),
        ("lane_timeline_port_range", "CHECK(port_no >= 1 AND port_no <= 16)", "Antenna Sequence Port must be between 1 and 16."),
        ("lane_timeline_duration_nonnegative", "CHECK(duration_from_previous >= 0)", "Antenna Sequence Duration cannot be negative."),
    ]

    @api.depends("lane_id.antenna_sequence_ids.sequence")
    def _compute_is_first_point(self):
        for record in self:
            ordered_points = record.lane_id.antenna_sequence_ids.sorted(
                lambda row: (row.sequence or 0, row.id)
            ) if record.lane_id else self.browse()
            record.is_first_point = bool(ordered_points and ordered_points[0] == record)

    @api.depends("lane_id.antenna_sequence_ids.sequence", "lane_id.antenna_sequence_ids.duration_from_previous")
    def _compute_cumulative_time(self):
        for record in self:
            total = 0.0
            for line in record.lane_id.antenna_sequence_ids.sorted(lambda item: (item.sequence or 0, item.id)):
                total += float(line.duration_from_previous or 0.0)
                if line.id == record.id:
                    record.cumulative_time = total
                    break
            else:
                record.cumulative_time = total

    @api.model_create_multi
    def create(self, vals_list):
        lane_ids = {
            int(values.get("lane_id") or 0)
            for values in vals_list
            if int(values.get("lane_id") or 0) and not int(values.get("sequence") or 0)
        }
        max_sequence_by_lane = {lane_id: 0 for lane_id in lane_ids}
        if lane_ids:
            rows = self._read_group(
                [("lane_id", "in", sorted(lane_ids))],
                ["lane_id"],
                ["sequence:max"],
            )
            max_sequence_by_lane.update({lane.id: int(max_sequence or 0) for lane, max_sequence in rows})
        prepared = []
        for source in vals_list:
            values = dict(source)
            lane_id = int(values.get("lane_id") or 0)
            if lane_id and not int(values.get("sequence") or 0):
                max_sequence_by_lane[lane_id] = max_sequence_by_lane.get(lane_id, 0) + 1
                values["sequence"] = max_sequence_by_lane[lane_id]
            prepared.append(values)
        records = super().create(prepared)
        lanes = records.mapped("lane_id")
        if not self.env.context.get("skip_first_duration_normalization"):
            lanes._normalize_first_sequence_duration()
        if not self.env.context.get("skip_lane_reader_config_sync"):
            lanes._sync_reader_configs_from_sequence()
        return records

    def write(self, vals):
        lanes = self.mapped("lane_id")
        result = super().write(vals)
        affected_lanes = lanes | self.mapped("lane_id")
        if not self.env.context.get("skip_first_duration_normalization"):
            affected_lanes._normalize_first_sequence_duration()
        if not self.env.context.get("skip_lane_reader_config_sync"):
            affected_lanes._sync_reader_configs_from_sequence()
        return result

    def unlink(self):
        lanes = self.mapped("lane_id")
        result = super().unlink()
        if not self.env.context.get("skip_first_duration_normalization"):
            lanes._normalize_first_sequence_duration()
        if not self.env.context.get("skip_lane_reader_config_sync"):
            lanes._sync_reader_configs_from_sequence()
        return result

    @api.constrains(
        "sequence", "reader_id", "port_no", "duration_from_previous", "lane_id"
    )
    def _check_antenna_sequence_point(self):
        for record in self:
            if record.sequence <= 0:
                raise ValidationError(_("Antenna Sequence order must be greater than zero."))
            if record.port_no < 1 or record.port_no > 16:
                raise ValidationError(_("Antenna/Port must be between 1 and 16."))
            if not record.reader_id.active:
                raise ValidationError(_("Antenna Sequence Reader must be active."))
            duration = float(record.duration_from_previous or 0.0)
            if duration < 0.0:
                raise ValidationError(_("Antenna Sequence Max Duration cannot be negative."))
