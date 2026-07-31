# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


VALID_DEVICE_TYPE_CODES = {"SERVER", "CONTROLLER", "RFID_READER", "ANTENNA"}


class DeviceWhitelist(models.Model):
    """Edge cache of device identities referenced by released assemblies.

    This model deliberately contains no parent, Reader port or operation
    parameters. Server, Controller, RFID Reader and Antenna relationships exist
    only in the active Lane Calibration or Parking Layout runtime assembly.
    """

    _name = "nsp.device.whitelist"
    _description = "NSP Device Whitelist"
    _inherit = ["image.mixin"]
    _rec_name = "display_name"
    _order = "device_type_id, technical_code, id"

    name = fields.Char(string="Device Name", readonly=True, copy=False, index=True)
    display_name = fields.Char(compute="_compute_display_name", store=True)
    active = fields.Boolean(default=True, index=True)
    device_type_id = fields.Many2one(
        "nsp.device.type", string="Device Type", required=True,
        ondelete="restrict", index=True,
    )
    device_type_code = fields.Char(
        related="device_type_id.code", store=True, readonly=True,
    )
    technical_code = fields.Char(
        string="Management Code", required=True, readonly=True,
        copy=False, index=True,
    )
    serial_number = fields.Char(string="Serial Number", index=True, copy=False)

    _sql_constraints = [
        (
            "device_whitelist_technical_code_unique", "unique(technical_code)",
            "Management Code must be unique.",
        ),
        (
            "device_whitelist_serial_unique", "unique(serial_number)",
            "Serial Number must be unique.",
        ),
    ]

    @api.depends("device_type_id.name", "serial_number", "technical_code")
    def _compute_display_name(self):
        for record in self:
            identity = record.serial_number or record.technical_code or ""
            record.display_name = "%s · %s" % (
                record.device_type_id.name or _("Device"), identity,
            )

    @api.model
    def _normalize_serial(self, value):
        value = str(value or "").strip().upper()
        return value or False

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for source in vals_list:
            values = dict(source)
            code = str(values.get("technical_code") or "").strip().upper()
            serial = self._normalize_serial(values.get("serial_number"))
            values.update({
                "technical_code": code,
                "serial_number": serial,
                "name": str(values.get("name") or serial or code).strip(),
            })
            prepared.append(values)
        return super().create(prepared)

    def write(self, vals):
        values = dict(vals)
        if "technical_code" in values:
            values["technical_code"] = str(values.get("technical_code") or "").strip().upper()
        if "serial_number" in values:
            values["serial_number"] = self._normalize_serial(values.get("serial_number"))
        if "name" in values:
            values["name"] = str(values.get("name") or "").strip()
        return super().write(values)

    @api.constrains("device_type_id", "technical_code", "serial_number")
    def _check_device_identity(self):
        for record in self:
            if record.device_type_code not in VALID_DEVICE_TYPE_CODES:
                raise ValidationError(_("Unsupported Device Type."))
            if not (record.technical_code or "").strip():
                raise ValidationError(_("Management Code is required."))
            if record.device_type_code == "RFID_READER" and not record.serial_number:
                raise ValidationError(_("Serial Number is required for RFID Reader."))
