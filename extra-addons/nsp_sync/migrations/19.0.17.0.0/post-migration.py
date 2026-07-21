# -*- coding: utf-8 -*-
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})

    jobs = env["nsp.sync.job"].sudo().search([
        ("sync_action_id.route_suffix", "=", "devices-status/sync"),
    ])
    if jobs:
        jobs.unlink()

    actions = env["ir.actions.core_api"].sudo().search([
        ("endpoint_code", "=", "nsp_gatekeeper_devices_status_sync"),
        ("route_suffix", "=", "devices-status/sync"),
    ])
    if actions:
        actions.unlink()

    xmlids = env["ir.model.data"].sudo().search([
        ("module", "=", "nsp_gatekeeper"),
        ("name", "=", "action_core_api_nsp_gatekeeper_devices_status_sync"),
    ])
    if xmlids:
        xmlids.unlink()
