# -*- coding: utf-8 -*-
from collections import defaultdict

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.addons.nsp_core.utils import (
    new_management_code,
    strip_empty_x2many_create_commands,
)


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

    user_card_ids = fields.One2many(
        "nsp.user.card",
        "user_id",
        string="User Cards",
        help="RFID cards assigned to this user. Only active assignments are synchronized.",
    )
    user_rfid_tid = fields.Char(
        string="Primary Active User TID",
        compute="_compute_card_summary",
        readonly=True,
    )
    active_user_card_count = fields.Integer(
        string="Active User Cards",
        compute="_compute_card_summary",
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

    @api.depends("user_card_ids.state", "user_card_ids.card_id.tid", "user_card_ids.assigned_at")
    def _compute_card_summary(self):
        """Compute all user card summaries with one assignment query for the record batch."""
        summary = defaultdict(list)
        persisted_ids = [rec.id for rec in self if isinstance(rec.id, int)]
        if persisted_ids:
            lines = self.env["nsp.user.card"].sudo().search([
                ("user_id", "in", persisted_ids),
                ("state", "=", "active"),
                ("card_id.tid", "!=", False),
            ], order="user_id, assigned_at desc, id desc")
            for line in lines:
                if line.user_id and line.tid:
                    summary[line.user_id.id].append(line.tid)

        for rec in self:
            tids = summary.get(rec.id, []) if isinstance(rec.id, int) else []
            rec.user_rfid_tid = tids[0] if tids else False
            rec.active_user_card_count = len(tids)

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

    def _sanitize_user_card_commands(self, commands):
        commands = strip_empty_x2many_create_commands(
            commands,
            required_field="card_id",
            ignored_fields={
                "user_id",
                "state",
                "assigned_at",
                "revoked_at",
            },
        )
        if not commands:
            return commands

        assignments = self.mapped("user_card_ids") if self else self.env["nsp.user.card"]
        removed_ids = {
            int(command[1])
            for command in commands
            if isinstance(command, (list, tuple))
            and len(command) > 1
            and command[0] in (2, 3)
            and command[1]
        }
        existing_cards = assignments.filtered(lambda rec: rec.id not in removed_ids).mapped("card_id")
        seen_card_ids = set(existing_cards.ids)
        seen_tids = {
            self.env["nsp.rfid.card"]._normalize_tid(tid)
            for tid in existing_cards.mapped("tid")
            if tid
        }

        cleaned = []
        Card = self.env["nsp.rfid.card"]
        for command in commands:
            if not isinstance(command, (list, tuple)) or len(command) < 3 or command[0] != 0:
                cleaned.append(command)
                continue

            values = command[2] if isinstance(command[2], dict) else {}
            card_id = values.get("card_id")
            if isinstance(card_id, (list, tuple)):
                card_id = card_id[0] if card_id else False
            card_id = int(card_id) if card_id else False
            tid = Card._normalize_tid(values.get("scan_tid"))
            if not card_id and tid:
                card = Card.search([("tid", "=", tid)], limit=1)
                card_id = card.id or False

            if (card_id and card_id in seen_card_ids) or (tid and tid in seen_tids):
                continue

            cleaned.append(command)
            if card_id:
                seen_card_ids.add(card_id)
                card = Card.browse(card_id)
                if card.exists() and card.tid:
                    seen_tids.add(Card._normalize_tid(card.tid))
            if tid:
                seen_tids.add(tid)

        return cleaned

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for source in vals_list:
            vals = dict(source)
            vals["user_code"] = self._normalize_code(
                vals.get("user_code") or new_management_code("USER")
            )
            if "user_card_ids" in vals:
                vals["user_card_ids"] = self._sanitize_user_card_commands(
                    vals.get("user_card_ids")
                )
            if "email" in vals:
                vals["email"] = self._normalize_email(vals.get("email"))
            if "phone" in vals:
                vals["phone"] = self._normalize_phone(vals.get("phone"))
            prepared.append(vals)
        return super().create(prepared)

    def write(self, vals):
        values = dict(vals)
        if "user_card_ids" in values:
            values["user_card_ids"] = self._sanitize_user_card_commands(
                values.get("user_card_ids")
            )
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
        active = self.filtered("active")
        if active:
            active.write({"active": False})
        return True

    def action_unarchive(self):
        archived = self.filtered(lambda rec: not rec.active)
        if archived:
            archived.write({"active": True})
        return True
