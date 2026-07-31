# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class NspParkingAreaSequence(models.Model):
    _inherit = "nsp.parking.area"

    @api.depends(
        "lane_ids.active", "lane_ids.controller_id",
        "lane_ids.timeline_line_ids.antenna_id",
        "lane_ids.timeline_line_ids.reader_id",
    )
    def _compute_topology(self):
        for record in self:
            lanes = record.lane_ids.filtered("active")
            record.antenna_transition_ids = lanes.mapped("antenna_transition_ids")
            record.controller_ids = lanes.mapped("controller_id")
            record.reader_ids = lanes.mapped("timeline_line_ids.reader_id")
            record.antenna_ids = lanes.mapped("timeline_line_ids.antenna_id")

    def _lane_payload(self):
        self.ensure_one()
        result = []
        for lane in self.lane_ids.filtered("active").sorted(
            key=lambda item: ((item.name or "").casefold(), item.code or "", item.id)
        ):
            result.append({
                "lane_code": lane.code,
                "lane_name": lane.name,
                "controller_code": lane.controller_id.controller_id,
                "direction": lane.direction,
                "tolerance_type": lane.tolerance_type,
                "tolerance_value": float(lane.tolerance_value or 0.0),
                "timeline": [line._sync_payload() for line in lane.timeline_line_ids.sorted("sequence")],
                "check_in_sequence": [line.antenna_id.technical_code or "" for line in lane.checkin_sequence_ids.sorted("sequence")],
                "check_out_sequence": [line.antenna_id.technical_code or "" for line in lane.checkout_sequence_ids.sorted("sequence")],
            })
        return result

    def _operational_issues(self):
        issues = []
        for area in self:
            lanes = area.lane_ids.filtered("active")
            if not lanes:
                issues.append(_("Configure at least one active Parking Lane."))
                continue
            for lane in lanes:
                if not lane.controller_id:
                    issues.append(_("Lane %s must have a Controller.") % lane.display_name)
                if len(lane.timeline_line_ids) < 2:
                    issues.append(_("Lane %s requires at least two Antenna Timeline points.") % lane.display_name)
                if lane.direction in ("entry", "bidirectional") and not lane.checkin_sequence_ids:
                    issues.append(_("Lane %s requires a Check-in Sequence.") % lane.display_name)
                if lane.direction in ("exit", "bidirectional") and not lane.checkout_sequence_ids:
                    issues.append(_("Lane %s requires a Check-out Sequence.") % lane.display_name)
        return issues


class NspParkingLaneSequence(models.Model):
    _inherit = "nsp.parking.lane"

    direction = fields.Selection([
        ("entry", "Entry"), ("exit", "Exit"), ("bidirectional", "Bidirectional"),
    ], default="entry", required=True, index=True)
    tolerance_type = fields.Selection([
        ("percent", "Percentage (%)"), ("seconds", "Seconds"),
    ], default="percent", required=True)
    tolerance_value = fields.Float(default=30.0, required=True)
    timeline_line_ids = fields.One2many(
        "nsp.parking.lane.timeline", "lane_id", string="Antenna Timeline",
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
    total_path_duration = fields.Float(compute="_compute_total_path_duration", digits=(8, 3))

    @api.depends("timeline_line_ids.duration_from_previous")
    def _compute_total_path_duration(self):
        for lane in self:
            lane.total_path_duration = sum(lane.timeline_line_ids.mapped("duration_from_previous"))

    @api.depends("timeline_line_ids")
    def _compute_transition_count(self):
        for lane in self:
            lane.transition_count = len(lane.timeline_line_ids)

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


class NspParkingLaneTimeline(models.Model):
    _name = "nsp.parking.lane.timeline"
    _description = "NSP Edge Parking Lane Antenna Timeline"
    _order = "lane_id, sequence, id"

    lane_id = fields.Many2one("nsp.parking.lane", required=True, ondelete="cascade", index=True)
    sequence = fields.Integer(required=True, index=True)
    antenna_id = fields.Many2one("nsp.device.antenna", required=True, ondelete="restrict", index=True)
    reader_id = fields.Many2one("nsp.device", required=True, ondelete="restrict", index=True)
    port_no = fields.Integer(required=True)
    duration_from_previous = fields.Float(required=True, digits=(8, 3), default=0.0)
    cumulative_time = fields.Float(digits=(8, 3), default=0.0)

    _sql_constraints = [
        ("edge_lane_timeline_order_unique", "unique(lane_id, sequence)", "Timeline order must be unique per Lane."),
        ("edge_lane_timeline_antenna_unique", "unique(lane_id, antenna_id)", "An Antenna can appear only once in a Lane Timeline."),
    ]

    def _sync_payload(self):
        self.ensure_one()
        return {
            "sequence": self.sequence,
            "antenna_code": self.antenna_id.technical_code or "",
            "reader_code": self.reader_id.device_code or "",
            "reader_serial_number": self.reader_id.serial_number or "",
            "port_no": self.port_no,
            "duration_from_previous": float(self.duration_from_previous or 0.0),
            "cumulative_time": float(self.cumulative_time or 0.0),
        }


class NspParkingLaneEventSequence(models.Model):
    _name = "nsp.parking.lane.event.sequence"
    _description = "NSP Edge Parking Lane Event Sequence"
    _order = "lane_id, sequence_type, sequence, id"

    lane_id = fields.Many2one("nsp.parking.lane", required=True, ondelete="cascade", index=True)
    sequence_type = fields.Selection([
        ("check_in", "Check-in"), ("check_out", "Check-out"),
    ], required=True, index=True)
    sequence = fields.Integer(required=True)
    antenna_id = fields.Many2one("nsp.device.antenna", required=True, ondelete="restrict", index=True)

    _sql_constraints = [
        ("edge_lane_event_sequence_order_unique", "unique(lane_id, sequence_type, sequence)", "Event sequence order must be unique."),
        ("edge_lane_event_sequence_antenna_unique", "unique(lane_id, sequence_type, antenna_id)", "An Antenna can appear only once in one Event Sequence."),
    ]
