# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import AccessError, ValidationError, UserError
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
    lane_ids = fields.One2many("nsp.parking.lane", "parking_area_id", string="Parking Lanes")
    controller_ids = fields.Many2many(
        "nsp.controller", string="Controllers", compute="_compute_topology",
        search="_search_controllers",
    )
    reader_ids = fields.Many2many("nsp.device", string="Readers", compute="_compute_topology")
    controller_count = fields.Integer(compute="_compute_counts")
    reader_count = fields.Integer(compute="_compute_counts")
    lane_count = fields.Integer(compute="_compute_counts")
    whitelist_count = fields.Integer(compute="_compute_whitelist_count")

    _sql_constraints = [
        ("code_unique", "unique(code)", "Parking Area Code must be unique."),
    ]

    @api.depends(
        "lane_ids.active",
        "lane_ids.controller_id",
        "lane_ids.timeline_line_ids.reader_id",
        "lane_ids.timeline_line_ids.port_no",
    )
    def _compute_topology(self):
        for record in self:
            lanes = record.lane_ids.filtered("active")
            record.controller_ids = lanes.mapped("controller_id")
            record.reader_ids = lanes.mapped("timeline_line_ids.reader_id")

    @api.model
    def _search_controllers(self, operator, value):
        return [("lane_ids.controller_id", operator, value)]

    @api.depends("controller_ids", "reader_ids", "lane_ids.active")
    def _compute_counts(self):
        for record in self:
            record.controller_count = len(record.controller_ids)
            record.reader_count = len(record.reader_ids)
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

    def action_set_operational(self):
        for record in self:
            issues = record._operational_issues()
            if issues:
                raise UserError("\n".join(issues))
        self.write({"state": "operational"})
        return True

    def action_reset_to_draft(self):
        self.write({"state": "draft"})
        return True

    def action_set_maintenance(self):
        self.write({"state": "maintenance"})
        return True

    def action_set_blocked(self):
        self.write({"state": "blocked"})
        return True

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
        action = self.env.ref(action_xmlid).sudo().read()[0]
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
        action = self.env.ref("nsp_business_gatekeeper.action_nsp_parking_lane").sudo().read()[0]
        action.update({
            "name": _("Parking Lanes"),
            "domain": [("parking_area_id", "=", self.id)],
            "context": {"default_parking_area_id": self.id},
        })
        return action


class NspParkingLane(models.Model):
    """One physical Lane with a calibrated timeline and explicit event sequences."""

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
    active = fields.Boolean(default=True, index=True)
    tolerance_type = fields.Selection(
        [("percent", "Percentage (%)"), ("seconds", "Seconds")],
        string="Tolerance Type", default="percent", required=True,
    )
    tolerance_value = fields.Float(string="Tolerance Value", default=30.0, required=True)
    timeline_line_ids = fields.One2many(
        "nsp.parking.lane.timeline", "lane_id", string="Reader Port Timeline",
    )
    event_sequence_ids = fields.One2many(
        "nsp.parking.lane.event.sequence", "lane_id", string="Parking Event Sequences",
    )
    checkin_sequence_ids = fields.One2many(
        "nsp.parking.lane.event.sequence", "lane_id",
        domain=[("sequence_type", "=", "check_in")], string="Check-in Sequence",
    )
    checkout_sequence_ids = fields.One2many(
        "nsp.parking.lane.event.sequence", "lane_id",
        domain=[("sequence_type", "=", "check_out")], string="Check-out Sequence",
    )
    total_path_duration = fields.Float(
        string="Total Path Duration", compute="_compute_total_path_duration", digits=(8, 3),
    )
    timeline_point_count = fields.Integer(string="Timeline Points", compute="_compute_timeline_point_count")

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

    @api.depends("timeline_line_ids.duration_from_previous")
    def _compute_total_path_duration(self):
        for record in self:
            record.total_path_duration = sum(record.timeline_line_ids.mapped("duration_from_previous"))

    @api.depends("timeline_line_ids")
    def _compute_timeline_point_count(self):
        for record in self:
            record.timeline_point_count = len(record.timeline_line_ids)

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
        line = self.timeline_line_ids.filtered(lambda item: item.sequence == sequence)[:1]
        base = float(line.duration_from_previous or 0.0) if line else 0.0
        if self.tolerance_type == "seconds":
            return base + float(self.tolerance_value or 0.0)
        return base * (1.0 + float(self.tolerance_value or 0.0) / 100.0)

    def max_sequence_window(self):
        self.ensure_one()
        return max(1.0, sum(
            self.allowed_duration_for_step(line.sequence)
            for line in self.timeline_line_ids.filtered(lambda item: item.sequence > 1)
        ))

    def _validate_runtime_configuration(self):
        for lane in self:
            controller = lane.controller_id
            if not controller or not controller.active or controller.cloud_removed:
                raise ValidationError(
                    _("Lane %(lane)s requires an active Controller.")
                    % {"lane": lane.display_name}
                )
            timeline = lane.timeline_line_ids.sorted(
                lambda row: (row.sequence or 0, row.id)
            )
            if len(timeline) < 2:
                raise ValidationError(
                    _("Lane %(lane)s requires at least two Reader Port Timeline points.")
                    % {"lane": lane.display_name}
                )
            if timeline.mapped("sequence") != list(range(1, len(timeline) + 1)):
                raise ValidationError(
                    _("Lane Timeline Order must be contiguous and start at 1.")
                )
            timeline_keys = [
                (line.reader_id.id, int(line.port_no or 0))
                for line in timeline
            ]
            if len(timeline_keys) != len(set(timeline_keys)):
                raise ValidationError(
                    _("A Reader Port can appear only once in the Lane Timeline.")
                )
            for index, line in enumerate(timeline):
                if line.reader_id.controller_id != controller:
                    raise ValidationError(
                        _("Every Timeline Reader must belong to the Lane Controller.")
                    )
                if int(line.port_no or 0) < 1 or int(line.port_no or 0) > 16:
                    raise ValidationError(
                        _("Timeline Reader Port must be an integer from 1 to 16.")
                    )
                if index == 0 and float(line.duration_from_previous or 0.0) != 0.0:
                    raise ValidationError(
                        _("The first Timeline point must have zero Duration from previous.")
                    )
                if index > 0 and float(line.duration_from_previous or 0.0) <= 0.0:
                    raise ValidationError(
                        _("Every Timeline point after the first requires a positive Duration.")
                    )
            if not lane.checkin_sequence_ids and not lane.checkout_sequence_ids:
                raise ValidationError(
                    _("Lane %(lane)s must define at least one Check-in or Check-out Sequence.")
                    % {"lane": lane.display_name}
                )
            timeline_position = {
                key: position
                for position, key in enumerate(timeline_keys, start=1)
            }
            orientation_by_type = {}
            for sequence_type, rows, label in (
                ("check_in", lane.checkin_sequence_ids, _("Check-in")),
                ("check_out", lane.checkout_sequence_ids, _("Check-out")),
            ):
                ordered = rows.sorted(lambda row: (row.sequence or 0, row.id))
                if not ordered:
                    continue
                if len(ordered) < 2:
                    raise ValidationError(
                        _("%(label)s Sequence requires at least two Reader Ports.")
                        % {"label": label}
                    )
                if ordered.mapped("sequence") != list(range(1, len(ordered) + 1)):
                    raise ValidationError(
                        _("%(label)s Sequence Order must be contiguous and start at 1.")
                        % {"label": label}
                    )
                sequence_keys = [
                    (row.reader_id.id, int(row.port_no or 0))
                    for row in ordered
                ]
                if len(sequence_keys) != len(set(sequence_keys)):
                    raise ValidationError(
                        _("A Reader Port can appear only once in the %(label)s Sequence.")
                        % {"label": label}
                    )
                if any(key not in timeline_position for key in sequence_keys):
                    raise ValidationError(
                        _("%(label)s Sequence must use the Lane Reader Port Timeline.")
                        % {"label": label}
                    )
                positions = [timeline_position[key] for key in sequence_keys]
                if any(
                    abs(current - previous) != 1
                    for previous, current in zip(positions, positions[1:])
                ):
                    raise ValidationError(
                        _("%(label)s Sequence must follow adjacent points in the Reader Port Timeline.")
                        % {"label": label}
                    )
                orientation_by_type[sequence_type] = (
                    1 if positions[1] > positions[0] else -1
                )
            if (
                orientation_by_type.get("check_in")
                and orientation_by_type.get("check_out")
                and orientation_by_type["check_in"] == orientation_by_type["check_out"]
            ):
                raise ValidationError(
                    _("Check-in and Check-out Sequences must follow opposite Timeline directions.")
                )
        return True

    def _runtime_payload(self):
        self.ensure_one()
        return {
            "lane_code": self.code,
            "lane_name": self.name,
            "controller_code": self.controller_id.controller_id or "",
            "reader_port_timeline": [
                row._sync_payload()
                for row in self.timeline_line_ids.sorted("sequence")
            ],
            "event_sequences": {
                "check_in": [
                    row._sync_payload()
                    for row in self.checkin_sequence_ids.sorted("sequence")
                ],
                "check_out": [
                    row._sync_payload()
                    for row in self.checkout_sequence_ids.sorted("sequence")
                ],
            },
            "timing_tolerance": {
                "type": self.tolerance_type,
                "value": float(self.tolerance_value or 0.0),
            },
        }


class NspParkingLaneTimeline(models.Model):
    _name = "nsp.parking.lane.timeline"
    _description = "NSP Edge Parking Lane Reader Port Timeline"
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
    ]

    @api.constrains("lane_id", "reader_id", "port_no")
    def _check_reader_port(self):
        for record in self:
            allowed = {
                (line.reader_id.id, int(line.port_no or 0))
                for line in record.lane_id.timeline_line_ids
            }
            key = (record.reader_id.id, int(record.port_no or 0))
            if key not in allowed:
                raise ValidationError(
                    _("Event Sequence can use only Reader Ports from the Lane Timeline.")
                )

    def _sync_payload(self):
        self.ensure_one()
        return {
            "reader_code": self.reader_id.device_code or "",
            "port_no": int(self.port_no or 0),
        }

