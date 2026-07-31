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

    antenna_transition_ids = fields.Many2many(
        "nsp.parking.antenna.transition", string="Movement Rules",
        compute="_compute_topology",
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
    antenna_ids = fields.Many2many(
        "nsp.device.antenna", string="Antennas", compute="_compute_topology",
    )
    edge_server_count = fields.Integer(compute="_compute_counts")
    controller_count = fields.Integer(compute="_compute_counts")
    reader_count = fields.Integer(compute="_compute_counts")
    antenna_count = fields.Integer(compute="_compute_counts")
    lane_count = fields.Integer(compute="_compute_counts")
    whitelist_count = fields.Integer(compute="_compute_whitelist_count")

    _sql_constraints = [
        ("code_unique", "unique(code)", "Parking Area Code must be unique."),
    ]

    @api.depends("published_payload_json")
    def _compute_is_published(self):
        for record in self:
            record.is_published = bool(record.published_payload_json)

    @api.depends(
        "lane_ids.active", "lane_ids.edge_server_id", "lane_ids.controller_id",
        "lane_ids.timeline_line_ids.reader_id",
        "lane_ids.timeline_line_ids.antenna_id",
    )
    def _compute_topology(self):
        for record in self:
            lanes = record.lane_ids.filtered("active")
            transitions = lanes.mapped("antenna_transition_ids")
            readers = lanes.mapped("timeline_line_ids.reader_id")
            antennas = lanes.mapped("timeline_line_ids.antenna_id")
            record.antenna_transition_ids = transitions
            record.edge_server_ids = lanes.mapped("edge_server_id")
            record.controller_ids = lanes.mapped("controller_id")
            record.reader_ids = readers
            record.antenna_ids = antennas

    @api.model
    def _search_controllers(self, operator, value):
        return [("lane_ids.controller_id", operator, value)]

    @api.depends(
        "edge_server_ids", "controller_ids", "reader_ids", "antenna_ids",
        "lane_ids.active",
    )
    def _compute_counts(self):
        for record in self:
            record.edge_server_count = len(record.edge_server_ids)
            record.controller_count = len(record.controller_ids)
            record.reader_count = len(record.reader_ids)
            record.antenna_count = len(record.antenna_ids)
            record.lane_count = len(record.lane_ids.filtered("active"))

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
                ("lane_id.parking_area_id", "=", area.id),
                ("event_type", "=", "check_in"),
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
        result = []
        for lane in self.lane_ids.filtered("active").sorted(
            key=lambda item: ((item.name or "").casefold(), item.code or "", item.id)
        ):
            timeline = []
            readers = {}
            for line in lane.timeline_line_ids.sorted(lambda l: (l.sequence or 0, l.id)):
                if line.reader_id:
                    reader_payload = readers.setdefault(line.reader_id.id, {
                        "technical_code": line.reader_id.device_code or "",
                        "serial_number": line.reader_id.serial_number or "",
                        "reader_name": line.reader_id.name or line.reader_id.serial_number or "",
                        "physical_connection": line.reader_id.connection_type or False,
                        "reader_parameters": {
                            "power_dbm": int(line.reader_id.power_dbm or 0),
                            "read_interval_ms": int(line.reader_id.read_interval_ms or 200),
                            "tid_start_address": int(line.reader_id.tid_addr or 0),
                            "tid_length": int(line.reader_id.tid_len or 0),
                        },
                        "antennas": {},
                    })
                    reader_payload["antennas"][int(line.port_no or 0)] = {
                        "antenna_no": int(line.port_no or 0),
                        "technical_code": line.antenna_id.technical_code or "",
                        "serial_number": line.antenna_id.serial_number or False,
                        "name": line.antenna_id.whitelist_id.display_name or line.antenna_id.display_name or "",
                    }
                timeline.append({
                    "sequence": int(line.sequence or 0),
                    "antenna_code": line.antenna_id.technical_code or "",
                    "antenna_name": line.antenna_id.whitelist_id.display_name or line.antenna_id.display_name or "",
                    "reader_code": line.reader_id.device_code or "",
                    "reader_serial_number": line.reader_id.serial_number or "",
                    "port_no": int(line.port_no or 0),
                    "duration_from_previous": float(line.duration_from_previous or 0.0),
                    "cumulative_time": float(line.cumulative_time or 0.0),
                })
            result.append({
                "lane_code": lane.code,
                "lane_name": lane.name,
                "server_code": lane.edge_server_id.edge_server_code or "",
                "controller_code": lane.controller_id.controller_id or "",
                "direction": lane.direction or "entry",
                "timeline": timeline,
                "check_in_sequence": [item.antenna_id.technical_code or "" for item in lane.checkin_sequence_ids.sorted(lambda s: (s.sequence or 0, s.id))],
                "check_out_sequence": [item.antenna_id.technical_code or "" for item in lane.checkout_sequence_ids.sorted(lambda s: (s.sequence or 0, s.id))],
                "tolerance_type": lane.tolerance_type or "percent",
                "tolerance_value": float(lane.tolerance_value or 0.0),
                "readers": [
                    {**reader, "antennas": [reader["antennas"][port] for port in sorted(reader["antennas"])]}
                    for reader in sorted(readers.values(), key=lambda row: (row["serial_number"], row["technical_code"]))
                ],
            })
        return result

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

    def _operational_issues(self):
        self.ensure_one()
        issues = []
        lanes = self.lane_ids.filtered("active")
        if not lanes:
            return [_('Configure at least one active Parking Lane.')]
        for lane in lanes:
            try:
                lane._validate_lane_assembly()
            except ValidationError as exc:
                issues.append(str(exc))
                continue
            if not lane.timeline_line_ids:
                issues.append(
                    _("Lane %(lane)s must have at least one Antenna Timeline row.")
                    % {"lane": lane.display_name}
                )
                continue
            if lane.direction in ("entry", "bidirectional") and not lane.checkin_sequence_ids:
                issues.append(_("Lane %(lane)s must define a Check-in Sequence.") % {"lane": lane.display_name})
            if lane.direction in ("exit", "bidirectional") and not lane.checkout_sequence_ids:
                issues.append(_("Lane %(lane)s must define a Check-out Sequence.") % {"lane": lane.display_name})
            try:
                lane._validate_timeline_and_sequences()
            except ValidationError as exc:
                issues.append(str(exc))
        return issues

    def _publish(self, target_state):
        for record in self:
            issues = record._operational_issues()
            if issues:
                raise UserError("\n".join(issues))
            revision = int(record.published_revision or 0) + 1
            payload = record._build_sync_payload(target_state, revision)
            previous_edge_codes = {
                item.strip().upper()
                for item in str(record.published_edge_server_codes or "").split(",")
                if item.strip()
            }
            edge_codes = sorted({
                str(lane.edge_server_id.edge_server_code or "").strip().upper()
                for lane in record.lane_ids.filtered("active")
                if lane.edge_server_id and lane.edge_server_id.edge_server_code
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
    antenna_transition_ids = fields.One2many(
        "nsp.parking.antenna.transition", "lane_id", string="Movement Rules",
    )
    transition_count = fields.Integer(compute="_compute_transition_count")
    calibration_source_id = fields.Many2one(
        "nsp.measurement.session", string="Calibration Source",
        ondelete="set null",
    )
    calibration_result_id = fields.Many2one(
        "nsp.measurement.result", string="Calibration",
        ondelete="set null", domain=[("state", "=", "accepted")],
    )
    direction = fields.Selection(
        [("entry", "Entry"), ("exit", "Exit"), ("bidirectional", "Both")],
        string="Direction", default="entry", required=True,
    )
    timeline_line_ids = fields.One2many(
        "nsp.parking.lane.timeline", "lane_id", string="Antenna Timeline",
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
        string="Tolerance Type", default="percent",
    )
    tolerance_value = fields.Float(string="Tolerance Value", default=30.0)
    total_path_duration = fields.Float(
        string="Total Path Duration", compute="_compute_total_path_duration", digits=(8,3),
    )

    _sql_constraints = [
        (
            "parking_lane_code_unique", "unique(code)",
            "Parking Lane Code must be unique.",
        ),
    ]

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
    def _compute_transition_count(self):
        for record in self:
            record.transition_count = len(record.timeline_line_ids)

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
                raise ValidationError(_("Antenna Timeline requires at least two detection points."))
            timeline_antennas = timeline.mapped("antenna_id")
            if len(timeline_antennas) != len(set(timeline_antennas.ids)):
                raise ValidationError(_("An Antenna can appear only once in the Lane Timeline."))
            if len(timeline.mapped("sequence")) != len(set(timeline.mapped("sequence"))):
                raise ValidationError(_("Timeline Order must be unique within a Lane."))
            for sequence_type, rows, label in (
                ("check_in", lane.checkin_sequence_ids, _("Check-in")),
                ("check_out", lane.checkout_sequence_ids, _("Check-out")),
            ):
                ordered = rows.sorted(lambda row: (row.sequence or 0, row.id))
                if ordered and len(ordered) < 2:
                    raise ValidationError(_("%(label)s Sequence requires at least two Antennas.") % {"label": label})
                if len(ordered.mapped("antenna_id")) != len(set(ordered.mapped("antenna_id").ids)):
                    raise ValidationError(_("An Antenna can appear only once in the %(label)s Sequence.") % {"label": label})
                if len(ordered.mapped("sequence")) != len(set(ordered.mapped("sequence"))):
                    raise ValidationError(_("Order must be unique in the %(label)s Sequence.") % {"label": label})
                outside = ordered.mapped("antenna_id") - timeline_antennas
                if outside:
                    raise ValidationError(_("%(label)s Sequence can use only Antennas from the Lane Timeline.") % {"label": label})
        return True

    @api.constrains(
        "timeline_line_ids", "timeline_line_ids.sequence", "timeline_line_ids.antenna_id",
        "checkin_sequence_ids", "checkin_sequence_ids.sequence", "checkin_sequence_ids.antenna_id",
        "checkout_sequence_ids", "checkout_sequence_ids.sequence", "checkout_sequence_ids.antenna_id",
    )
    def _check_timeline_and_sequences(self):
        self._validate_timeline_and_sequences()

    def action_import_calibration_result(self):
        for lane in self:
            result = lane.calibration_result_id
            if not result or result.state != "accepted":
                raise ValidationError(_("Select an accepted Calibration first."))
            lane.calibration_source_id = result.session_id
            lane.timeline_line_ids.unlink()
            lane.checkin_sequence_ids.unlink()
            lane.checkout_sequence_ids.unlink()
            ordered_lines = result.line_ids.sorted("sequence")
            timeline_commands = []
            checkin_commands = []
            checkout_commands = []
            for line in ordered_lines:
                timeline_commands.append((0, 0, {
                    "sequence": line.sequence,
                    "antenna_id": line.antenna_id.id,
                    "reader_id": line.reader_id.id,
                    "port_no": line.port_no,
                    "duration_from_previous": line.duration_standard,
                }))
                checkin_commands.append((0, 0, {
                    "sequence_type": "check_in",
                    "sequence": line.sequence,
                    "antenna_id": line.antenna_id.id,
                }))
            for sequence, line in enumerate(reversed(ordered_lines), start=1):
                checkout_commands.append((0, 0, {
                    "sequence_type": "check_out",
                    "sequence": sequence,
                    "antenna_id": line.antenna_id.id,
                }))
            lane.write({
                "timeline_line_ids": timeline_commands,
                "checkin_sequence_ids": checkin_commands,
                "checkout_sequence_ids": checkout_commands,
                "tolerance_type": "percent",
                "tolerance_value": result.tolerance_percent,
            })
        return True


class NspParkingLaneTimeline(models.Model):
    _name = "nsp.parking.lane.timeline"
    _description = "NSP Parking Lane Antenna Timeline"
    _order = "lane_id, sequence, id"

    lane_id = fields.Many2one("nsp.parking.lane", required=True, ondelete="cascade", index=True)
    sequence = fields.Integer(string="Order", required=True, default=1)
    antenna_id = fields.Many2one("nsp.device.antenna", string="Antenna / Detection Point", required=True, ondelete="restrict")
    reader_id = fields.Many2one("nsp.device", string="Reader", ondelete="restrict")
    port_no = fields.Integer(string="Port / Channel", default=1)
    duration_from_previous = fields.Float(string="Duration from previous (s)", digits=(8,3), default=0.0)
    cumulative_time = fields.Float(string="Cumulative Time (s)", compute="_compute_cumulative_time", digits=(8,3))
    available_reader_ids = fields.Many2many("nsp.device", compute="_compute_available_devices")
    available_antenna_ids = fields.Many2many("nsp.device.antenna", compute="_compute_available_devices")

    @api.depends("lane_id.timeline_line_ids.sequence", "lane_id.timeline_line_ids.duration_from_previous")
    def _compute_cumulative_time(self):
        for record in self:
            total = 0.0
            for line in record.lane_id.timeline_line_ids.sorted(lambda l: (l.sequence or 0, l.id)):
                total += float(line.duration_from_previous or 0.0)
                if line.id == record.id:
                    record.cumulative_time = total
                    break
            else:
                record.cumulative_time = total

    @api.depends("reader_id")
    def _compute_available_devices(self):
        Reader = self.env["nsp.device"]
        Antenna = self.env["nsp.device.antenna"]
        readers = Reader.search([("active","=",True),("whitelist_id.device_type_code","=","RFID_READER")])
        antennas = Antenna.search([("active","=",True),("whitelist_id.device_type_code","=","ANTENNA")])
        for record in self:
            record.available_reader_ids = readers
            record.available_antenna_ids = antennas

    @api.model_create_multi
    def create(self, vals_list):
        res = super().create(vals_list)
        for rec in res:
            if not rec.sequence:
                siblings = rec.lane_id.timeline_line_ids.filtered(lambda l: l.id != rec.id)
                rec.sequence = (max(siblings.mapped("sequence")) if siblings else 0) + 1
        return res

    @api.constrains("sequence", "port_no")
    def _check_positive(self):
        for rec in self:
            if rec.sequence <= 0:
                raise ValidationError(_("Timeline order must be greater than zero."))
            if rec.port_no <= 0:
                raise ValidationError(_("Port / Channel must be greater than zero."))


class NspParkingLaneEventSequence(models.Model):
    _name = "nsp.parking.lane.event.sequence"
    _description = "NSP Parking Lane Event Sequence"
    _order = "lane_id, sequence_type, sequence, id"

    lane_id = fields.Many2one("nsp.parking.lane", required=True, ondelete="cascade", index=True)
    sequence_type = fields.Selection([("check_in", "Check-in"), ("check_out", "Check-out")], required=True, index=True, default="check_in")
    sequence = fields.Integer(string="Order", required=True, default=1)
    antenna_id = fields.Many2one("nsp.device.antenna", string="Antenna", required=True, ondelete="restrict")
    available_antenna_ids = fields.Many2many("nsp.device.antenna", compute="_compute_available_antennas")

    @api.depends("lane_id.timeline_line_ids.antenna_id")
    def _compute_available_antennas(self):
        Antenna = self.env["nsp.device.antenna"]
        for record in self:
            allowed = record.lane_id.timeline_line_ids.mapped("antenna_id")
            record.available_antenna_ids = allowed or Antenna.browse()

    @api.model_create_multi
    def create(self, vals_list):
        res = super().create(vals_list)
        for rec in res:
            if not rec.sequence:
                siblings = rec.lane_id.checkin_sequence_ids if rec.sequence_type == "check_in" else rec.lane_id.checkout_sequence_ids
                siblings = siblings.filtered(lambda l: l.id != rec.id)
                rec.sequence = (max(siblings.mapped("sequence")) if siblings else 0) + 1
        return res

    @api.constrains("sequence")
    def _check_sequence(self):
        for rec in self:
            if rec.sequence <= 0:
                raise ValidationError(_("Sequence order must be greater than zero."))


class NspParkingAntennaTransition(models.Model):
    """Directed contextual Reader-port path used by Edge business logic."""

    _name = "nsp.parking.antenna.transition"
    _description = "NSP Parking Movement Rule"
    _order = "lane_id, event_type, from_device_id, from_antenna_no, id"
    _rec_name = "rule_name"

    rule_name = fields.Char(compute="_compute_rule_name")
    lane_id = fields.Many2one(
        "nsp.parking.lane", string="Parking Lane", required=True,
        ondelete="cascade", index=True,
    )
    parking_area_id = fields.Many2one(
        "nsp.parking.area", related="lane_id.parking_area_id", readonly=True,
    )
    event_type = fields.Selection(
        [("check_in", "Check-in"), ("check_out", "Check-out")],
        string="Event Type", required=True, index=True,
    )
    from_device_id = fields.Many2one(
        "nsp.device", string="From Reader", required=True,
        ondelete="restrict", index=True,
    )
    from_antenna_no = fields.Integer(string="From Port", required=True, default=1)
    from_antenna_id = fields.Many2one(
        "nsp.device.antenna", string="From Antenna", required=True,
        ondelete="restrict", index=True,
    )
    to_device_id = fields.Many2one(
        "nsp.device", string="To Reader", required=True,
        ondelete="restrict", index=True,
    )
    to_antenna_no = fields.Integer(string="To Port", required=True, default=2)
    to_antenna_id = fields.Many2one(
        "nsp.device.antenna", string="To Antenna", required=True,
        ondelete="restrict", index=True,
    )
    duration_seconds = fields.Float(
        string="Duration (Seconds)", required=True, default=2.0, digits=(8, 3),
    )
    from_serial_number = fields.Char(related="from_device_id.serial_number", readonly=True)
    to_serial_number = fields.Char(related="to_device_id.serial_number", readonly=True)
    available_reader_ids = fields.Many2many(
        "nsp.device", compute="_compute_available_devices", readonly=True,
    )
    available_antenna_ids = fields.Many2many(
        "nsp.device.antenna", compute="_compute_available_devices", readonly=True,
    )

    _sql_constraints = [
        (
            "unique_directed_transition",
            "unique(lane_id, from_device_id, from_antenna_no, to_device_id, to_antenna_no)",
            "The same directed Movement Rule can be configured only once per Lane.",
        ),
        (
            "transition_duration_positive", "CHECK(duration_seconds > 0)",
            "Movement Rule Duration must be greater than zero.",
        ),
        (
            "transition_ports_positive", "CHECK(from_antenna_no > 0 AND to_antenna_no > 0)",
            "Reader port numbers must be greater than zero.",
        ),
    ]

    @api.depends(
        "lane_id.display_name", "event_type",
        "from_device_id.display_name", "from_antenna_no", "from_antenna_id.display_name",
        "to_device_id.display_name", "to_antenna_no", "to_antenna_id.display_name",
        "duration_seconds",
    )
    def _compute_rule_name(self):
        labels = dict(self._fields["event_type"].selection)
        for record in self:
            record.rule_name = "%s / %s: %s [No.%s · %s] → %s [No.%s · %s] / %.3gs" % (
                record.lane_id.display_name or _("Lane"),
                labels.get(record.event_type, record.event_type or ""),
                record.from_device_id.display_name or _("Reader"),
                record.from_antenna_no or "-",
                record.from_antenna_id.whitelist_id.technical_code or _("Antenna"),
                record.to_device_id.display_name or _("Reader"),
                record.to_antenna_no or "-",
                record.to_antenna_id.whitelist_id.technical_code or _("Antenna"),
                record.duration_seconds or 0.0,
            )

    @api.depends("lane_id", "from_device_id", "to_device_id")
    def _compute_available_devices(self):
        Reader = self.env["nsp.device"]
        Antenna = self.env["nsp.device.antenna"]
        readers = Reader.search([
            ("active", "=", True),
            ("whitelist_id", "!=", False),
            ("whitelist_id.active", "=", True),
            ("whitelist_id.device_type_code", "=", "RFID_READER"),
        ])
        antennas = Antenna.search([
            ("active", "=", True),
            ("whitelist_id", "!=", False),
            ("whitelist_id.active", "=", True),
            ("whitelist_id.device_type_code", "=", "ANTENNA"),
        ])
        for record in self:
            record.available_reader_ids = readers
            record.available_antenna_ids = antennas

    @api.model
    def _validate_identity(self, record, type_code, label):
        if (
            not record or not record.active or not record.whitelist_id
            or not record.whitelist_id.active
            or record.whitelist_id.device_type_code != type_code
        ):
            raise ValidationError(
                _("%(label)s must be an active device from Device Whitelist.")
                % {"label": label}
            )

    def _validate_lane_mapping_consistency(self):
        for lane in self.mapped("lane_id"):
            endpoint_to_antenna = {}
            antenna_to_endpoint = {}
            for rule in lane.antenna_transition_ids:
                for reader, port_no, antenna, side in (
                    (rule.from_device_id, int(rule.from_antenna_no or 0), rule.from_antenna_id, _("From")),
                    (rule.to_device_id, int(rule.to_antenna_no or 0), rule.to_antenna_id, _("To")),
                ):
                    self._validate_identity(reader, "RFID_READER", _("%s Reader") % side)
                    self._validate_identity(antenna, "ANTENNA", _("%s Antenna") % side)
                    if port_no <= 0:
                        raise ValidationError(_("Reader port number must be greater than zero."))
                    endpoint = (reader.id, port_no)
                    previous_antenna = endpoint_to_antenna.get(endpoint)
                    if previous_antenna and previous_antenna != antenna.id:
                        raise ValidationError(
                            _("The same Reader port cannot be mapped to different Antennas in one Lane.")
                        )
                    previous_endpoint = antenna_to_endpoint.get(antenna.id)
                    if previous_endpoint and previous_endpoint != endpoint:
                        raise ValidationError(
                            _("The same Antenna cannot be mapped to different Reader ports in one Lane.")
                        )
                    endpoint_to_antenna[endpoint] = antenna.id
                    antenna_to_endpoint[antenna.id] = endpoint
                if (
                    rule.from_device_id == rule.to_device_id
                    and int(rule.from_antenna_no or 0) == int(rule.to_antenna_no or 0)
                ):
                    raise ValidationError(_("From and To endpoints must be different."))
        return True

    @api.constrains(
        "lane_id", "from_device_id", "from_antenna_no", "from_antenna_id",
        "to_device_id", "to_antenna_no", "to_antenna_id", "duration_seconds",
    )
    def _check_transition(self):
        self._validate_lane_mapping_consistency()

    def _prepare_sync_payload(self):
        self.ensure_one()
        return {
            "from_reader_code": self.from_device_id.device_code or "",
            "from_serial_number": self.from_serial_number or "",
            "from_antenna_no": int(self.from_antenna_no or 0),
            "from_antenna_code": self.from_antenna_id.technical_code or "",
            "to_reader_code": self.to_device_id.device_code or "",
            "to_serial_number": self.to_serial_number or "",
            "to_antenna_no": int(self.to_antenna_no or 0),
            "to_antenna_code": self.to_antenna_id.technical_code or "",
            "event_type": self.event_type,
            "duration_seconds": float(self.duration_seconds or 0.0),
        }
