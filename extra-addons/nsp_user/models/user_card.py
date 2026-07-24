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
        "nsp.rfid.card",
        string="Master Card",
        required=True,
        ondelete="cascade",
        index=True,
        domain=[("card_type", "=", "user_card"), ("usage_state", "=", "available")],
    )
    tid = fields.Char(string="TID", related="card_id.tid", readonly=True)
    card_type = fields.Selection(related="card_id.card_type", string="Card Type", readonly=True)
    state = fields.Selection([
        ("active", "Active"),
        ("revoked", "Revoked"),
    ], string="Status", default="active", required=True, readonly=True, index=True)
    assigned_at = fields.Datetime(string="Assigned At", default=fields.Datetime.now, readonly=True, index=True)
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
        self.env.cr.execute(
            """
            CREATE INDEX IF NOT EXISTS nsp_user_card_active_user_idx
                ON nsp_user_card (user_id, assigned_at DESC, id DESC)
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
        records = self.filtered("card_id")
        invalid = records.filtered(lambda rec: rec.card_id.card_type != "user_card")
        if invalid:
            raise ValidationError(_("User Cards must use a Master Card with type User Card."))

        active = records.filtered(lambda rec: rec.state == "active")
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
                ("card_id", "in", active.card_id.ids),
                ("state", "=", "active"),
            ], limit=1)
            if vehicle_card:
                raise ValidationError(
                    _("This card is already active for vehicle %s.") % vehicle_card.vehicle_id.license_plate
                )


    def write(self, vals):
        if "user_id" in vals or "card_id" in vals:
            for rec in self:
                if "user_id" in vals and vals.get("user_id") and rec.user_id.id != int(vals["user_id"]):
                    raise ValidationError(_("User Card owner cannot be changed after assignment. Revoke it and create a new assignment."))
                if "card_id" in vals and vals.get("card_id") and rec.card_id.id != int(vals["card_id"]):
                    raise ValidationError(_("User Card cannot be replaced on an existing assignment. Revoke it and create a new assignment."))
        return super().write(vals)

    def action_revoke(self):
        active = self.filtered(lambda rec: rec.state == "active")
        if active:
            active.write({"state": "revoked", "revoked_at": fields.Datetime.now()})
        return True

    def action_activate(self):
        revoked = self.filtered(lambda rec: rec.state == "revoked")
        if revoked:
            revoked.write({"state": "active", "revoked_at": False})
        return True
