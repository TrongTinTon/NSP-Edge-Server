# -*- coding: utf-8 -*-
from collections import defaultdict

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.addons.nsp_core.utils import new_management_code


class NspUser(models.Model):
    _name = "nsp.user"
    _description = "NSP User"
    _inherit = ["mail.thread", "image.mixin"]
    _rec_name = "name"
    _order = "name, id"

    user_code = fields.Char(
        string="Technical Code",
        required=True,
        readonly=True,
        copy=False,
        index=True,
        default=lambda self: new_management_code("USER"),
        help="Stable system-generated identifier used for Cloud/Edge synchronization.",
    )
    name = fields.Char(string="User Name", required=True, tracking=True, index=True)
    active = fields.Boolean(default=True, tracking=True, index=True)
    email = fields.Char(string="Email", index=True)
    phone = fields.Char(string="Phone", index=True)
    note = fields.Text(string="Note")

    web_user_id = fields.Many2one(
        "res.users",
        string="Odoo Web Account",
        copy=False,
        tracking=True,
        ondelete="set null",
        domain=[("share", "=", False)],
        groups="base.group_system",
        help=(
            "Optional Odoo backend account for this NSP business user. "
            "Web login, groups, ACLs and record rules remain owned by res.users. "
            "Mobile authentication and NSP business ownership remain on nsp.user."
        ),
    )

    friendship_sent_ids = fields.One2many(
        "nsp.user.friendship", "requester_id", string="Sent Friend Requests"
    )
    friendship_received_ids = fields.One2many(
        "nsp.user.friendship", "addressee_id", string="Received Friend Requests"
    )
    accepted_friendship_ids = fields.Many2many(
        "nsp.user.friendship",
        compute="_compute_accepted_friendships",
        string="Accepted Friendships",
    )

    _sql_constraints = [
        ("user_code_unique", "unique(user_code)", "User Technical Code must be unique."),
        (
            "web_user_unique",
            "unique(web_user_id)",
            "An Odoo Web Account can be linked to only one NSP User.",
        ),
    ]

    @api.model
    def _normalize_code(self, value):
        return str(value or "").strip().upper()

    @api.model
    def _normalize_email(self, value):
        return str(value or "").strip().lower() or False

    @api.model
    def _normalize_phone(self, value):
        return str(value or "").strip() or False

    @api.depends(
        "friendship_sent_ids.state",
        "friendship_received_ids.state",
        "friendship_sent_ids.accepted_at",
        "friendship_received_ids.accepted_at",
    )
    def _compute_accepted_friendships(self):
        mapped = defaultdict(list)
        persisted_ids = [rec.id for rec in self if isinstance(rec.id, int)]
        if persisted_ids:
            friendships = self.env["nsp.user.friendship"].sudo().search([
                ("state", "=", "accepted"),
                "|",
                ("requester_id", "in", persisted_ids),
                ("addressee_id", "in", persisted_ids),
            ], order="accepted_at desc, id desc")
            wanted = set(persisted_ids)
            for friendship in friendships:
                if friendship.requester_id.id in wanted:
                    mapped[friendship.requester_id.id].append(friendship.id)
                if friendship.addressee_id.id in wanted:
                    mapped[friendship.addressee_id.id].append(friendship.id)

        Friendship = self.env["nsp.user.friendship"]
        for rec in self:
            rec.accepted_friendship_ids = Friendship.browse(mapped.get(rec.id, []))

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for source in vals_list:
            vals = dict(source)
            vals["user_code"] = self._normalize_code(
                vals.get("user_code") or new_management_code("USER")
            )
            if "email" in vals:
                vals["email"] = self._normalize_email(vals.get("email"))
            if "phone" in vals:
                vals["phone"] = self._normalize_phone(vals.get("phone"))
            prepared.append(vals)
        return super().create(prepared)

    def write(self, vals):
        values = dict(vals)
        if "user_code" in values:
            normalized = self._normalize_code(values.get("user_code"))
            if any(rec.user_code and rec.user_code != normalized for rec in self):
                raise ValidationError(_("User Technical Code cannot be changed after creation."))
            values["user_code"] = normalized
        if "email" in values:
            values["email"] = self._normalize_email(values.get("email"))
        if "phone" in values:
            values["phone"] = self._normalize_phone(values.get("phone"))
        return super().write(values)

    @api.constrains("user_code")
    def _check_user_code(self):
        for rec in self:
            if not rec._normalize_code(rec.user_code):
                raise ValidationError(_("User Technical Code is required."))

    @api.constrains("web_user_id")
    def _check_web_user(self):
        for rec in self:
            if rec.web_user_id and rec.web_user_id.share:
                raise ValidationError(
                    _("Odoo Web Account must be an internal user, not a portal/shared user.")
                )

    def action_open_web_user(self):
        self.ensure_one()
        if not self.env.user.has_group("base.group_system"):
            raise AccessError(_("Only Odoo administrators can manage Odoo Web Accounts."))
        if not self.web_user_id:
            raise UserError(_("This NSP User is not linked to an Odoo Web Account."))
        return {
            "type": "ir.actions.act_window",
            "name": _("Odoo Web Account"),
            "res_model": "res.users",
            "view_mode": "form",
            "res_id": self.web_user_id.id,
            "target": "current",
        }

    def action_archive(self):
        self.filtered("active").write({"active": False})
        return True

    def action_unarchive(self):
        self.filtered(lambda rec: not rec.active).write({"active": True})
        return True
