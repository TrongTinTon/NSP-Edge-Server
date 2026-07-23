# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class NspUserFriendship(models.Model):
    _name = "nsp.user.friendship"
    _description = "NSP User Friendship"
    _order = "id desc"
    _rec_name = "name"

    name = fields.Char(compute="_compute_name", store=True)
    requester_id = fields.Many2one("nsp.user", string="Requester", required=True, index=True, ondelete="cascade")
    addressee_id = fields.Many2one("nsp.user", string="Friend", required=True, index=True, ondelete="cascade")
    pair_key = fields.Char(required=True, copy=False, readonly=True, index=True)
    state = fields.Selection([
        ("pending", "Pending"),
        ("accepted", "Accepted"),
        ("cancelled", "Cancelled"),
    ], default="pending", required=True, index=True)
    accepted_at = fields.Datetime(readonly=True)

    _sql_constraints = [
        ("friendship_pair_unique", "unique(pair_key)", "A friendship already exists between these users."),
    ]


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
            prepared.append(vals)
        return super().create(prepared)

    @api.constrains("requester_id", "addressee_id")
    def _check_users(self):
        for rec in self:
            if rec.requester_id == rec.addressee_id:
                raise ValidationError(_("A user cannot add themselves as a friend."))

    def action_accept(self):
        for rec in self:
            if rec.state != "accepted":
                rec.write({"state": "accepted", "accepted_at": fields.Datetime.now()})
        return True

    def action_cancel(self):
        self.write({"state": "cancelled", "accepted_at": False})
        return True

    @api.model
    def are_friends(self, user_a, user_b):
        if not user_a or not user_b or user_a == user_b:
            return False
        key = self._make_pair_key(user_a.id, user_b.id)
        return bool(self.sudo().search_count([("pair_key", "=", key), ("state", "=", "accepted")]))

    @api.model
    def accepted_friends(self, user):
        if not user:
            return self.env["nsp.user"].browse()
        friendships = self.sudo().search([
            ("state", "=", "accepted"),
            "|", ("requester_id", "=", user.id), ("addressee_id", "=", user.id),
        ])
        friend_ids = set(friendships.mapped("requester_id").ids + friendships.mapped("addressee_id").ids)
        friend_ids.discard(user.id)
        return self.env["nsp.user"].sudo().browse(sorted(friend_ids))
