# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class DeviceAntenna(models.Model):
    """Physical antenna ports declared on an RFID reader.

    Reader parameters belong to ``nsp.device``.  An antenna only identifies the
    physical port/position that can be selected in parking-lane mappings and
    measurement sessions.
    """

    _name = "nsp.device.antenna"
    _description = "NSP Reader Antenna"
    _rec_name = "display_name"
    _order = "device_id, antenna_id, id"

    display_name = fields.Char(string="Antenna", compute="_compute_display_name", store=True)
    antenna_id = fields.Integer(string="Antenna No", required=True, index=True)
    physical_antenna = fields.Char(
        string="Physical Antenna",
        required=True,
        help="Human-readable physical port or installation position, for example ANT-1 or Entry Left.",
    )
    device_id = fields.Many2one(
        "nsp.device",
        string="Reader",
        ondelete="cascade",
        required=True,
        index=True,
        help="RFID reader that owns this physical antenna.",
    )
    device_serial = fields.Char(
        string="Reader Serial",
        related="device_id.serial_number",
        store=True,
        readonly=True,
        index=True,
    )
    controller_id = fields.Many2one(
        "nsp.controller",
        string="Controller",
        related="device_id.controller_id",
        store=True,
        readonly=True,
        index=True,
    )
    lane_rule_ids = fields.One2many(
        "nsp.parking.lane.antenna.mapping",
        "antenna_ref_id",
        string="Parking Lane Antenna Mapping",
        readonly=True,
    )
    lane_count = fields.Integer(string="Mapped Lanes", compute="_compute_lane_count", store=False)

    _sql_constraints = [
        ("device_antenna_unique", "unique(device_id, antenna_id)", "Antenna number must be unique per Reader."),
        ("antenna_no_positive", "CHECK(antenna_id > 0)", "Antenna number must be greater than zero."),
    ]

    @api.depends("device_id.serial_number", "antenna_id", "physical_antenna")
    def _compute_display_name(self):
        for antenna in self:
            label = antenna.physical_antenna or _("Physical Antenna")
            antenna.display_name = "%s / Antenna %s / %s" % (
                antenna.device_id.serial_number or _("Reader"),
                antenna.antenna_id or "",
                label,
            )

    @api.depends("lane_rule_ids", "lane_rule_ids.is_active")
    def _compute_lane_count(self):
        for antenna in self:
            antenna.lane_count = len(antenna.lane_rule_ids.filtered("is_active"))

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for source in vals_list:
            vals = dict(source)
            antenna_no = int(vals.get("antenna_id") or 0)
            if antenna_no <= 0:
                raise ValidationError(_("Antenna number must be greater than zero."))
            vals["physical_antenna"] = str(
                vals.get("physical_antenna") or "ANT-%s" % antenna_no
            ).strip()
            prepared.append(vals)
        return super().create(prepared)

    def write(self, vals):
        values = dict(vals)
        if "antenna_id" in values and int(values.get("antenna_id") or 0) <= 0:
            raise ValidationError(_("Antenna number must be greater than zero."))
        if "physical_antenna" in values:
            values["physical_antenna"] = str(values.get("physical_antenna") or "").strip()
            if not values["physical_antenna"]:
                raise ValidationError(_("Physical Antenna is required."))
        return super().write(values)
