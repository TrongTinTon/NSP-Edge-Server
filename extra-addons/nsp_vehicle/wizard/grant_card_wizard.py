# -*- coding: utf-8 -*-
from odoo import models, fields, _
from odoo.exceptions import UserError


class GrantCardWizard(models.TransientModel):
    _name = 'nsp.grant.card.wizard'
    _description = 'Grant Card Wizard'

    vehicle_id = fields.Many2one('nsp.vehicle', string="Vehicle", required=True, readonly=True)
    current_tid = fields.Char(string="Current TID", readonly=True)
    scanned_tid = fields.Char(string="Scanned TID", readonly=True, help="Please scan the card on the reader...")

    def action_apply(self):
        self.ensure_one()
        new_tid = (self.scanned_tid or self.current_tid or "").strip()
        if not new_tid:
            raise UserError(_("Please scan a card to get the TID before saving."))

        Card = self.env['nsp.rfid.card'].sudo()
        card = Card.search([('tid', '=', new_tid), ('card_type', '=', 'vehicle_card')], limit=1)
        if not card:
            raise UserError(_("TID %s does not exist in Master Cards as a Vehicle Card.") % new_tid)
        if card.usage_state != 'available':
            raise UserError(_("TID %s is not available in Master Cards.") % new_tid)

        duplicate_vehicle_card = self.env['nsp.vehicle.card'].sudo().search([
            ('card_id', '=', card.id), ('state', '=', 'active'), ('vehicle_id', '!=', self.vehicle_id.id)
        ], limit=1)
        if duplicate_vehicle_card:
            raise UserError(_("Card with TID %s is already active for vehicle %s!") % (new_tid, duplicate_vehicle_card.vehicle_id.license_plate))

        duplicate_user_card = self.env['nsp.user.card'].sudo().search([
            ('card_id', '=', card.id), ('state', '=', 'active')
        ], limit=1)
        if duplicate_user_card:
            raise UserError(_("Card with TID %s is already active for user %s!") % (new_tid, duplicate_user_card.user_id.display_name or duplicate_user_card.user_id.name))

        line = self.env['nsp.vehicle.card'].sudo().search([
            ('vehicle_id', '=', self.vehicle_id.id), ('card_id', '=', card.id)
        ], limit=1)
        if line:
            line.action_activate()
        else:
            self.env['nsp.vehicle.card'].sudo().create({
                'vehicle_id': self.vehicle_id.id,
                'card_id': card.id,
                'state': 'active',
            })

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Success"),
                'message': _("Vehicle Card assigned from Master Cards."),
                'type': 'success',
                'sticky': False,
                'next': {'type': 'ir.actions.act_window_close'},
            }
        }
