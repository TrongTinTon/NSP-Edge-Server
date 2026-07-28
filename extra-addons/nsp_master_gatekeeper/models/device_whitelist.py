# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class DeviceWhitelist(models.Model):
    _name = "nsp.device.whitelist"
    _description = "NSP Device Whitelist"
    _rec_name = "serial_number"
    _order = "serial_number, id"

    serial_number = fields.Char(string="Serial", required=True, index=True, copy=False)
    device_type_id = fields.Many2one(
        "nsp.device.type",
        string="Device Type",
        required=True,
        ondelete="restrict",
        index=True,
        default=lambda self: self.env.ref("nsp_master_gatekeeper.device_type_rfid_reader", raise_if_not_found=False).id or False,
    )
    model_id = fields.Many2one("nsp.reference.model", string="Model", ondelete="set null", index=True)
    vendor_id = fields.Many2one("nsp.reference.vendor", string="Vendor", ondelete="set null", index=True)

    _sql_constraints = [
        ("serial_number_unique", "unique(serial_number)", "Serial must be unique in Device Whitelist."),
    ]

    @api.model
    def _normalize_serial(self, value):
        return str(value or "").strip().upper()

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for source in vals_list:
            vals = dict(source)
            vals["serial_number"] = self._normalize_serial(vals.get("serial_number"))
            prepared.append(vals)
        return super().create(prepared)

    def write(self, vals):
        values = dict(vals)
        if "serial_number" in values:
            values["serial_number"] = self._normalize_serial(values.get("serial_number"))
        return super().write(values)

    @api.constrains("serial_number")
    def _check_values(self):
        for record in self:
            if not self._normalize_serial(record.serial_number):
                raise ValidationError(_("Serial is required."))
