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
        "lane_ids.timeline_line_ids.reader_id",
        "lane_ids.timeline_line_ids.port_no",
    )
    def _compute_topology(self):
        for record in self:
            lanes = record.lane_ids.filtered("active")
            record.edge_server_ids = lanes.mapped("edge_server_id")
            record.controller_ids = lanes.mapped("controller_id")
            record.reader_ids = lanes.mapped("timeline_line_ids.reader_id")

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
        "lane_ids.checkin_sequence_ids",
        "lane_ids.checkout_sequence_ids",
    )
    def _compute_configuration_health(self):
        for record in self:
            active_lanes = record.lane_ids.filtered("active")
            ready_lanes = active_lanes.filtered(
                lambda lane: lane.configuration_state == "ready"
            )
            incomplete_lanes = active_lanes - ready_lanes
            has_checkin = any(lane.checkin_sequence_ids for lane in active_lanes)
            has_checkout = any(lane.checkout_sequence_ids for lane in active_lanes)
            coverage_issues = []
            if active_lanes and not has_checkin:
                coverage_issues.append(_("Parking Layout requires at least one Check-in Lane"))
            if active_lanes and not has_checkout:
                coverage_issues.append(_("Parking Layout requires at least one Check-out Lane"))

            record.ready_lane_count = len(ready_lanes)
            record.incomplete_lane_count = len(incomplete_lanes)
            if not active_lanes:
                record.configuration_state = "empty"
                record.configuration_summary = _("Add at least one active Lane.")
            elif incomplete_lanes or coverage_issues:
                record.configuration_state = "incomplete"
                summary_parts = []
                if incomplete_lanes:
                    summary_parts.append(
                        _("%(ready)s ready · %(incomplete)s need attention") % {
                            "ready": len(ready_lanes),
                            "incomplete": len(incomplete_lanes),
                        }
                    )
                summary_parts.extend(coverage_issues)
                record.configuration_summary = " · ".join(summary_parts)
            else:
                record.configuration_state = "ready"
                record.configuration_summary = _(
                    "All %(count)s active Lanes are ready · Check-in and Check-out are covered."
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
        area = self.sudo().browse(parking_area_id).exists()
        if not area:
            return {"found": False}
        transactions = self.env["nsp.parking.transaction"].sudo().search(
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
            timeline = []
            for line in lane.timeline_line_ids.sorted(lambda row: (row.sequence or 0, row.id)):
                reader = line.reader_id
                reader_payload = readers.setdefault(reader.id, {
                    "technical_code": reader.device_code or "",
                    "serial_number": reader.serial_number or "",
                    "reader_name": reader.name or reader.serial_number or "",
                    "physical_connection": reader.connection_type or False,
                    "reader_parameters": {
                        "power_dbm": int(reader.power_dbm or 0),
                        "read_interval_ms": int(reader.read_interval_ms or 200),
                        "tid_start_address": int(reader.tid_addr or 0),
                        "tid_length": int(reader.tid_len or 0),
                    },
                    "ports": set(),
                })
                reader_payload["ports"].add(int(line.port_no or 0))
                timeline.append({
                    "sequence": int(line.sequence or 0),
                    "reader_code": reader.device_code or "",
                    "reader_serial_number": reader.serial_number or "",
                    "port_no": int(line.port_no or 0),
                    "duration_from_previous_seconds": float(line.duration_from_previous or 0.0),
                    "cumulative_time_seconds": float(line.cumulative_time or 0.0),
                })
            payloads.append({
                "lane_code": lane.code,
                "lane_name": lane.name,
                "server_code": lane.edge_server_id.edge_server_code or "",
                "controller_code": lane.controller_id.controller_id or "",
                "reader_port_timeline": timeline,
                "event_sequences": {
                    "check_in": [
                        {
                            "reader_code": row.reader_id.device_code or "",
                            "port_no": int(row.port_no or 0),
                        }
                        for row in lane.checkin_sequence_ids.sorted(lambda item: (item.sequence or 0, item.id))
                    ],
                    "check_out": [
                        {
                            "reader_code": row.reader_id.device_code or "",
                            "port_no": int(row.port_no or 0),
                        }
                        for row in lane.checkout_sequence_ids.sorted(lambda item: (item.sequence or 0, item.id))
                    ],
                },
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
            "reader_port_timeline", "event_sequences", "timing_tolerance", "readers",
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
        return True

    def _operational_issues(self):
        self.ensure_one()
        issues = []
        lanes = self.lane_ids.filtered("active")
        if not lanes:
            return [_('Configure at least one active Parking Lane.')]
        lane_by_reader_port = {}
        layout_has_checkin = False
        layout_has_checkout = False
        for lane in lanes:
            has_checkin = bool(lane.checkin_sequence_ids)
            has_checkout = bool(lane.checkout_sequence_ids)
            layout_has_checkin = layout_has_checkin or has_checkin
            layout_has_checkout = layout_has_checkout or has_checkout
            try:
                lane._validate_lane_assembly()
                lane._validate_timeline_and_sequences()
            except ValidationError as exc:
                issues.append(str(exc))
                continue
            for line in lane.timeline_line_ids:
                key = (line.reader_id.id, int(line.port_no or 0))
                previous_lane = lane_by_reader_port.get(key)
                if previous_lane:
                    issues.append(
                        _("Reader Port %(reader)s:%(port)s is assigned to both %(first)s and %(second)s.")
                        % {
                            "reader": line.reader_id.device_code or line.reader_id.serial_number,
                            "port": line.port_no,
                            "first": previous_lane.display_name,
                            "second": lane.display_name,
                        }
                    )
                else:
                    lane_by_reader_port[key] = lane
            if len(lane.timeline_line_ids) < 2:
                issues.append(
                    _("Lane %(lane)s requires at least two Reader Port Timeline points.")
                    % {"lane": lane.display_name}
                )
            if not has_checkin and not has_checkout:
                issues.append(
                    _("Lane %(lane)s must define at least one Check-in or Check-out Sequence.")
                    % {"lane": lane.display_name}
                )
        if not layout_has_checkin:
            issues.append(_("Parking Layout must contain at least one Check-in Sequence."))
        if not layout_has_checkout:
            issues.append(_("Parking Layout must contain at least one Check-out Sequence."))
        return issues

    def _publish(self, target_state):
        for record in self:
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
                    for point in lane_payload.get("reader_port_timeline", [])
                }
                other_layouts = self.search([
                    ("id", "!=", record.id),
                    ("published_payload_json", "!=", False),
                ])
                conflicts = []
                for other in other_layouts:
                    try:
                        other_payload = json.loads(other.published_payload_json)
                    except Exception as exc:
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
                        for point in lane_payload.get("reader_port_timeline", [])
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
                # Maintenance/Blocked is an operational control action. It must
                # update the immutable snapshot already running on Edge, never
                # publish a potentially incomplete Cloud draft under revision.
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
            affected_edges = self.env["nsp.edge.server"].sudo().with_context(active_test=False).search([
                ("edge_server_code", "in", sorted(affected_codes)),
            ]) if affected_codes else self.env["nsp.edge.server"]
            affected_edges.bump_config_revision()
        return True

    def action_set_operational(self):
        return self._publish("operational")

    def action_set_maintenance(self):
        return self._publish("maintenance")

    def action_set_blocked(self):
        return self._publish("blocked")

    def action_reset_to_draft(self):
        # Keep the last immutable published payload active on Edge while Cloud is edited.
        self.write({"state": "draft"})
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
    edge_server_id = fields.Many2one(
        "nsp.edge.server", string="Server", required=True,
        ondelete="restrict", index=True,
    )
    controller_id = fields.Many2one(
        "nsp.controller", string="Controller", required=True,
        ondelete="restrict", index=True,
    )
    available_edge_server_ids = fields.Many2many(
        "nsp.edge.server", compute="_compute_available_devices", readonly=True,
    )
    available_controller_ids = fields.Many2many(
        "nsp.controller", compute="_compute_available_devices", readonly=True,
    )
    active = fields.Boolean(default=True, index=True)
    timeline_point_count = fields.Integer(string="Timeline Points", compute="_compute_timeline_point_count")
    timeline_line_ids = fields.One2many(
        "nsp.parking.lane.timeline", "lane_id", string="Reader Port Timeline",
    )
    checkin_sequence_ids = fields.One2many(
        "nsp.parking.lane.event.sequence", "lane_id",
        domain=[("sequence_type", "=", "check_in")], string="Check-in Sequence",
    )
    checkout_sequence_ids = fields.One2many(
        "nsp.parking.lane.event.sequence", "lane_id",
        domain=[("sequence_type", "=", "check_out")], string="Check-out Sequence",
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
    checkin_point_count = fields.Integer(
        string="Check-in Points", compute="_compute_sequence_counts",
    )
    checkout_point_count = fields.Integer(
        string="Check-out Points", compute="_compute_sequence_counts",
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
        if not parking_area_id or not edge_server_id or not controller_id:
            raise UserError(_(
                "Select a Parking Layout first. Server and Controller are taken from the selected Detection Timeline."
            ))
        record = self.create({
            "name": lane_name,
            "parking_area_id": parking_area_id,
            "edge_server_id": edge_server_id,
            "controller_id": controller_id,
        })
        return record.id, record.display_name

    @api.depends("name", "parking_area_id.name")
    def _compute_display_name(self):
        for record in self:
            record.display_name = "%s / %s" % (
                record.parking_area_id.name or _("Parking"),
                record.name or _("Lane"),
            )


    @api.depends("timeline_line_ids.duration_from_previous")
    def _compute_total_path_duration(self):
        for record in self:
            record.total_path_duration = sum(record.timeline_line_ids.sorted(lambda l: (l.sequence or 0, l.id)).mapped("duration_from_previous"))

    @api.depends("timeline_line_ids")
    def _compute_timeline_point_count(self):
        for record in self:
            record.timeline_point_count = len(record.timeline_line_ids)

    @api.depends("checkin_sequence_ids", "checkout_sequence_ids")
    def _compute_sequence_counts(self):
        for record in self:
            record.checkin_point_count = len(record.checkin_sequence_ids)
            record.checkout_point_count = len(record.checkout_sequence_ids)

    @api.depends(
        "active",
        "edge_server_id",
        "controller_id",
        "timeline_line_ids",
        "timeline_line_ids.sequence",
        "timeline_line_ids.reader_id",
        "timeline_line_ids.port_no",
        "timeline_line_ids.duration_from_previous",
        "checkin_sequence_ids",
        "checkin_sequence_ids.sequence",
        "checkin_sequence_ids.reader_id",
        "checkin_sequence_ids.port_no",
        "checkout_sequence_ids",
        "checkout_sequence_ids.sequence",
        "checkout_sequence_ids.reader_id",
        "checkout_sequence_ids.port_no",
    )
    def _compute_configuration_health(self):
        for lane in self:
            if not lane.active:
                lane.configuration_state = "disabled"
                lane.configuration_issue = _("Lane is disabled.")
                continue
            issues = []
            if not lane.edge_server_id:
                issues.append(_("Server is missing"))
            if not lane.controller_id:
                issues.append(_("Controller is missing"))
            timeline = lane.timeline_line_ids.sorted(
                lambda row: (row.sequence or 0, row.id)
            )
            if len(timeline) < 2:
                issues.append(_("Timeline needs at least 2 points"))
            elif timeline.mapped("sequence") != list(range(1, len(timeline) + 1)):
                issues.append(_("Timeline order is not contiguous"))
            timeline_keys = [
                (line.reader_id.id, int(line.port_no or 0)) for line in timeline
            ]
            if len(timeline_keys) != len(set(timeline_keys)):
                issues.append(_("Timeline contains duplicate Reader Ports"))
            has_checkin = bool(lane.checkin_sequence_ids)
            has_checkout = bool(lane.checkout_sequence_ids)
            if not has_checkin and not has_checkout:
                issues.append(_("At least one Check-in or Check-out Sequence is required"))
            lane.configuration_state = "incomplete" if issues else "ready"
            if issues:
                lane.configuration_issue = "; ".join(issues)
            elif has_checkin and has_checkout:
                lane.configuration_issue = _("Bidirectional Lane: Check-in and Check-out are configured.")
            elif has_checkin:
                lane.configuration_issue = _("Check-in Lane is configured.")
            else:
                lane.configuration_issue = _("Check-out Lane is configured.")

    @api.model
    def _active_whitelisted(self, model_name, type_code):
        return self.env[model_name].search([
            ("active", "=", True),
            ("whitelist_id", "!=", False),
            ("whitelist_id.active", "=", True),
            ("whitelist_id.device_type_code", "=", type_code),
        ])

    @api.depends("edge_server_id", "controller_id")
    def _compute_available_devices(self):
        Edge = self.env["nsp.edge.server"]
        Controller = self.env["nsp.controller"]
        edges = self._active_whitelisted("nsp.edge.server", "SERVER")
        controllers = self._active_whitelisted("nsp.controller", "CONTROLLER")
        for record in self:
            record.available_edge_server_ids = edges if edges else Edge.browse()
            record.available_controller_ids = controllers if controllers else Controller.browse()

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

    @api.constrains("edge_server_id", "controller_id", "active")
    def _check_lane_assembly(self):
        self._validate_lane_assembly()

    def _validate_timeline_and_sequences(self):
        for lane in self:
            timeline = lane.timeline_line_ids.sorted(lambda row: (row.sequence or 0, row.id))
            if timeline and len(timeline) < 2:
                raise ValidationError(_("Reader Port Timeline requires at least two detection points."))
            if timeline and timeline.mapped("sequence") != list(range(1, len(timeline) + 1)):
                raise ValidationError(_("Timeline Order must be contiguous and start at 1."))
            timeline_keys = [(line.reader_id.id, int(line.port_no or 0)) for line in timeline]
            if len(timeline_keys) != len(set(timeline_keys)):
                raise ValidationError(_("A Reader Port can appear only once in the Lane Timeline."))
            for index, line in enumerate(timeline):
                # Lane ownership is defined by lane.controller_id.  The Reader's
                # inventory controller is an independently synchronized runtime
                # relation and is not authoritative for a saved Lane topology.
                # The Apply Configuration wizard validates the selected Reader
                # and Port against the Calibration Infrastructure Scope before
                # persisting this independent Lane snapshot.
                if not line.reader_id.active:
                    raise ValidationError(_("Every Timeline Reader must be active."))
                if (
                    not line.reader_id.whitelist_id
                    or not line.reader_id.whitelist_id.active
                    or line.reader_id.whitelist_id.device_type_code != "RFID_READER"
                ):
                    raise ValidationError(_("Every Timeline Reader must be an active RFID Reader from Device Whitelist."))
                if int(line.port_no or 0) < 1 or int(line.port_no or 0) > 16:
                    raise ValidationError(_("Timeline Reader Port must be an integer from 1 to 16."))
                if index == 0 and float(line.duration_from_previous or 0.0) != 0.0:
                    raise ValidationError(_("The first Timeline point must have zero Duration from previous."))
                if index > 0 and float(line.duration_from_previous or 0.0) <= 0.0:
                    raise ValidationError(_("Every Timeline point after the first requires a positive Duration."))

            timeline_position = {key: position for position, key in enumerate(timeline_keys, start=1)}
            orientation_by_type = {}
            for sequence_type, rows, label in (
                ("check_in", lane.checkin_sequence_ids, _("Check-in")),
                ("check_out", lane.checkout_sequence_ids, _("Check-out")),
            ):
                ordered = rows.sorted(lambda row: (row.sequence or 0, row.id))
                if ordered and len(ordered) < 2:
                    raise ValidationError(_("%(label)s Sequence requires at least two Reader Ports.") % {"label": label})
                if ordered and ordered.mapped("sequence") != list(range(1, len(ordered) + 1)):
                    raise ValidationError(_("%(label)s Sequence Order must be contiguous and start at 1.") % {"label": label})
                sequence_keys = [(line.reader_id.id, int(line.port_no or 0)) for line in ordered]
                if len(sequence_keys) != len(set(sequence_keys)):
                    raise ValidationError(_("A Reader Port can appear only once in the %(label)s Sequence.") % {"label": label})
                outside = [key for key in sequence_keys if key not in timeline_position]
                if outside:
                    raise ValidationError(_("%(label)s Sequence can use only Reader Ports from the Lane Timeline.") % {"label": label})
                positions = [timeline_position[key] for key in sequence_keys]
                if any(abs(current - previous) != 1 for previous, current in zip(positions, positions[1:])):
                    raise ValidationError(
                        _("%(label)s Sequence must follow adjacent points in the Reader Port Timeline.")
                        % {"label": label}
                    )
                if len(positions) >= 2:
                    orientation_by_type[sequence_type] = 1 if positions[1] > positions[0] else -1
            if (
                orientation_by_type.get("check_in")
                and orientation_by_type.get("check_out")
                and orientation_by_type["check_in"] == orientation_by_type["check_out"]
            ):
                raise ValidationError(
                    _("Check-in and Check-out Sequences must follow opposite Timeline directions.")
                )
        return True

    @api.constrains(
        "timeline_line_ids", "timeline_line_ids.sequence", "timeline_line_ids.reader_id", "timeline_line_ids.port_no",
        "checkin_sequence_ids", "checkin_sequence_ids.sequence", "checkin_sequence_ids.reader_id", "checkin_sequence_ids.port_no",
        "checkout_sequence_ids", "checkout_sequence_ids.sequence", "checkout_sequence_ids.reader_id", "checkout_sequence_ids.port_no",
    )
    def _check_timeline_and_sequences(self):
        self._validate_timeline_and_sequences()



class NspParkingLaneTimeline(models.Model):
    _name = "nsp.parking.lane.timeline"
    _description = "NSP Parking Lane Reader Port Timeline"
    _order = "lane_id, sequence, id"

    lane_id = fields.Many2one("nsp.parking.lane", required=True, ondelete="cascade", index=True)
    sequence = fields.Integer(string="Order", required=True)
    reader_id = fields.Many2one("nsp.device", string="Reader", required=True, ondelete="restrict")
    port_no = fields.Integer(string="Port", required=True)
    duration_from_previous = fields.Float(string="Duration from previous (s)", required=True, digits=(8, 3), default=0.0)
    cumulative_time = fields.Float(string="Cumulative Time (s)", compute="_compute_cumulative_time", digits=(8, 3))
    available_reader_ids = fields.Many2many("nsp.device", compute="_compute_available_readers")

    _sql_constraints = [
        ("lane_timeline_order_unique", "unique(lane_id, sequence)", "Timeline Order must be unique per Lane."),
        ("lane_timeline_reader_port_unique", "unique(lane_id, reader_id, port_no)", "A Reader Port can appear only once per Lane Timeline."),
        ("lane_timeline_sequence_positive", "CHECK(sequence > 0)", "Timeline Order must be greater than zero."),
        ("lane_timeline_port_range", "CHECK(port_no >= 1 AND port_no <= 16)", "Timeline Reader Port must be between 1 and 16."),
        ("lane_timeline_duration_nonnegative", "CHECK(duration_from_previous >= 0)", "Timeline Duration cannot be negative."),
    ]

    @api.depends("lane_id.timeline_line_ids.sequence", "lane_id.timeline_line_ids.duration_from_previous")
    def _compute_cumulative_time(self):
        for record in self:
            total = 0.0
            for line in record.lane_id.timeline_line_ids.sorted(lambda item: (item.sequence or 0, item.id)):
                total += float(line.duration_from_previous or 0.0)
                if line.id == record.id:
                    record.cumulative_time = total
                    break
            else:
                record.cumulative_time = total

    @api.depends("lane_id.controller_id")
    def _compute_available_readers(self):
        Reader = self.env["nsp.device"]
        for record in self:
            domain = [
                ("active", "=", True),
                ("whitelist_id.active", "=", True),
                ("whitelist_id.device_type_code", "=", "RFID_READER"),
            ]
            # Do not filter by nsp.device.controller_id.  A Lane owns its
            # Controller explicitly and the Reader inventory relation may be
            # empty or updated independently by runtime synchronization.
            record.available_reader_ids = Reader.search(domain)

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        next_sequence_by_lane = {}
        for source in vals_list:
            values = dict(source)
            lane_id = int(values.get("lane_id") or 0)
            if lane_id and not int(values.get("sequence") or 0):
                if lane_id not in next_sequence_by_lane:
                    last = self.search(
                        [("lane_id", "=", lane_id)],
                        order="sequence desc, id desc",
                        limit=1,
                    )
                    next_sequence_by_lane[lane_id] = int(last.sequence or 0) + 1
                values["sequence"] = next_sequence_by_lane[lane_id]
                next_sequence_by_lane[lane_id] += 1
            prepared.append(values)
        return super().create(prepared)

    @api.constrains("sequence", "reader_id", "port_no", "lane_id")
    def _check_timeline_point(self):
        for record in self:
            if record.sequence <= 0:
                raise ValidationError(_("Timeline order must be greater than zero."))
            if record.port_no < 1 or record.port_no > 16:
                raise ValidationError(_("Reader Port must be between 1 and 16."))
            if not record.reader_id.active:
                raise ValidationError(_("Timeline Reader must be active."))
            if (
                not record.reader_id.whitelist_id
                or not record.reader_id.whitelist_id.active
                or record.reader_id.whitelist_id.device_type_code != "RFID_READER"
            ):
                raise ValidationError(_("Timeline Reader must be an active RFID Reader from Device Whitelist."))


class NspParkingLaneEventSequence(models.Model):
    _name = "nsp.parking.lane.event.sequence"
    _description = "NSP Parking Lane Event Sequence"
    _order = "lane_id, sequence_type, sequence, id"

    lane_id = fields.Many2one("nsp.parking.lane", required=True, ondelete="cascade", index=True)
    sequence_type = fields.Selection([("check_in", "Check-in"), ("check_out", "Check-out")], required=True, index=True, default="check_in")
    sequence = fields.Integer(string="Order", required=True)
    reader_id = fields.Many2one("nsp.device", string="Reader", required=True, ondelete="restrict")
    port_no = fields.Integer(string="Port", required=True)
    available_reader_ids = fields.Many2many("nsp.device", compute="_compute_available_readers")

    _sql_constraints = [
        ("lane_event_sequence_order_unique", "unique(lane_id, sequence_type, sequence)", "Event Sequence Order must be unique."),
        ("lane_event_sequence_reader_port_unique", "unique(lane_id, sequence_type, reader_id, port_no)", "A Reader Port can appear only once per Event Sequence."),
        ("lane_event_sequence_positive", "CHECK(sequence > 0)", "Event Sequence Order must be greater than zero."),
        ("lane_event_sequence_port_range", "CHECK(port_no >= 1 AND port_no <= 16)", "Event Sequence Reader Port must be between 1 and 16."),
    ]

    @api.depends("lane_id.timeline_line_ids.reader_id")
    def _compute_available_readers(self):
        Reader = self.env["nsp.device"]
        for record in self:
            readers = record.lane_id.timeline_line_ids.mapped("reader_id")
            record.available_reader_ids = readers or Reader.browse()

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        next_sequence_by_key = {}
        for source in vals_list:
            values = dict(source)
            lane_id = int(values.get("lane_id") or 0)
            sequence_type = values.get("sequence_type") or "check_in"
            if lane_id and not int(values.get("sequence") or 0):
                key = (lane_id, sequence_type)
                if key not in next_sequence_by_key:
                    last = self.search(
                        [("lane_id", "=", lane_id), ("sequence_type", "=", sequence_type)],
                        order="sequence desc, id desc",
                        limit=1,
                    )
                    next_sequence_by_key[key] = int(last.sequence or 0) + 1
                values["sequence"] = next_sequence_by_key[key]
                next_sequence_by_key[key] += 1
            prepared.append(values)
        return super().create(prepared)

    @api.constrains("sequence", "reader_id", "port_no", "lane_id")
    def _check_sequence_point(self):
        for record in self:
            if record.sequence <= 0:
                raise ValidationError(_("Sequence order must be greater than zero."))
            if record.port_no < 1 or record.port_no > 16:
                raise ValidationError(_("Reader Port must be between 1 and 16."))
            allowed = {
                (line.reader_id.id, int(line.port_no or 0))
                for line in record.lane_id.timeline_line_ids
            }
            if (record.reader_id.id, int(record.port_no or 0)) not in allowed:
                raise ValidationError(_("Event Sequence can use only Reader Ports from the Lane Timeline."))
