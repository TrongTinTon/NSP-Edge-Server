# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class NspVehicleCard(models.Model):
    _name = "nsp.vehicle.card"
    _description = "NSP Vehicle Card Assignment"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _rec_name = "display_name"
    _order = "vehicle_id, state, assigned_at desc, id desc"

    display_name = fields.Char(string="Display Name", compute="_compute_display_name", store=True)
    vehicle_id = fields.Many2one("nsp.vehicle", string="Vehicle", required=True, ondelete="cascade", index=True, tracking=True)
    card_id = fields.Many2one(
        "nsp.rfid.card",
        string="Master Card",
        required=True,
        ondelete="cascade",
        domain=[("card_type", "=", "vehicle_card"), ("usage_state", "=", "available")],
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
        ("vehicle_card_unique", "unique(vehicle_id, card_id)", "This card is already assigned to this vehicle."),
    ]

    @api.depends("vehicle_id", "vehicle_id.license_plate", "card_id", "card_id.tid", "state")
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = "%s - %s" % (rec.vehicle_id.license_plate or rec.vehicle_id.display_name or _("Vehicle"), rec.tid or _("Card"))

    @api.constrains("card_id", "state")
    def _check_card_assignment(self):
        for rec in self:
            if not rec.card_id:
                continue
            if rec.card_id.card_type != "vehicle_card":
                raise ValidationError(_("Vehicle Cards must be selected from Master Cards with type Vehicle Card."))
            if rec.state == "active":
                other_vehicle = self.search([("card_id", "=", rec.card_id.id), ("state", "=", "active"), ("id", "!=", rec.id)], limit=1)
                if other_vehicle:
                    raise ValidationError(_("This vehicle card is already active for vehicle %s.") % (other_vehicle.vehicle_id.license_plate or other_vehicle.vehicle_id.display_name))
                user_card = self.env["nsp.user.card"].sudo().search([("card_id", "=", rec.card_id.id), ("state", "=", "active")], limit=1)
                if user_card:
                    raise ValidationError(_("This card is already active for user %s.") % (user_card.user_id.display_name or user_card.user_id.name))

    def action_revoke(self):
        self.write({"state": "revoked", "revoked_at": fields.Datetime.now()})

    def action_activate(self):
        self.write({"state": "active", "revoked_at": False})
