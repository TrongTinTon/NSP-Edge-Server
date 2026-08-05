# -*- coding: utf-8 -*-
from collections import defaultdict

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class NspUserFriendship(models.Model):
    _name = "nsp.user.friendship"
    _description = "NSP User Friendship"
    _order = "id desc"
    _rec_name = "name"

    name = fields.Char(compute="_compute_name", store=True)
    requester_id = fields.Many2one(
        "nsp.user", string="Requester", required=True, index=True, ondelete="cascade"
    )
    addressee_id = fields.Many2one(
        "nsp.user", string="Friend", required=True, index=True, ondelete="cascade"
    )
    pair_key = fields.Char(required=True, copy=False, readonly=True, index=True)
    state = fields.Selection([
        ("pending", "Pending"),
        ("accepted", "Accepted"),
    ], default="pending", required=True, readonly=True, index=True)
    accepted_at = fields.Datetime(readonly=True, index=True)

    _sql_constraints = [
        ("friendship_pair_unique", "unique(pair_key)", "A friendship already exists between these users."),
    ]

    def init(self):
        self.env.cr.execute(
            """
            CREATE INDEX IF NOT EXISTS nsp_user_friendship_requester_state_idx
                ON nsp_user_friendship (requester_id, state, id DESC)
            """
        )
        self.env.cr.execute(
            """
            CREATE INDEX IF NOT EXISTS nsp_user_friendship_addressee_state_idx
                ON nsp_user_friendship (addressee_id, state, id DESC)
            """
        )

    @api.depends("requester_id.name", "addressee_id.name")
    def _compute_name(self):
        for rec in self:
            rec.name = "%s ↔ %s" % (
                rec.requester_id.name or _("User"),
                rec.addressee_id.name or _("User"),
            )

    @api.model
    def _make_pair_key(self, user_a_id, user_b_id):
        a, b = sorted((int(user_a_id), int(user_b_id)))
        return "%s:%s" % (a, b)

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for source in vals_list:
            vals = dict(source)
            requester_id = int(vals.get("requester_id") or 0)
            addressee_id = int(vals.get("addressee_id") or 0)
            if not requester_id or not addressee_id:
                raise ValidationError(_("Requester and Friend are required."))
            if requester_id == addressee_id:
                raise ValidationError(_("A user cannot add themselves as a friend."))
            vals["pair_key"] = self._make_pair_key(requester_id, addressee_id)
            # A new relationship always starts as a request. Acceptance must use action_accept().
            vals["state"] = "pending"
            vals["accepted_at"] = False
            prepared.append(vals)
        return super().create(prepared)

    @api.constrains("requester_id", "addressee_id")
    def _check_users(self):
        for rec in self:
            if rec.requester_id == rec.addressee_id:
                raise ValidationError(_("A user cannot add themselves as a friend."))

    def action_accept(self):
        pending = self.filtered(lambda rec: rec.state == "pending")
        if pending:
            pending.write({"state": "accepted", "accepted_at": fields.Datetime.now()})
        return True

    def action_cancel(self):
        """Decline a pending request or remove an existing friendship."""
        self.unlink()
        return True

    @api.model
    def accepted_friends_map(self, users):
        """Return {user_id: [friend_user_ids]} with one friendship query."""
        users = users.exists()
        result = {user_id: [] for user_id in users.ids}
        if not users:
            return result
        user_ids = set(users.ids)
        friendships = self.sudo().search([
            ("state", "=", "accepted"),
            "|",
            ("requester_id", "in", list(user_ids)),
            ("addressee_id", "in", list(user_ids)),
        ])
        mapped = defaultdict(set)
        for friendship in friendships:
            a = friendship.requester_id.id
            b = friendship.addressee_id.id
            if a in user_ids:
                mapped[a].add(b)
            if b in user_ids:
                mapped[b].add(a)
        for user_id in result:
            result[user_id] = sorted(mapped.get(user_id, set()))
        return result
