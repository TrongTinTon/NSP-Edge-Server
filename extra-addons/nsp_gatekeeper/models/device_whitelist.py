# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

class DeviceWhitelist(models.Model):
    _name = "nsp.device.whitelist"
    _description = "NSP Device Whitelist"
    _rec_name = "display_name"
    _order = "approval_state, serial_number, id"

    display_name = fields.Char(string="Device", compute="_compute_display_name", store=True)
    device_code = fields.Char(string="Device Code", index=True)
    device_name = fields.Char(string="Device Name")
    device_type = fields.Selection([
        ("rfid_reader", "RFID Reader"),
        ("camera", "Camera"),
        ("other", "Other"),
    ], string="Device Type", help="Optional. Leave blank when the controller did not report a type or Admin has not classified it yet.")
    model_number = fields.Char(string="Model Number")
    device_vendor = fields.Char(string="Vendor")
    serial_number = fields.Char(string="Serial", required=True, index=True)
    antennas = fields.Char(string="Antennas")
    controller_ref = fields.Char(string="Controller")
    source_last_seen = fields.Datetime(string="Device Last Seen", readonly=True)
    imported_from_gatekeeper = fields.Boolean(string="Imported from Gatekeeper", readonly=True)

    approval_state = fields.Selection([
        ("pending", "Pending Review"),
        ("valid", "Valid Device"),
        ("invalid", "Invalid Device"),
    ], string="Whitelist Status", default="pending", required=True, tracking=True, index=True)
    note = fields.Text(string="Note")
    approved_by = fields.Many2one("res.users", string="Approved By", readonly=True)
    approved_at = fields.Datetime(string="Approved At", readonly=True)

    _sql_constraints = [
        ("serial_number_unique", "unique(serial_number)", "Serial number must be unique in Device Whitelist."),
    ]

    @api.depends("serial_number", "device_name")
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = rec.device_name or rec.serial_number or _("Device")

    @api.model
    def _normalize_serial(self, value):
        return str(value or "").strip().upper()

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("serial_number"):
                vals["serial_number"] = self._normalize_serial(vals.get("serial_number"))
        return super().create(vals_list)

    def write(self, vals):
        if vals.get("serial_number"):
            vals = dict(vals)
            vals["serial_number"] = self._normalize_serial(vals.get("serial_number"))
        return super().write(vals)

    @api.constrains("serial_number")
    def _check_serial_number_unique(self):
        for record in self:
            if not record._normalize_serial(record.serial_number):
                raise ValidationError(_("Serial number is required."))

    def action_mark_valid(self):
        self.write({
            "approval_state": "valid",
            "approved_by": self.env.user.id,
            "approved_at": fields.Datetime.now(),
        })
        return True

    def action_mark_invalid(self):
        self.write({
            "approval_state": "invalid",
            "approved_by": self.env.user.id,
            "approved_at": fields.Datetime.now(),
        })
        return True

    def action_reset_pending(self):
        self.write({"approval_state": "pending", "approved_by": False, "approved_at": False})
        return True

    @api.model
    def _vals_from_gatekeeper_device(self, device):
        return {
            "device_code": getattr(device, "device_code", False),
            "device_name": getattr(device, "device_name", False),
            "device_type": getattr(getattr(device, "device_type_id", False), "code", False) or False,
            "model_number": getattr(device, "model_number", False),
            "device_vendor": getattr(device, "device_vendor", False),
            "serial_number": getattr(device, "serial_number", False),
            "antennas": str(getattr(device, "antennas", "") or ""),
            "controller_ref": getattr(getattr(device, "controller_id", False), "display_name", False) or getattr(getattr(device, "controller_id", False), "controller_id", False),
            "source_last_seen": getattr(device, "last_seen", False),
            "imported_from_gatekeeper": True,
        }

    @api.model
    def action_import_from_gatekeeper_devices(self):
        """Import/update whitelist candidates from NSP Gatekeeper devices when the model exists."""
        try:
            Device = self.env["nsp.device"].sudo()
        except Exception:
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {"title": _("NSP Gatekeeper"), "message": _("NSP Gatekeeper device model is not installed yet."), "type": "warning"},
            }
        created = updated = 0
        for device in Device.search([]):
            vals = self._vals_from_gatekeeper_device(device)
            serial = vals.get("serial_number")
            if not serial:
                continue
            rec = self.search([("serial_number", "=", serial)], limit=1)
            if rec:
                # Do not reset approval decision while refreshing metadata.
                vals.pop("approval_state", None)
                rec.write(vals)
                updated += 1
            else:
                vals["approval_state"] = "pending"
                self.create(vals)
                created += 1
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("NSP Gatekeeper"),
                "message": _("Imported %(created)s new device(s), updated %(updated)s device(s).") % {"created": created, "updated": updated},
                "type": "success",
            },
        }

    @api.model
    def is_device_valid(self, serial_number=None, device_code=None):
        domain = []
        if serial_number:
            domain = [("serial_number", "=", serial_number)]
        elif device_code:
            domain = [("device_code", "=", device_code)]
        else:
            return False
        rec = self.sudo().search(domain, limit=1)
        return bool(rec and rec.approval_state == "valid")
