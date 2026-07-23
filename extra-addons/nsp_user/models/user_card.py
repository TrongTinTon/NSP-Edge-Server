# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class NspUserCard(models.Model):
    _name = "nsp.user.card"
    _description = "NSP User Card Assignment"
    _rec_name = "display_name"
    _order = "user_id, state, assigned_at desc, id desc"

    display_name = fields.Char(string="Display Name", compute="_compute_display_name", store=True)
    user_id = fields.Many2one(
        "nsp.user", string="User", required=True, ondelete="cascade", index=True,
    )
    card_id = fields.Many2one(
        "nsp.rfid.card", string="Master Card", required=True,
        ondelete="cascade", index=True,
        domain=[("card_type", "=", "user_card"), ("usage_state", "=", "available")],
    )
    tid = fields.Char(string="TID", related="card_id.tid", readonly=True)
    card_type = fields.Selection(related="card_id.card_type", string="Card Type", readonly=True)
    state = fields.Selection([
        ("active", "Active"),
        ("revoked", "Revoked"),
    ], string="Status", default="active", required=True, index=True)
    assigned_at = fields.Datetime(string="Assigned At", default=fields.Datetime.now, readonly=True)
    revoked_at = fields.Datetime(string="Revoked At", readonly=True)
    note = fields.Text(string="Note")

    _sql_constraints = [
        ("user_card_unique", "unique(user_id, card_id)", "This card is already assigned to this user."),
    ]

    def init(self):
        self.env.cr.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS nsp_user_card_one_active_card_idx
                ON nsp_user_card (card_id)
             WHERE state = 'active'
            """
        )

    @api.depends("user_id.name", "card_id.tid")
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = "%s - %s" % (
                rec.user_id.name or _("User"), rec.card_id.tid or _("Card")
            )

    @api.constrains("card_id", "state")
    def _check_card_assignment(self):
        active = self.filtered(lambda rec: rec.card_id and rec.state == "active")
        for rec in self.filtered("card_id"):
            if rec.card_id.card_type != "user_card":
                raise ValidationError(_("User Cards must use a Master Card with type User Card."))
        if not active:
            return
        duplicate = self.search([
            ("card_id", "in", active.card_id.ids),
            ("state", "=", "active"),
            ("id", "not in", active.ids),
        ], limit=1)
        if duplicate:
            raise ValidationError(
                _("This user card is already active for user %s.") % duplicate.user_id.name
            )
        if "nsp.vehicle.card" in self.env.registry.models:
            vehicle_card = self.env["nsp.vehicle.card"].sudo().search([
                ("card_id", "in", active.card_id.ids), ("state", "=", "active")
            ], limit=1)
            if vehicle_card:
                raise ValidationError(
                    _("This card is already active for vehicle %s.") % vehicle_card.vehicle_id.license_plate
                )

    def action_revoke(self):
        self.write({"state": "revoked", "revoked_at": fields.Datetime.now()})
        return True

    def action_activate(self):
        self.write({"state": "active", "revoked_at": False})
        return True
