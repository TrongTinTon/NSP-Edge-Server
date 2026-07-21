# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class RfidCard(models.Model):
    _name = "nsp.rfid.card"
    _description = "NSP RFID Cards"
    _rec_name = "tid"
    _order = "card_type, tid, id"

    card_type = fields.Selection([
        ("vehicle_card", "Vehicle Card"),
        ("user_card", "User Card"),
    ], string="Card Type", required=True, index=True, help="Business type of this RFID card.")
    tid = fields.Char(string="TID", required=True, index=True, help="Unique TID data read from the RFID reader.")
    note = fields.Char(string="Note")

    is_used = fields.Boolean(string="Used", compute="_compute_usage", search="_search_is_used")
    usage_state = fields.Selection([
        ("available", "Available"),
        ("used", "Used"),
    ], string="Usage State", compute="_compute_usage", search="_search_usage_state")
    assigned_to = fields.Char(string="Assigned To", compute="_compute_usage")

    _sql_constraints = [
        ("tid_unique", "unique(tid)", "TID must be unique in RFID Cards."),
    ]

    @api.model
    def _normalize_tid(self, value):
        return str(value or "").strip().upper().replace(" ", "")

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("tid"):
                vals["tid"] = self._normalize_tid(vals.get("tid"))
        return super().create(vals_list)

    def write(self, vals):
        if vals.get("tid"):
            vals = dict(vals)
            vals["tid"] = self._normalize_tid(vals.get("tid"))
        return super().write(vals)

    def _model_ready(self, model_name):
        return model_name in self.env.registry.models

    @api.depends("card_type", "tid")
    def _compute_usage(self):
        for card in self:
            user_names = card._assigned_users()
            vehicle_names = card._assigned_vehicles()
            used = bool(user_names or vehicle_names)
            card.is_used = used
            card.usage_state = "used" if used else "available"
            labels = []
            labels += ["User: %s" % name for name in user_names if name]
            labels += ["Vehicle: %s" % name for name in vehicle_names if name]
            card.assigned_to = ", ".join(labels)

    def _assigned_users(self):
        self.ensure_one()
        if not self.id or not isinstance(self.id, int) or not self._model_ready("nsp.user.card"):
            return []
        lines = self.env["nsp.user.card"].sudo().search([("card_id", "=", self.id), ("state", "=", "active")])
        return [line.user_id.display_name or line.user_id.name or line.user_id.user_code for line in lines if line.user_id]

    def _assigned_vehicles(self):
        self.ensure_one()
        if not self.id or not isinstance(self.id, int) or not self._model_ready("nsp.vehicle.card"):
            return []
        lines = self.env["nsp.vehicle.card"].sudo().search([("card_id", "=", self.id), ("state", "=", "active")])
        return [line.vehicle_id.license_plate or line.vehicle_id.display_name for line in lines if line.vehicle_id]

    @api.model
    def _used_card_ids(self):
        used = set()
        if self._model_ready("nsp.user.card"):
            used.update(self.env["nsp.user.card"].sudo().search([("card_id", "!=", False), ("state", "=", "active")]).mapped("card_id").ids)
        if self._model_ready("nsp.vehicle.card"):
            used.update(self.env["nsp.vehicle.card"].sudo().search([("card_id", "!=", False), ("state", "=", "active")]).mapped("card_id").ids)
        return list(used)

    @api.model
    def _search_is_used(self, operator, value):
        used_ids = self._used_card_ids()
        requested_used = bool(value)
        if operator in ("!=", "not in", "not ilike"):
            requested_used = not requested_used
        return [("id", "in" if requested_used else "not in", used_ids or [0])]

    @api.model
    def _search_usage_state(self, operator, value):
        requested_used = str(value or "").strip() == "used"
        if operator in ("!=", "not in"):
            requested_used = not requested_used
        used_ids = self._used_card_ids()
        return [("id", "in" if requested_used else "not in", used_ids or [0])]

    @api.constrains("card_type", "tid")
    def _check_card_data(self):
        for card in self:
            if not card.card_type:
                raise ValidationError(_("Card Type is required."))
            if not card._normalize_tid(card.tid):
                raise ValidationError(_("TID is required."))
            if card.tid != card._normalize_tid(card.tid):
                raise ValidationError(_("TID must be normalized uppercase without spaces."))

    def _clear_references_before_unlink(self):
        ids = [card_id for card_id in self.ids if isinstance(card_id, int)]
        if not ids:
            return
        if self._model_ready("nsp.user.card"):
            self.env["nsp.user.card"].sudo().search([("card_id", "in", ids)]).unlink()
        if self._model_ready("nsp.vehicle.card"):
            self.env["nsp.vehicle.card"].sudo().search([("card_id", "in", ids)]).unlink()

    def unlink(self):
        self._clear_references_before_unlink()
        return super().unlink()

    def name_get(self):
        result = []
        for card in self:
            label = "%s [%s]" % (card.tid or "", dict(self._fields["card_type"].selection).get(card.card_type, card.card_type or ""))
            if card.is_used and card.assigned_to:
                label += " - %s" % card.assigned_to
            result.append((card.id, label))
        return result
