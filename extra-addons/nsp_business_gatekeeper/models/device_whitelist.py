# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


VALID_DEVICE_TYPE_CODES = {"SERVER", "CONTROLLER", "RFID_READER", "ANTENNA"}



class DeviceWhitelist(models.Model):
    """Cloud-authoritative device inventory cached on Edge."""

    _name = "nsp.device.whitelist"
    _description = "NSP Device Whitelist"
    _rec_name = "display_name"
    _order = "device_type_id, name, technical_code, id"

    name = fields.Char(string="Device Name", required=True, index=True)
    display_name = fields.Char(compute="_compute_display_name", store=True)
    active = fields.Boolean(default=True, index=True)
    device_type_id = fields.Many2one(
        "nsp.device.type", string="Device Type", required=True, ondelete="restrict", index=True
    )
    device_type_code = fields.Char(related="device_type_id.code", store=True, readonly=True)
    technical_code = fields.Char(required=True, readonly=True, copy=False, index=True)
    serial_number = fields.Char(string="Serial Number", index=True, copy=False)
    parent_id = fields.Many2one(
        "nsp.device.whitelist", string="Parent Device", ondelete="restrict", index=True
    )
    child_ids = fields.One2many("nsp.device.whitelist", "parent_id", string="Connected Devices")
    antenna_no = fields.Integer(string="Antenna No.")
    model_id = fields.Many2one("nsp.reference.model", string="Model", ondelete="set null", index=True)
    vendor_id = fields.Many2one("nsp.reference.vendor", string="Vendor", ondelete="set null", index=True)
    connection_type = fields.Selection(
        [
            ("usb", "USB"), ("rs232", "RS-232"), ("rs485", "RS-485"),
            ("ethernet", "Ethernet (RJ45)"), ("wiegand", "Wiegand"),
            ("bluetooth", "Bluetooth"), ("wifi", "Wi-Fi"), ("cellular", "4G/5G"),
        ],
        string="Physical Connection",
    )
    tid_addr = fields.Integer(string="TID Start Address (Words)", default=0)
    tid_len = fields.Integer(string="TID Length (Words)", default=6)

    _sql_constraints = [
        ("device_whitelist_technical_code_unique", "unique(technical_code)", "Technical Code must be unique."),
        ("device_whitelist_serial_unique", "unique(serial_number)", "Serial Number must be unique."),
    ]

    @api.depends("name", "device_type_id.name", "serial_number", "technical_code", "antenna_no")
    def _compute_display_name(self):
        for record in self:
            identity = record.serial_number or record.technical_code or ""
            if record.device_type_code == "ANTENNA" and record.antenna_no:
                identity = _("Antenna %s") % record.antenna_no
            record.display_name = "%s · %s · %s" % (
                record.device_type_id.name or _("Device"), record.name or _("Unnamed"), identity
            )

    @api.model
    def _normalize_serial(self, value):
        normalized = str(value or "").strip().upper()
        return normalized or False

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for source in vals_list:
            vals = dict(source)
            vals["name"] = str(vals.get("name") or vals.get("technical_code") or _("Device")).strip()
            vals["technical_code"] = str(vals.get("technical_code") or "").strip().upper()
            vals["serial_number"] = self._normalize_serial(vals.get("serial_number"))
            prepared.append(vals)
        return super().create(prepared)

    def write(self, vals):
        values = dict(vals)
        if "name" in values:
            values["name"] = str(values.get("name") or "").strip()
        if "technical_code" in values:
            values["technical_code"] = str(values.get("technical_code") or "").strip().upper()
        if "serial_number" in values:
            values["serial_number"] = self._normalize_serial(values.get("serial_number"))
        return super().write(values)

    @api.constrains("device_type_id", "technical_code")
    def _check_device_definition(self):
        for record in self:
            code = record.device_type_code
            if code not in VALID_DEVICE_TYPE_CODES:
                raise ValidationError(_("Unsupported Device Type: %s") % (record.device_type_id.name or code))
            if not (record.technical_code or "").strip():
                raise ValidationError(_("Technical Code is required."))
