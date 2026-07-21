# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class DeviceAntenna(models.Model):
    """Antenna ports declared on an RFID Reader."""

    _name = "nsp.device.antenna"
    _description = "NSP Reader Antenna"
    _rec_name = "display_name"
    _order = "device_id, antenna_no, id"

    display_name = fields.Char(string="Antenna", compute="_compute_display_name", store=True)
    antenna_no = fields.Integer(string="Antenna No", required=True, index=True)
    device_id = fields.Many2one(
        "nsp.device",
        string="Reader",
        ondelete="cascade",
        required=True,
        index=True,
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
        ("device_antenna_unique", "unique(device_id, antenna_no)", "Antenna number must be unique per Reader."),
        ("antenna_no_positive", "CHECK(antenna_no > 0)", "Antenna number must be greater than zero."),
    ]

    @api.depends("device_id.serial_number", "antenna_no")
    def _compute_display_name(self):
        for antenna in self:
            antenna.display_name = "%s / Antenna %s" % (
                antenna.device_id.serial_number or _("Reader"),
                antenna.antenna_no or "",
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
            if int(vals.get("antenna_no") or 0) <= 0:
                raise ValidationError(_("Antenna number must be greater than zero."))
            prepared.append(vals)
        return super().create(prepared)

    def write(self, vals):
        values = dict(vals)
        if "antenna_no" in values and int(values.get("antenna_no") or 0) <= 0:
            raise ValidationError(_("Antenna number must be greater than zero."))
        return super().write(values)
