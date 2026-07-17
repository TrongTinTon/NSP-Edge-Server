# -*- coding: utf-8 -*-
from odoo import fields, models


class CardsMasterVehicleExt(models.Model):
    _inherit = "nsp.cards.master"

    vehicle_card_ids = fields.One2many("nsp.vehicle.card", "card_id", string="Vehicle Card Assignments", readonly=True)
