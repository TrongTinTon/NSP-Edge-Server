# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class NspUserCard(models.Model):
    _name = "nsp.user.card"
    _description = "NSP User Card Assignment"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _rec_name = "display_name"
    _order = "user_id, state, assigned_at desc, id desc"

    display_name = fields.Char(string="Display Name", compute="_compute_display_name", store=True)
    user_id = fields.Many2one("nsp.user", string="User", required=True, ondelete="cascade", index=True, tracking=True)
    card_id = fields.Many2one(
        "nsp.rfid.card",
        string="Master Card",
        required=True,
        ondelete="cascade",
        domain=[("card_type", "=", "user_card"), ("usage_state", "=", "available")],
        tracking=True,
    )
    tid = fields.Char(string="TID", related="card_id.tid", store=False, readonly=True)
    card_type = fields.Selection(related="card_id.card_type", string="Card Type", store=False, readonly=True)
    state = fields.Selection([
        ("active", "Active"),
        ("revoked", "Revoked"),
    ], string="Status", default="active", required=True, tracking=True, index=True)
    assigned_at = fields.Datetime(string="Assigned At", default=fields.Datetime.now, tracking=True)
    revoked_at = fields.Datetime(string="Revoked At", readonly=True, tracking=True)
    note = fields.Text(string="Note")

    _sql_constraints = [
        ("user_card_unique", "unique(user_id, card_id)", "This card is already assigned to this user."),
    ]

    @api.depends("user_id", "user_id.name", "card_id", "card_id.tid", "state")
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = "%s - %s" % (rec.user_id.display_name or rec.user_id.name or _("User"), rec.tid or _("Card"))

    @api.constrains("card_id", "state")
    def _check_card_assignment(self):
        for rec in self:
            if not rec.card_id:
                continue
            if rec.card_id.card_type != "user_card":
                raise ValidationError(_("User Cards must be selected from Master Cards with type User Card."))
            if rec.state == "active":
                other_user = self.search([("card_id", "=", rec.card_id.id), ("state", "=", "active"), ("id", "!=", rec.id)], limit=1)
                if other_user:
                    raise ValidationError(_("This user card is already active for user %s.") % (other_user.user_id.display_name or other_user.user_id.name))
                if "nsp.vehicle.card" in self.env:
                    vehicle_card = self.env["nsp.vehicle.card"].sudo().search([("card_id", "=", rec.card_id.id), ("state", "=", "active")], limit=1)
                    if vehicle_card:
                        raise ValidationError(_("This card is already active for vehicle %s.") % (vehicle_card.vehicle_id.display_name or vehicle_card.vehicle_id.license_plate))

    def action_revoke(self):
        self.write({"state": "revoked", "revoked_at": fields.Datetime.now()})

    def action_activate(self):
        self.write({"state": "active", "revoked_at": False})
