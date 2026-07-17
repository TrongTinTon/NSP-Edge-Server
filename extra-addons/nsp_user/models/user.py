# -*- coding: utf-8 -*-
import uuid

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class NspUser(models.Model):
    _name = "nsp.user"
    _description = "NSP User"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _rec_name = "display_name"
    _order = "user_code, name, id"

    display_name = fields.Char(string="Display Name", compute="_compute_display_name", store=True)
    user_code = fields.Char(string="User Code", required=True, copy=False, index=True, tracking=True, help="Stable user code synced to Controller. This replaces HR Code and does not depend on Odoo HR.")
    name = fields.Char(string="User Name", required=True, tracking=True)
    active = fields.Boolean(default=True, tracking=True, index=True)
    pin = fields.Char(string="PIN", copy=False, tracking=True)
    email = fields.Char(string="Email")
    phone = fields.Char(string="Phone")
    note = fields.Text(string="Note")
    user_card_ids = fields.One2many("nsp.user.card", "user_id", string="User Cards", help="All cards assigned to this user. Only Active cards are synced to Controller.")
    user_rfid_tid = fields.Char(string="Primary Active User TID", compute="_compute_card_tids", store=False, readonly=True)
    user_rfid_tids = fields.Char(string="All Active User TIDs", compute="_compute_card_tids", store=False, readonly=True)
    active_user_card_count = fields.Integer(string="Active User Cards", compute="_compute_card_tids", store=False)

    _sql_constraints = [
        ("user_code_unique", "unique(user_code)", "User Code must be unique."),
    ]

    @api.depends("user_code", "name")
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = "[%s] %s" % (rec.user_code or "-", rec.name or "")

    @api.depends("user_card_ids.state", "user_card_ids.tid", "user_card_ids.card_id.tid")
    def _compute_card_tids(self):
        for rec in self:
            active_cards = rec.user_card_ids.filtered(lambda line: line.state == "active" and line.tid)
            tids = active_cards.mapped("tid")
            rec.user_rfid_tid = tids[0] if tids else False
            rec.user_rfid_tids = ",".join(tids) if tids else False
            rec.active_user_card_count = len(tids)

    @api.model
    def _normalize_code(self, value):
        return str(value or "").strip().upper()

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        generated_indexes = set()
        for idx, vals in enumerate(vals_list):
            vals = dict(vals)
            if vals.get("user_code"):
                vals["user_code"] = self._normalize_code(vals.get("user_code"))
            else:
                vals["user_code"] = "USER-TMP-%s" % uuid.uuid4().hex[:12].upper()
                generated_indexes.add(idx)
            prepared.append(vals)
        records = super().create(prepared)
        for idx, rec in enumerate(records):
            if idx in generated_indexes:
                rec.user_code = "USER-%06d" % rec.id
        return records

    def write(self, vals):
        if vals.get("user_code"):
            vals = dict(vals)
            vals["user_code"] = self._normalize_code(vals.get("user_code"))
        return super().write(vals)

    @api.constrains("user_code")
    def _check_user_code(self):
        for rec in self:
            if not rec._normalize_code(rec.user_code):
                raise ValidationError(_("User Code is required."))

    def action_archive(self):
        self.write({"active": False})
        return True

    def action_unarchive(self):
        self.write({"active": True})
        return True
