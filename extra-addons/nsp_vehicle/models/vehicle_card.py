# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class NspVehicleCard(models.Model):
    _name = "nsp.vehicle.card"
    _description = "NSP Vehicle Card Assignment"
    _rec_name = "display_name"
    _order = "vehicle_id, state, assigned_at desc, id desc"

    display_name = fields.Char(string="Display Name", compute="_compute_display_name", store=True)
    vehicle_id = fields.Many2one(
        "nsp.vehicle", string="Vehicle", required=True, ondelete="cascade", index=True,
    )
    scan_tid = fields.Char(
        string="Master Card",
        store=False,
        copy=False,
        help=(
            "Keyboard-scanned RFID TID used only to resolve Master Card. "
            "The scan value is not stored."
        ),
    )
    card_id = fields.Many2one(
        "nsp.rfid.card", string="Master Card", required=True,
        ondelete="cascade", index=True,
        domain=[("card_type", "=", "vehicle_card"), ("usage_state", "=", "available")],
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
        ("vehicle_card_unique", "unique(vehicle_id, card_id)", "This card is already assigned to this vehicle."),
    ]

    def init(self):
        self.env.cr.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS nsp_vehicle_card_one_active_card_idx
                ON nsp_vehicle_card (card_id)
             WHERE state = 'active'
            """
        )

    @api.depends("vehicle_id.license_plate", "card_id.tid")
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = "%s - %s" % (
                rec.vehicle_id.license_plate or _("Vehicle"), rec.card_id.tid or _("Card")
            )

    @api.model
    def _prepare_scanned_card_values(self, vals):
        """Resolve scan_tid to Master Card before required-field validation."""
        values = dict(vals)
        if values.get("card_id"):
            return values

        normalized = self.env["nsp.rfid.card"]._normalize_tid(values.get("scan_tid"))
        if not normalized:
            return values

        result = self.env["nsp.rfid.card"].nsp_validate_scan(
            normalized,
            expected_card_type="vehicle_card",
            require_available=True,
        )
        if not result.get("valid") or not result.get("card_id"):
            raise ValidationError(result.get("message") or _("Invalid Vehicle Card."))

        values["card_id"] = result["card_id"]
        values["scan_tid"] = result.get("tid") or normalized
        return values

    @api.model_create_multi
    def create(self, vals_list):
        prepared = [self._prepare_scanned_card_values(vals) for vals in vals_list]
        card_ids = [vals.get("card_id") for vals in prepared if vals.get("card_id")]
        if len(card_ids) != len(set(card_ids)):
            raise ValidationError(_("The same Master Card is entered more than once."))
        return super().create(prepared)

    @api.constrains("card_id", "state")
    def _check_card_assignment(self):
        active = self.filtered(lambda rec: rec.card_id and rec.state == "active")
        for rec in self.filtered("card_id"):
            if rec.card_id.card_type != "vehicle_card":
                raise ValidationError(_("Vehicle Cards must use a Master Card with type Vehicle Card."))
        if not active:
            return
        duplicate = self.search([
            ("card_id", "in", active.card_id.ids),
            ("state", "=", "active"),
            ("id", "not in", active.ids),
        ], limit=1)
        if duplicate:
            raise ValidationError(
                _("This vehicle card is already active for vehicle %s.") % duplicate.vehicle_id.license_plate
            )
        user_card = self.env["nsp.user.card"].sudo().search([
            ("card_id", "in", active.card_id.ids), ("state", "=", "active")
        ], limit=1)
        if user_card:
            raise ValidationError(
                _("This card is already active for user %s.") % user_card.user_id.name
            )

    def action_revoke(self):
        self.write({"state": "revoked", "revoked_at": fields.Datetime.now()})
        return True

    def action_activate(self):
        self.write({"state": "active", "revoked_at": False})
        return True
