# -*- coding: utf-8 -*-
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    actions = env['ir.actions.core_api'].sudo().search([('route_suffix', '=', 'controllers/sync')])
    if actions:
        env['nsp.sync.job'].sudo().search([('sync_action_id', 'in', actions.ids)]).unlink()
    cr.execute("DROP INDEX IF EXISTS nsp_sync_auth_active_local_server_uniq")
