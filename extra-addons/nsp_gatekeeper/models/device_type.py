# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class NspDeviceType(models.Model):
    _name = "nsp.device.type"
    _description = "NSP Device Type"
    _rec_name = "name"
    _order = "sequence, name, id"

    name = fields.Char(string="Device Type", required=True, translate=True)
    code = fields.Char(string="Code", required=True, index=True, copy=False)
    category = fields.Selection([
        ("rfid_reader", "RFID Reader"),
        ("camera", "Camera"),
        ("other", "Other"),
    ], string="Category", default="other", required=True, index=True)
    supports_antenna_mapping = fields.Boolean(
        string="Supports Antenna Mapping",
        help="Enable this for RFID readers that can be selected in Parking Lane antenna mappings.",
    )
    active = fields.Boolean(default=True)
    sequence = fields.Integer(default=10)

    _sql_constraints = [
        ("code_unique", "unique(code)", "Device Type Code must be unique."),
    ]

    @api.model
    def _normalize_code(self, value):
        return str(value or "").strip().lower().replace(" ", "_")

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("code"):
                vals["code"] = self._normalize_code(vals.get("code"))
            elif vals.get("name"):
                vals["code"] = self._normalize_code(vals.get("name"))
        return super().create(vals_list)

    def write(self, vals):
        if vals.get("code"):
            vals = dict(vals)
            vals["code"] = self._normalize_code(vals.get("code"))
        return super().write(vals)

    @api.constrains("code")
    def _check_code(self):
        for rec in self:
            if not rec._normalize_code(rec.code):
                raise ValidationError(_("Device Type Code is required."))

    @api.model
    def find_by_reported_value(self, value):
        """Resolve a controller-reported type without auto-creating master data.

        If the controller does not report a type, or reports a value not configured by Admin,
        return an empty recordset. The Device keeps Device Type blank and Admin can set it later.
        """
        raw = self._normalize_code(value)
        if not raw:
            return self.browse()
        return self.search(["|", ("code", "=", raw), ("name", "=ilike", str(value or "").strip())], limit=1)
