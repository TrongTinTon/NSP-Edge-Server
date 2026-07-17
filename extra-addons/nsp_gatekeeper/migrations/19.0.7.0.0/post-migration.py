# -*- coding: utf-8 -*-
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    # Existing Controllers remain valid but are visibly unpaired until an
    # approved request binds a physical machine. No synthetic pairing record is
    # created because machine_id cannot be inferred safely.
    controllers = env['nsp.controller'].sudo().search([
        ('node_type', '=', 'controller'),
        ('paired_machine_id', '=', False),
    ])
    if controllers:
        controllers.write({'paired_at': False})
