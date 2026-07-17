# -*- coding: utf-8 -*-
from odoo import api, SUPERUSER_ID


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    xmlids = [
        'nsp_zeroconfig.menu_nsp_zeroconfig_pairing_requests',
        'nsp_zeroconfig.action_nsp_zeroconfig_pairing_requests',
        'nsp_zeroconfig.view_nsp_zeroconfig_pairing_request_search',
        'nsp_zeroconfig.view_nsp_zeroconfig_pairing_request_list',
        'nsp_zeroconfig.view_nsp_zeroconfig_pairing_request_form',
        'nsp_zeroconfig_gatekeeper.view_nsp_zeroconfig_pairing_request_search_gatekeeper',
        'nsp_zeroconfig_gatekeeper.view_nsp_zeroconfig_pairing_request_list_gatekeeper',
        'nsp_zeroconfig_gatekeeper.view_nsp_zeroconfig_pairing_request_form_gatekeeper',
    ]
    for xmlid in xmlids:
        record = env.ref(xmlid, raise_if_not_found=False)
        if record:
            record.sudo().unlink()
    model = env['ir.model'].sudo().search([('model', '=', 'nsp.zeroconfig.pairing.request')], limit=1)
    if model:
        env['ir.model.access'].sudo().search([('model_id', '=', model.id)]).unlink()

    # Pairing Requests were removed. Drop their legacy data, including any one-time secrets.
    cr.execute("DROP TABLE IF EXISTS nsp_zeroconfig_pairing_request CASCADE")
