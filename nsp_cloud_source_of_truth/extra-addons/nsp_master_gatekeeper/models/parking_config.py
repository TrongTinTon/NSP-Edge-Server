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
                coverage_issues.append(_("Parking Layout requires at least one Lane In"))
            if active_lanes and not has_checkout:
                coverage_issues.append(_("Parking Layout requires at least one Lane Out"))

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
                    "All %(count)s active Lanes are ready · Lane In and Lane Out are covered."
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
            timeline = []
            config_by_reader = {
                config.reader_id.id: config for config in lane.reader_config_ids
            }
            for line in lane.timeline_line_ids.sorted(lambda row: (row.sequence or 0, row.id)):
                reader = line.reader_id
                config = config_by_reader.get(reader.id)
                if not config:
                    raise ValidationError(
                        _("Lane %(lane)s has no Applied Reader Configuration for %(reader)s.")
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
                            "duration_from_previous_seconds": float(row.duration_from_previous or 0.0),
                        }
                        for row in lane.checkin_sequence_ids.sorted(lambda item: (item.sequence or 0, item.id))
                    ],
                    "check_out": [
                        {
                            "reader_code": row.reader_id.device_code or "",
                            "port_no": int(row.port_no or 0),
                            "duration_from_previous_seconds": float(row.duration_from_previous or 0.0),
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
                lane._validate_reader_configs()
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
                    _("Lane %(lane)s must define at least one Lane In or Lane Out path.")
                    % {"lane": lane.display_name}
                )
        if not layout_has_checkin:
            issues.append(_("Parking Layout must contain at least one Lane In path."))
        if not layout_has_checkout:
            issues.append(_("Parking Layout must contain at least one Lane Out path."))
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
    setup_state = fields.Selection(
        [("draft", "Draft"), ("applied", "Applied")],
        string="Lane Setup State",
        required=True,
        default="applied",
        copy=True,
        index=True,
        help="Draft Lane Setup cannot be published to Edge until it is applied.",
    )
    setup_applied_at = fields.Datetime(
        string="Lane Setup Applied At", readonly=True, copy=False,
    )
    timeline_point_count = fields.Integer(string="Timeline Points", compute="_compute_timeline_point_count")
    timeline_line_ids = fields.One2many(
        "nsp.parking.lane.timeline", "lane_id", string="Reader Port Timeline",
    )
    reader_config_ids = fields.One2many(
        "nsp.parking.lane.reader.config", "lane_id", string="Applied Reader Configuration",
        copy=True,
    )
    reader_config_count = fields.Integer(
        string="Configured Readers", compute="_compute_reader_config_count",
    )
    checkin_sequence_ids = fields.One2many(
        "nsp.parking.lane.event.sequence", "lane_id",
        domain=[("sequence_type", "=", "check_in")], string="Lane In Path",
    )
    checkout_sequence_ids = fields.One2many(
        "nsp.parking.lane.event.sequence", "lane_id",
        domain=[("sequence_type", "=", "check_out")], string="Lane Out Path",
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
        string="Lane In Points", compute="_compute_sequence_counts",
    )
    checkout_point_count = fields.Integer(
        string="Lane Out Points", compute="_compute_sequence_counts",
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


    @api.depends(
        "checkin_sequence_ids.duration_from_previous",
        "checkout_sequence_ids.duration_from_previous",
    )
    def _compute_total_path_duration(self):
        for record in self:
            lane_in = sum(record.checkin_sequence_ids.mapped("duration_from_previous"))
            lane_out = sum(record.checkout_sequence_ids.mapped("duration_from_previous"))
            record.total_path_duration = max(lane_in, lane_out)

    @api.depends("timeline_line_ids")
    def _compute_timeline_point_count(self):
        for record in self:
            record.timeline_point_count = len(record.timeline_line_ids)

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

    def _sync_reader_configs_from_timeline(self):
        """Keep exactly one stable Reader configuration per Timeline Reader.

        Existing snapshots are preserved so later changes on nsp.device do not
        silently alter a working Lane. Lane Calibration replaces the snapshots
        atomically when Lane Setup is saved.
        """
        Config = self.env["nsp.parking.lane.reader.config"].sudo()
        for lane in self:
            readers = lane.timeline_line_ids.mapped("reader_id")
            existing_by_reader = {
                config.reader_id.id: config for config in lane.reader_config_ids
            }
            for reader in readers:
                if existing_by_reader.get(reader.id):
                    continue
                create_values = lane._default_reader_config_values(reader)
                create_values["lane_id"] = lane.id
                Config.create(create_values)
            stale = lane.reader_config_ids.filtered(
                lambda config: config.reader_id not in readers
            )
            if stale:
                stale.unlink()
        return True

    def _validate_reader_configs(self):
        for lane in self:
            timeline_readers = lane.timeline_line_ids.mapped("reader_id")
            config_by_reader = {
                config.reader_id.id: config for config in lane.reader_config_ids
            }
            missing = timeline_readers.filtered(
                lambda reader: reader.id not in config_by_reader
            )
            stale = lane.reader_config_ids.filtered(
                lambda config: config.reader_id not in timeline_readers
            )
            if missing:
                raise ValidationError(
                    _("Lane %(lane)s is missing Applied Reader Configuration for: %(readers)s")
                    % {
                        "lane": lane.display_name,
                        "readers": ", ".join(missing.mapped("display_name")),
                    }
                )
            if stale:
                raise ValidationError(
                    _("Lane %(lane)s contains Reader Configuration not used by its Timeline.")
                    % {"lane": lane.display_name}
                )
            lane.reader_config_ids._validate_parameter_ranges()
        return True

    @api.depends("checkin_sequence_ids", "checkout_sequence_ids")
    def _compute_sequence_counts(self):
        for record in self:
            record.checkin_point_count = len(record.checkin_sequence_ids)
            record.checkout_point_count = len(record.checkout_sequence_ids)

    @api.depends(
        "active",
        "setup_state",
        "edge_server_id",
        "controller_id",
        "timeline_line_ids",
        "timeline_line_ids.sequence",
        "timeline_line_ids.reader_id",
        "timeline_line_ids.port_no",
        "timeline_line_ids.duration_from_previous",
        "reader_config_ids",
        "reader_config_ids.reader_id",
        "reader_config_ids.power_dbm",
        "reader_config_ids.read_interval_ms",
        "reader_config_ids.tid_start_address",
        "reader_config_ids.tid_length",
        "checkin_sequence_ids",
        "checkin_sequence_ids.sequence",
        "checkin_sequence_ids.reader_id",
        "checkin_sequence_ids.port_no",
        "checkin_sequence_ids.duration_from_previous",
        "checkout_sequence_ids",
        "checkout_sequence_ids.sequence",
        "checkout_sequence_ids.reader_id",
        "checkout_sequence_ids.port_no",
        "checkout_sequence_ids.duration_from_previous",
    )
    def _compute_configuration_health(self):
        for lane in self:
            if not lane.active:
                lane.configuration_state = "disabled"
                lane.configuration_issue = _("Lane is disabled.")
                continue
            issues = []
            if lane.setup_state == "draft":
                issues.append(_("Lane Setup is saved as Draft; apply it before publishing"))
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
            timeline_reader_ids = set(timeline.mapped("reader_id").ids)
            configured_reader_ids = set(lane.reader_config_ids.mapped("reader_id").ids)
            if timeline_reader_ids != configured_reader_ids:
                issues.append(_("Applied Reader Configuration does not match Timeline Readers"))
            has_checkin = bool(lane.checkin_sequence_ids)
            has_checkout = bool(lane.checkout_sequence_ids)
            if not has_checkin and not has_checkout:
                issues.append(_("At least one Lane In or Lane Out path is required"))
            lane.configuration_state = "incomplete" if issues else "ready"
            if issues:
                lane.configuration_issue = "; ".join(issues)
            elif has_checkin and has_checkout:
                lane.configuration_issue = _("Bidirectional Lane: Lane In and Lane Out are configured.")
            elif has_checkin:
                lane.configuration_issue = _("Lane In is configured.")
            else:
                lane.configuration_issue = _("Lane Out is configured.")

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
        """Validate Lane Setup against the calibrated hardware allowlist.

        The legacy ``timeline_line_ids`` model is retained in NSP 19.x only as
        the set of Reader/Ports made available by Lane Calibration. Lane In and
        Lane Out are independent paths: their order and duration do not need to
        be adjacent, reversed, or otherwise derived from the Calibration
        Detection Timeline.
        """
        for lane in self:
            allowed_rows = lane.timeline_line_ids.sorted(
                lambda row: (row.sequence or 0, row.id)
            )
            allowed_keys = [
                (line.reader_id.id, int(line.port_no or 0))
                for line in allowed_rows
            ]
            if allowed_rows and allowed_rows.mapped("sequence") != list(
                range(1, len(allowed_rows) + 1)
            ):
                raise ValidationError(_(
                    "Calibration Antenna Scope Order must be contiguous and start at 1."
                ))
            if len(allowed_keys) != len(set(allowed_keys)):
                raise ValidationError(_(
                    "A Reader Port can appear only once in the Calibration Antenna Scope."
                ))
            for line in allowed_rows:
                if not line.reader_id.active:
                    raise ValidationError(_(
                        "Every calibrated Reader used by Lane Setup must be active."
                    ))
                if (
                    not line.reader_id.whitelist_id
                    or not line.reader_id.whitelist_id.active
                    or line.reader_id.whitelist_id.device_type_code != "RFID_READER"
                ):
                    raise ValidationError(_(
                        "Every Lane Setup Reader must be an active RFID Reader from Device Whitelist."
                    ))
                if int(line.port_no or 0) < 1 or int(line.port_no or 0) > 16:
                    raise ValidationError(_(
                        "Lane Setup Antenna/Port must be an integer from 1 to 16."
                    ))

            allowed = set(allowed_keys)
            for rows, label in (
                (lane.checkin_sequence_ids, _("Lane In")),
                (lane.checkout_sequence_ids, _("Lane Out")),
            ):
                ordered = rows.sorted(lambda row: (row.sequence or 0, row.id))
                if not ordered:
                    continue
                if len(ordered) < 2:
                    raise ValidationError(
                        _("%(label)s requires at least two Antennas.")
                        % {"label": label}
                    )
                if ordered.mapped("sequence") != list(range(1, len(ordered) + 1)):
                    raise ValidationError(
                        _("%(label)s Order must be contiguous and start at 1.")
                        % {"label": label}
                    )
                sequence_keys = [
                    (line.reader_id.id, int(line.port_no or 0)) for line in ordered
                ]
                if len(sequence_keys) != len(set(sequence_keys)):
                    raise ValidationError(
                        _("An Antenna can appear only once in %(label)s.")
                        % {"label": label}
                    )
                outside = [key for key in sequence_keys if key not in allowed]
                if outside:
                    raise ValidationError(
                        _("%(label)s can use only Readers and Antennas from Lane Calibration.")
                        % {"label": label}
                    )
                if float(ordered[0].duration_from_previous or 0.0) != 0.0:
                    raise ValidationError(
                        _("The first Antenna in %(label)s must use 0 ms Max Duration.")
                        % {"label": label}
                    )
                if any(
                    float(line.duration_from_previous or 0.0) <= 0.0
                    for line in ordered[1:]
                ):
                    raise ValidationError(
                        _("Every Antenna after the first in %(label)s requires a positive Max Duration.")
                        % {"label": label}
                    )
        return True

    @api.constrains(
        "timeline_line_ids", "timeline_line_ids.sequence", "timeline_line_ids.reader_id", "timeline_line_ids.port_no",
        "checkin_sequence_ids", "checkin_sequence_ids.sequence", "checkin_sequence_ids.reader_id", "checkin_sequence_ids.port_no", "checkin_sequence_ids.duration_from_previous",
        "checkout_sequence_ids", "checkout_sequence_ids.sequence", "checkout_sequence_ids.reader_id", "checkout_sequence_ids.port_no", "checkout_sequence_ids.duration_from_previous",
    )
    def _check_timeline_and_sequences(self):
        self._validate_timeline_and_sequences()



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
        "lane_id.timeline_line_ids.reader_id",
        "lane_id.timeline_line_ids.port_no",
        "reader_id",
    )
    def _compute_port_summary(self):
        for config in self:
            ports = sorted({
                int(line.port_no or 0)
                for line in config.lane_id.timeline_line_ids
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
        readers = self.env["nsp.device"].search([
            ("active", "=", True),
            ("whitelist_id.active", "=", True),
            ("whitelist_id.device_type_code", "=", "RFID_READER"),
        ])
        # Reader inventory may be synchronized independently from Lane ownership.
        for record in self:
            record.available_reader_ids = readers

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
        if not self.env.context.get("skip_lane_reader_config_sync"):
            records.mapped("lane_id")._sync_reader_configs_from_timeline()
        return records

    def write(self, vals):
        lanes = self.mapped("lane_id")
        result = super().write(vals)
        if not self.env.context.get("skip_lane_reader_config_sync"):
            (lanes | self.mapped("lane_id"))._sync_reader_configs_from_timeline()
        return result

    def unlink(self):
        lanes = self.mapped("lane_id")
        result = super().unlink()
        if not self.env.context.get("skip_lane_reader_config_sync"):
            lanes._sync_reader_configs_from_timeline()
        return result

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
    sequence_type = fields.Selection([("check_in", "Lane In"), ("check_out", "Lane Out")], required=True, index=True, default="check_in")
    sequence = fields.Integer(string="Order", required=True)
    reader_id = fields.Many2one("nsp.device", string="Reader", required=True, ondelete="restrict")
    port_no = fields.Integer(string="Port", required=True)
    duration_from_previous = fields.Float(
        string="Max Duration from Previous (s)",
        required=True,
        digits=(8, 3),
        default=0.0,
        help="Direction-specific maximum time from the previous Antenna.",
    )
    available_reader_ids = fields.Many2many("nsp.device", compute="_compute_available_readers")

    _sql_constraints = [
        ("lane_event_sequence_order_unique", "unique(lane_id, sequence_type, sequence)", "Event Sequence Order must be unique."),
        ("lane_event_sequence_reader_port_unique", "unique(lane_id, sequence_type, reader_id, port_no)", "A Reader Port can appear only once per Event Sequence."),
        ("lane_event_sequence_positive", "CHECK(sequence > 0)", "Event Sequence Order must be greater than zero."),
        ("lane_event_sequence_port_range", "CHECK(port_no >= 1 AND port_no <= 16)", "Event Sequence Reader Port must be between 1 and 16."),
        ("lane_event_sequence_duration_nonnegative", "CHECK(duration_from_previous >= 0)", "Lane Direction Duration cannot be negative."),
    ]

    @api.depends("lane_id.timeline_line_ids.reader_id")
    def _compute_available_readers(self):
        Reader = self.env["nsp.device"]
        for record in self:
            readers = record.lane_id.timeline_line_ids.mapped("reader_id")
            record.available_reader_ids = readers or Reader.browse()

    @api.model_create_multi
    def create(self, vals_list):
        keys = {
            (int(values.get("lane_id") or 0), values.get("sequence_type") or "check_in")
            for values in vals_list
            if int(values.get("lane_id") or 0) and not int(values.get("sequence") or 0)
        }
        max_sequence_by_key = {key: 0 for key in keys}
        lane_ids = sorted({lane_id for lane_id, _sequence_type in keys})
        if lane_ids:
            rows = self._read_group(
                [("lane_id", "in", lane_ids)],
                ["lane_id", "sequence_type"],
                ["sequence:max"],
            )
            max_sequence_by_key.update({
                (lane.id, sequence_type): int(max_sequence or 0)
                for lane, sequence_type, max_sequence in rows
            })
        prepared = []
        for source in vals_list:
            values = dict(source)
            lane_id = int(values.get("lane_id") or 0)
            sequence_type = values.get("sequence_type") or "check_in"
            if lane_id and not int(values.get("sequence") or 0):
                key = (lane_id, sequence_type)
                max_sequence_by_key[key] = max_sequence_by_key.get(key, 0) + 1
                values["sequence"] = max_sequence_by_key[key]
            prepared.append(values)
        return super().create(prepared)

    @api.constrains("sequence", "reader_id", "port_no", "duration_from_previous", "lane_id")
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
                raise ValidationError(_(
                    "Lane Direction can use only Reader Ports from Lane Calibration."
                ))
            if record.sequence == 1 and float(record.duration_from_previous or 0.0) != 0.0:
                raise ValidationError(_("The first Lane Direction Antenna must use 0 ms Max Duration."))
            if record.sequence > 1 and float(record.duration_from_previous or 0.0) <= 0.0:
                raise ValidationError(_("Lane Direction Antennas after the first require a positive Max Duration."))
