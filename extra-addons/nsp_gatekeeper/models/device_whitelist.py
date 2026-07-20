# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class DeviceWhitelist(models.Model):
    _name = "nsp.device.whitelist"
    _description = "NSP Device Whitelist"
    _rec_name = "serial_number"
    _order = "serial_number, id"

    serial_number = fields.Char(string="Serial", required=True, index=True, copy=False)
    model_number = fields.Char(string="Model")
    device_vendor = fields.Char(string="Vendor")
    device_type = fields.Selection([
        ("rfid_reader", "RFID Reader"),
        ("camera", "Camera"),
        ("other", "Other"),
    ], string="Device Type", required=True, default="rfid_reader", index=True)

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
            vals["model_number"] = str(vals.get("model_number") or "").strip() or False
            vals["device_vendor"] = str(vals.get("device_vendor") or "").strip() or False
            prepared.append(vals)
        records = super().create(prepared)
        records._resolve_device_alerts()
        return records

    def write(self, vals):
        values = dict(vals)
        if "serial_number" in values:
            values["serial_number"] = self._normalize_serial(values.get("serial_number"))
        if "model_number" in values:
            values["model_number"] = str(values.get("model_number") or "").strip() or False
        if "device_vendor" in values:
            values["device_vendor"] = str(values.get("device_vendor") or "").strip() or False
        result = super().write(values)
        self._resolve_device_alerts()
        return result

    @api.constrains("serial_number")
    def _check_serial_number(self):
        for record in self:
            if not self._normalize_serial(record.serial_number):
                raise ValidationError(_("Serial is required."))

    @api.model
    def is_device_whitelisted(self, serial_number):
        serial = self._normalize_serial(serial_number)
        if not serial:
            return False
        return bool(self.sudo().search_count([("serial_number", "=", serial)]))

    def _resolve_device_alerts(self):
        """Archive existing not-whitelisted alerts after an administrator allows the Serial."""
        if "nsp.notification" not in self.env.registry.models:
            return True
        serials = set(self.mapped("serial_number"))
        if not serials:
            return True
        notifications = self.env["nsp.notification"].sudo().search([
            ("category", "=", "device_security"),
            ("device_serial", "in", list(serials)),
            ("state", "!=", "archived"),
        ])
        if notifications:
            notifications.write({"state": "archived", "active": False})
        return True
