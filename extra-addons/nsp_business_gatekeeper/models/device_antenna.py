# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class DeviceAntenna(models.Model):
    """Edge cache of a physical RFID antenna port declared by Cloud."""

    _name = "nsp.device.antenna"
    _description = "NSP Reader Antenna"
    _rec_name = "display_name"
    _order = "device_id, antenna_no, id"

    display_name = fields.Char(string="Antenna", compute="_compute_display_name", store=True)
    whitelist_id = fields.Many2one(
        "nsp.device.whitelist", string="Device Whitelist", readonly=True, copy=False,
        ondelete="set null", index=True,
    )
    technical_code = fields.Char(string="Technical Code", readonly=True, copy=False, index=True)
    serial_number = fields.Char(string="Serial Number", copy=False, index=True)
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
        readonly=True,
    )
    controller_id = fields.Many2one(
        "nsp.controller",
        string="Controller",
        related="device_id.controller_id",
        readonly=True,
    )
    active = fields.Boolean(default=True, index=True)
    cloud_removed = fields.Boolean(default=False, readonly=True, index=True, copy=False)

    _sql_constraints = [
        ("antenna_technical_code_unique", "unique(technical_code)", "Antenna Technical Code must be unique."),
        (
            "device_antenna_unique",
            "unique(device_id, antenna_no)",
            "Antenna number must be unique per Reader.",
        ),
        (
            "antenna_no_positive",
            "CHECK(antenna_no > 0)",
            "Antenna number must be greater than zero.",
        ),
    ]

    @api.depends("device_id.name", "device_id.serial_number", "antenna_no")
    def _compute_display_name(self):
        for antenna in self:
            antenna.display_name = "%s / Antenna %s" % (
                antenna.device_id.name or antenna.device_id.serial_number or _("Reader"),
                antenna.antenna_no or "",
            )

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for source in vals_list:
            vals = dict(source)
            self._validate_values(vals)
            prepared.append(vals)
        return super().create(prepared)

    def write(self, vals):
        values = dict(vals)
        self._validate_values(values, partial=True)
        return super().write(values)

    @api.model
    def _validate_values(self, values, partial=False):
        if "antenna_no" in values or not partial:
            if int(values.get("antenna_no") or 0) <= 0:
                raise ValidationError(_("Antenna number must be greater than zero."))
