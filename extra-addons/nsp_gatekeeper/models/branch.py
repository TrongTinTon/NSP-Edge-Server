# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError
from odoo.addons.nsp_core.utils import new_management_code


class NspBranch(models.Model):
    _name = "nsp.branch"
    _description = "NSP Branch"
    _rec_name = "name"
    _order = "code, name, id"

    name = fields.Char(string="Branch Name", required=True)
    code = fields.Char(
        string="Branch Code", required=True, readonly=True, index=True, copy=False,
        default=lambda self: new_management_code("BRN"),
    )
    status = fields.Selection([
        ("active", "Active"),
        ("inactive", "Inactive"),
    ], string="Status", default="active", required=True, index=True)
    timezone = fields.Char(
        string="Timezone",
        default="Asia/Ho_Chi_Minh",
        required=True,
        help="Branch-level IANA timezone used for parking operations at this branch.",
    )
    note = fields.Text(string="Note")
    parking_area_ids = fields.One2many("nsp.parking.area", "branch_id", string="Parking Areas", readonly=True)
    parking_area_count = fields.Integer(string="Parking Areas", compute="_compute_parking_area_count")

    _sql_constraints = [
        ("code_unique", "unique(code)", "Branch Code must be unique."),
    ]

    @api.depends("parking_area_ids")
    def _compute_parking_area_count(self):
        for rec in self:
            rec.parking_area_count = len(rec.parking_area_ids)

    @api.model
    def _normalize_code(self, value):
        return str(value or "").strip().upper()

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            vals["code"] = self._normalize_code(
                vals.get("code") or new_management_code("BRN")
            )
            if not vals.get("timezone"):
                vals["timezone"] = "Asia/Ho_Chi_Minh"
        records = super().create(vals_list)
        return records

    def write(self, vals):
        vals = dict(vals)
        if vals.get("code"):
            vals["code"] = self._normalize_code(vals.get("code"))
        return super().write(vals)

    @api.constrains("code")
    def _check_code(self):
        for rec in self:
            if not rec._normalize_code(rec.code):
                raise ValidationError(_("Branch Code is required."))

    @api.constrains("timezone")
    def _check_timezone(self):
        for rec in self:
            tz = str(rec.timezone or "").strip()
            if not tz or (tz != "UTC" and "/" not in tz):
                raise ValidationError(_("Branch Timezone must be an IANA value, for example Asia/Ho_Chi_Minh."))
