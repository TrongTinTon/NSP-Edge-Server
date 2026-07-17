# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError
from odoo.addons.t4_coreapi.utils import endpoint, get_params, get_body


class NspBranch(models.Model):
    _name = "nsp.branch"
    _description = "NSP Branch"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _rec_name = "name"
    _order = "code, name, id"

    name = fields.Char(string="Branch Name", required=True, tracking=True)
    code = fields.Char(string="Branch Code", required=True, index=True, tracking=True, copy=False)
    status = fields.Selection([
        ("active", "Active"),
        ("inactive", "Inactive"),
    ], string="Status", default="active", required=True, tracking=True, index=True)
    timezone = fields.Char(
        string="Timezone",
        default="Asia/Ho_Chi_Minh",
        required=True,
        tracking=True,
        help="Branch-level IANA timezone used by all gates and controllers at this branch.",
    )
    note = fields.Text(string="Note")
    gate_ids = fields.One2many("nsp.gate", "branch_id", string="Gates", readonly=True)
    gate_count = fields.Integer(string="Gates", compute="_compute_gate_count")

    _sql_constraints = [
        ("code_unique", "unique(code)", "Branch Code must be unique."),
    ]

    @api.depends("gate_ids")
    def _compute_gate_count(self):
        for rec in self:
            rec.gate_count = len(rec.gate_ids)

    @api.model
    def _normalize_code(self, value):
        return str(value or "").strip().upper()

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("code"):
                vals["code"] = self._normalize_code(vals.get("code"))
            if not vals.get("timezone"):
                vals["timezone"] = "Asia/Ho_Chi_Minh"
        records = super().create(vals_list)
        return records

    def write(self, vals):
        vals = dict(vals)
        if vals.get("code"):
            vals["code"] = self._normalize_code(vals.get("code"))
        res = super().write(vals)
        if {"code", "name", "timezone"}.intersection(vals.keys()):
            gates = self.mapped("gate_ids")
            if gates and hasattr(gates, "_refresh_config_hash"):
                gates._refresh_config_hash(bump_if_changed=True)
        return res

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

    @api.model
    def _table_exists(self, table_name):
        self.env.cr.execute("SELECT to_regclass(%s)", (table_name,))
        return bool(self.env.cr.fetchone()[0])

    @api.model
    def get_default_branch(self):
        """Return/create the default Branch only after the table is available."""
        Branch = self.sudo()
        if not Branch._table_exists("nsp_branch"):
            return Branch.browse()
        branch = Branch.search([("code", "=", "DEFAULT")], limit=1)
        if branch:
            return branch
        return Branch.create({
            "name": "Default Branch",
            "code": "DEFAULT",
            "status": "active",
            "timezone": "Asia/Ho_Chi_Minh",
            "note": "Automatically created as the default NSP branch.",
        })
