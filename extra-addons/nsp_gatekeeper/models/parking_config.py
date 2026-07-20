# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError, UserError

class NspParkingArea(models.Model):
    """Server-owned parking topology and operational configuration.

    Controllers receive only their managed device settings; this topology remains
    on Cloud/Edge servers.
    """

    _name = "nsp.parking.area"
    _description = "NSP Parking Operation Configuration"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _rec_name = "name"
    _order = "branch_id, name, id"

    name = fields.Char(string="Parking Area Name", required=True, tracking=True)
    code = fields.Char(string="Parking Area Code", required=True, tracking=True, copy=False, index=True)
    branch_id = fields.Many2one(
        "nsp.branch", string="Branch", required=True, ondelete="restrict", tracking=True, index=True
    )
    status = fields.Selection([
        ("active", "Active"),
        ("blocked", "Blocked"),
        ("maintenance", "Maintenance"),
    ], string="Status", default="active", required=True, tracking=True, index=True)
    operation_state = fields.Selection([
        ("draft", "Draft / Configuring"),
        ("operational", "Operational"),
    ], string="Operation State", default="draft", required=True, tracking=True, index=True)
    detection_window_ms = fields.Integer(string="Default Detection Window (ms)", default=1500, required=True)

    lane_ids = fields.One2many("nsp.parking.lane", "parking_area_id", string="Parking Lanes")
    lane_antenna_group_ids = fields.One2many(
        "nsp.parking.lane.antenna.group", "parking_area_id", string="Antenna Groups", readonly=True
    )
    lane_antenna_rule_ids = fields.One2many(
        "nsp.parking.lane.antenna.mapping", "parking_area_id", string="Antenna Mapping", readonly=True
    )
    controller_ids = fields.Many2many(
        "nsp.controller", string="Controllers", compute="_compute_controllers", search="_search_controllers",
        help="Controllers used by active parking lanes. This list is derived automatically."
    )
    controller_count = fields.Integer(string="Controllers", compute="_compute_counts")
    lane_count = fields.Integer(string="Lanes", compute="_compute_counts")
    antenna_count = fields.Integer(string="Mapped Antennas", compute="_compute_counts")

    _sql_constraints = [
        ("code_unique", "unique(code)", "Parking Area Code must be unique."),
        ("detection_window_positive", "CHECK(detection_window_ms > 0)", "Detection Window must be greater than zero."),
    ]

    @api.depends("lane_ids.active", "lane_ids.controller_id")
    def _compute_controllers(self):
        for rec in self:
            rec.controller_ids = rec.lane_ids.filtered("active").mapped("controller_id")

    @api.model
    def _search_controllers(self, operator, value):
        return [("lane_ids.controller_id", operator, value)]

    @api.depends(
        "lane_ids.active", "lane_ids.controller_id",
        "lane_antenna_rule_ids.is_active",
    )
    def _compute_counts(self):
        for rec in self:
            lanes = rec.lane_ids.filtered("active")
            rec.controller_count = len(lanes.mapped("controller_id"))
            rec.lane_count = len(lanes)
            rec.antenna_count = len(rec.lane_antenna_rule_ids.filtered(lambda item: item.is_active))

    @api.model
    def _normalize_code(self, value):
        return str(value or "").strip().upper()

    @api.model_create_multi
    def create(self, vals_list):
        Branch = self.env["nsp.branch"].sudo()
        default_branch = Branch.get_default_branch() if hasattr(Branch, "get_default_branch") else Branch.search([], limit=1)
        prepared = []
        for source in vals_list:
            vals = dict(source)
            if not vals.get("branch_id") and default_branch:
                vals["branch_id"] = default_branch.id
            vals["code"] = self._normalize_code(vals.get("code"))
            prepared.append(vals)
        return super().create(prepared)

    def write(self, vals):
        values = dict(vals)
        if "code" in values:
            values["code"] = self._normalize_code(values.get("code"))
        return super().write(values)

    def _lane_payload(self):
        """Return server topology for Cloud/Edge synchronization.

        This payload is not exposed to Controllers. Controller device pull returns
        only reader and antenna technical settings from nsp.device.
        """
        self.ensure_one()
        result = []
        lanes = self.lane_ids.filtered("active").sorted(key=lambda item: (item.sequence, item.lane_no, item.id))
        for lane in lanes:
            groups = []
            for group in lane.antenna_group_ids.filtered("active").sorted(key=lambda item: (item.sequence, item.direction, item.id)):
                antennas = []
                mappings = group.antenna_mapping_ids.filtered(lambda item: item.is_active and item.antenna_ref_id)
                for mapping in mappings.sorted(key=lambda item: (item.sequence_no, item.serial_number or "", item.antenna_no, item.id)):
                    item = {
                        "serial_number": mapping.serial_number or "",
                        "antenna_no": int(mapping.antenna_no or 0),
                        "tag_role": mapping.tag_role,
                    }
                    if group.detection_mode == "sequential":
                        item["sequence_no"] = int(mapping.sequence_no or 0)
                    antennas.append(item)
                groups.append({
                    "direction": group.direction,
                    "detection_mode": group.detection_mode,
                    "antennas": antennas,
                })
            result.append({
                "lane_code": lane.code,
                "lane_name": lane.name,
                "controller_code": lane.controller_id.controller_id,
                "lane_type": lane.lane_type,
                "direction": lane.direction,
                "detection_window_ms": int(lane.detection_window_ms or self.detection_window_ms),
                "required_vehicle_tid": bool(lane.required_vehicle_tid),
                "required_user_tid": bool(lane.required_user_tid),
                "antenna_groups": groups,
            })
        return result

    def _operational_issues(self):
        self.ensure_one()
        issues = []
        lanes = self.lane_ids.filtered("active")
        if not lanes:
            return [_('Configure at least one active Parking Lane.')]
        for lane in lanes:
            lane_name = lane.display_name or lane.code
            if not lane.controller_id:
                issues.append(_("Lane %(lane)s must have a Controller.") % {"lane": lane_name})
                continue
            groups = lane.antenna_group_ids.filtered("active")
            expected_directions = {lane.direction} if lane.lane_type == "one_way" else {"entry", "exit"}
            if set(groups.mapped("direction")) != expected_directions or len(groups) != len(expected_directions):
                issues.append(_("Lane %(lane)s must have exactly the antenna groups required by its direction.") % {"lane": lane_name})
            for group in groups:
                mappings = group.antenna_mapping_ids.filtered("is_active")
                if not mappings:
                    issues.append(_("Antenna group %(direction)s of lane %(lane)s has no active antenna.") % {
                        "direction": group.direction, "lane": lane_name,
                    })
                    continue
                wrong_scope = mappings.filtered(lambda item: item.controller_id != lane.controller_id)
                if wrong_scope:
                    issues.append(_("All antennas of lane %(lane)s must belong to Controller %(controller)s.") % {
                        "lane": lane_name, "controller": lane.controller_id.controller_id,
                    })
                invalid = mappings.filtered(
                    lambda item: not item.device_id.managed or not item.antenna_ref_id.is_active
                )
                if invalid:
                    issues.append(_("Lane %(lane)s contains an inactive device or antenna.") % {"lane": lane_name})
                if group.detection_mode == "sequential":
                    sequence = sorted(int(item.sequence_no or 0) for item in mappings)
                    if sequence != list(range(1, len(sequence) + 1)):
                        issues.append(_("Sequential group %(direction)s of lane %(lane)s must use continuous sequence numbers starting at 1.") % {
                            "direction": group.direction, "lane": lane_name,
                        })
                elif any(int(item.sequence_no or 0) for item in mappings):
                    issues.append(_("Any-mode group %(direction)s of lane %(lane)s must use sequence number 0.") % {
                        "direction": group.direction, "lane": lane_name,
                    })
        return issues

    def action_set_operational(self):
        for rec in self:
            issues = rec._operational_issues()
            if issues:
                raise UserError("\n".join(issues))
        self.write({"operation_state": "operational"})
        return True

    def action_reset_to_draft(self):
        self.write({"operation_state": "draft"})
        return True

    def prepare_sync_payload(self):
        self.ensure_one()
        return {
            "parking_area_code": self.code,
            "parking_area_name": self.name,
            "branch_code": self.branch_id.code or "",
            "active": self.status == "active",
            "operational": self.operation_state == "operational",
            "lanes": self._lane_payload(),
        }

class NspParkingLane(models.Model):
    _name = "nsp.parking.lane"
    _description = "NSP Parking Lane"
    _order = "parking_area_id, sequence, lane_no, id"
    _rec_name = "display_name"

    name = fields.Char(string="Lane Name", required=True, default="Lane")
    code = fields.Char(string="Lane Code", required=True, copy=False, index=True)
    display_name = fields.Char(string="Display Name", compute="_compute_display_name", store=True)
    parking_area_id = fields.Many2one("nsp.parking.area", string="Parking Area", required=True, ondelete="cascade", index=True)
    branch_id = fields.Many2one("nsp.branch", related="parking_area_id.branch_id", store=True, readonly=True, index=True)
    controller_id = fields.Many2one(
        "nsp.controller", string="Controller", required=True, ondelete="restrict", index=True,
        help="Controller that owns every reader and antenna used by this lane."
    )
    lane_no = fields.Integer(string="Lane No.", default=1, required=True)
    sequence = fields.Integer(default=10)
    lane_type = fields.Selection([
        ("one_way", "One-way"),
        ("two_way", "Two-way"),
    ], required=True, default="one_way", index=True)
    direction = fields.Selection([
        ("entry", "Entry"),
        ("exit", "Exit"),
        ("both", "Two-way"),
    ], required=True, default="entry", index=True)
    detection_window_ms = fields.Integer(string="Detection Window (ms)", default=1500, required=True)
    required_vehicle_tid = fields.Boolean(string="Require Vehicle TID", default=True)
    required_user_tid = fields.Boolean(string="Require User TID", default=False)
    active = fields.Boolean(default=True, index=True)
    antenna_group_ids = fields.One2many("nsp.parking.lane.antenna.group", "lane_id", string="Antenna Groups")
    antenna_rule_ids = fields.One2many("nsp.parking.lane.antenna.mapping", "lane_id", string="Antenna Mapping", readonly=True)
    active_antenna_count = fields.Integer(compute="_compute_active_antenna_count")

    _sql_constraints = [
        ("lane_code_per_area_unique", "unique(parking_area_id, code)", "Lane Code must be unique within a Parking Area."),
        ("lane_no_per_area_unique", "unique(parking_area_id, lane_no)", "Lane number must be unique within a Parking Area."),
        ("lane_no_positive", "CHECK(lane_no > 0)", "Lane number must be greater than zero."),
        ("lane_detection_window_positive", "CHECK(detection_window_ms > 0)", "Detection Window must be greater than zero."),
    ]

    @api.depends("parking_area_id.code", "code", "name")
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = "%s / %s" % (rec.parking_area_id.code or "Parking", rec.code or rec.name or "Lane")

    @api.depends("antenna_rule_ids.is_active")
    def _compute_active_antenna_count(self):
        for rec in self:
            rec.active_antenna_count = len(rec.antenna_rule_ids.filtered("is_active"))

    @api.model
    def _normalize_code(self, value):
        return str(value or "").strip().upper()

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            vals["code"] = self._normalize_code(vals.get("code") or vals.get("name") or "LANE")
            if vals.get("lane_type") == "two_way":
                vals["direction"] = "both"
            elif vals.get("direction") not in ("entry", "exit"):
                vals["direction"] = "entry"
        return super().create(vals_list)

    def write(self, vals):
        values = dict(vals)
        if "code" in values:
            values["code"] = self._normalize_code(values.get("code"))
        if values.get("lane_type") == "two_way":
            values["direction"] = "both"
        elif values.get("lane_type") == "one_way" and values.get("direction") not in ("entry", "exit"):
            values["direction"] = "entry"
        result = super().write(values)
        if "controller_id" in values:
            invalid = self.mapped("antenna_rule_ids").filtered(
                lambda item: item.controller_id != item.lane_id.controller_id
            )
            if invalid:
                raise ValidationError(_("Existing antenna mappings do not belong to the selected Lane Controller."))
        return result

    @api.constrains("lane_type", "direction")
    def _check_direction(self):
        for rec in self:
            if rec.lane_type == "one_way" and rec.direction not in ("entry", "exit"):
                raise ValidationError(_("One-way Lane direction must be Entry or Exit."))
            if rec.lane_type == "two_way" and rec.direction != "both":
                raise ValidationError(_("Two-way Lane direction must be Two-way."))

class NspParkingLaneAntennaGroup(models.Model):
    _name = "nsp.parking.lane.antenna.group"
    _description = "NSP Lane Antenna Group"
    _order = "lane_id, sequence, direction, id"
    _rec_name = "display_name"

    display_name = fields.Char(compute="_compute_display_name", store=True)
    lane_id = fields.Many2one("nsp.parking.lane", required=True, ondelete="cascade", index=True)
    parking_area_id = fields.Many2one("nsp.parking.area", related="lane_id.parking_area_id", store=True, readonly=True, index=True)
    controller_id = fields.Many2one("nsp.controller", related="lane_id.controller_id", store=True, readonly=True, index=True)
    direction = fields.Selection([("entry", "Entry"), ("exit", "Exit")], required=True, index=True)
    detection_mode = fields.Selection([
        ("sequential", "Sequential"),
        ("any", "Any Antenna"),
    ], required=True, default="sequential", index=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True, index=True)
    antenna_mapping_ids = fields.One2many("nsp.parking.lane.antenna.mapping", "antenna_group_id", string="Antennas")

    _sql_constraints = [
        ("unique_lane_direction", "unique(lane_id, direction)", "A Lane can have only one Antenna Group per direction."),
    ]

    @api.depends("lane_id.display_name", "direction", "detection_mode")
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = "%s / %s / %s" % (
                rec.lane_id.display_name or "Lane", rec.direction or "", rec.detection_mode or ""
            )

    @api.constrains("lane_id", "direction")
    def _check_lane_direction(self):
        for rec in self:
            if rec.lane_id.lane_type == "one_way" and rec.direction != rec.lane_id.direction:
                raise ValidationError(_("A one-way Lane Antenna Group must match the Lane direction."))

class NspParkingLaneAntennaMapping(models.Model):
    _name = "nsp.parking.lane.antenna.mapping"
    _description = "NSP Parking Lane Antenna Mapping"
    _order = "parking_area_id, lane_id, antenna_group_id, sequence_no, id"
    _rec_name = "rule_name"

    rule_name = fields.Char(compute="_compute_rule_name", store=True)
    antenna_group_id = fields.Many2one(
        "nsp.parking.lane.antenna.group", string="Antenna Group", required=True, ondelete="cascade", index=True
    )
    lane_id = fields.Many2one("nsp.parking.lane", related="antenna_group_id.lane_id", store=True, readonly=True, index=True)
    lane_controller_id = fields.Many2one("nsp.controller", related="lane_id.controller_id", store=True, readonly=True, index=True)
    parking_area_id = fields.Many2one("nsp.parking.area", related="antenna_group_id.parking_area_id", store=True, readonly=True, index=True)
    group_direction = fields.Selection(related="antenna_group_id.direction", store=True, readonly=True, index=True)
    detection_mode = fields.Selection(related="antenna_group_id.detection_mode", store=True, readonly=True, index=True)
    tag_role = fields.Selection([
        ("vehicle_tid", "Vehicle TID"),
        ("user_tid", "User TID"),
        ("both", "Vehicle/User TID"),
    ], default="vehicle_tid", required=True, index=True)
    effective_direction = fields.Selection(
        [("entry", "Entry"), ("exit", "Exit")], related="antenna_group_id.direction",
        store=True, readonly=True, index=True
    )
    antenna_ref_id = fields.Many2one(
        "nsp.device.antenna", string="Physical Antenna", required=True, ondelete="restrict", index=True
    )
    controller_id = fields.Many2one(
        "nsp.controller", related="antenna_ref_id.device_id.controller_id", store=True, readonly=True, index=True
    )
    device_id = fields.Many2one("nsp.device", related="antenna_ref_id.device_id", store=True, readonly=True, index=True)
    serial_number = fields.Char(related="device_id.serial_number", store=True, readonly=True, index=True)
    antenna_no = fields.Integer(related="antenna_ref_id.antenna_id", store=True, readonly=True, index=True)
    sequence_no = fields.Integer(default=0)
    is_active = fields.Boolean(default=True, index=True)

    _sql_constraints = [
        ("unique_group_antenna", "unique(antenna_group_id, antenna_ref_id)", "This physical antenna is already mapped to this Antenna Group."),
    ]

    @api.depends(
        "parking_area_id.code", "lane_id.code", "serial_number", "antenna_no",
        "group_direction", "detection_mode", "sequence_no",
    )
    def _compute_rule_name(self):
        for rec in self:
            rec.rule_name = "%s / %s / %s:%s / %s / Seq %s" % (
                rec.parking_area_id.code or "Parking",
                rec.lane_id.code or "Lane",
                rec.serial_number or "Device",
                rec.antenna_no or "",
                rec.group_direction or "",
                rec.sequence_no or 0,
            )

    @api.constrains("antenna_group_id", "antenna_ref_id", "sequence_no", "is_active")
    def _check_mapping(self):
        for rec in self:
            if rec.is_active and rec.antenna_ref_id:
                duplicate = self.search_count([
                    ("id", "!=", rec.id),
                    ("antenna_ref_id", "=", rec.antenna_ref_id.id),
                    ("is_active", "=", True),
                ])
                if duplicate:
                    raise ValidationError(_("A physical antenna can be actively mapped to only one Parking Lane direction."))
            if rec.controller_id != rec.lane_id.controller_id:
                raise ValidationError(_("Physical Antenna must belong to the Controller assigned to this Lane."))
            if not rec.device_id.managed:
                raise ValidationError(_("Physical Antenna must belong to a managed device."))
            if not rec.device_id.device_type_id or not rec.device_id.device_type_id.supports_antenna_mapping:
                raise ValidationError(_("Physical Antenna must belong to a device type that supports antenna mapping."))
            if rec.detection_mode == "sequential" and int(rec.sequence_no or 0) <= 0:
                raise ValidationError(_("Sequential antenna mapping requires a sequence number greater than zero."))
            if rec.detection_mode == "any" and int(rec.sequence_no or 0) != 0:
                raise ValidationError(_("Any-mode antenna mapping must use sequence number 0."))
