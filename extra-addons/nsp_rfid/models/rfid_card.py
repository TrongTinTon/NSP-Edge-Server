# -*- coding: utf-8 -*-
from collections import defaultdict

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
        """Resolve assignment labels in at most two queries for the whole batch."""
        card_ids = [record.id for record in self if isinstance(record.id, int)]
        user_names = defaultdict(list)
        vehicle_names = defaultdict(list)

        if card_ids and self._model_ready("nsp.user.card"):
            lines = self.env["nsp.user.card"].sudo().search([
                ("card_id", "in", card_ids),
                ("state", "=", "active"),
            ])
            for line in lines:
                if line.card_id and line.user_id:
                    user_names[line.card_id.id].append(
                        line.user_id.display_name or line.user_id.name or line.user_id.user_code
                    )

        if card_ids and self._model_ready("nsp.vehicle.card"):
            lines = self.env["nsp.vehicle.card"].sudo().search([
                ("card_id", "in", card_ids),
                ("state", "=", "active"),
            ])
            for line in lines:
                if line.card_id and line.vehicle_id:
                    vehicle_names[line.card_id.id].append(
                        line.vehicle_id.license_plate or line.vehicle_id.display_name
                    )

        for card in self:
            users = user_names.get(card.id, [])
            vehicles = vehicle_names.get(card.id, [])
            card.is_used = bool(users or vehicles)
            card.usage_state = "used" if card.is_used else "available"
            labels = ["User: %s" % name for name in users if name]
            labels.extend("Vehicle: %s" % name for name in vehicles if name)
            card.assigned_to = ", ".join(labels)

    @api.model
    def _used_card_ids(self):
        used = set()
        if self._model_ready("nsp.user.card"):
            used.update(
                self.env["nsp.user.card"].sudo().search([
                    ("card_id", "!=", False),
                    ("state", "=", "active"),
                ]).mapped("card_id").ids
            )
        if self._model_ready("nsp.vehicle.card"):
            used.update(
                self.env["nsp.vehicle.card"].sudo().search([
                    ("card_id", "!=", False),
                    ("state", "=", "active"),
                ]).mapped("card_id").ids
            )
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
            normalized = card._normalize_tid(card.tid)
            if not card.card_type:
                raise ValidationError(_("Card Type is required."))
            if not normalized:
                raise ValidationError(_("TID is required."))
            if card.tid != normalized:
                raise ValidationError(_("TID must be normalized uppercase without spaces."))

    def name_get(self):
        result = []
        simple_name = bool(self.env.context.get("nsp_simple_card_name"))
        selections = dict(self._fields["card_type"].selection)
        for card in self:
            label = "%s [%s]" % (card.tid or "", selections.get(card.card_type, card.card_type or ""))
            if not simple_name and card.is_used and card.assigned_to:
                label += " - %s" % card.assigned_to
            result.append((card.id, label))
        return result
