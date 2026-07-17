# -*- coding: utf-8 -*-
from odoo import fields, models


class RfidCardVehicleExt(models.Model):
    _inherit = "nsp.rfid.card"

    vehicle_card_ids = fields.One2many("nsp.vehicle.card", "card_id", string="Vehicle Card Assignments", readonly=True)
