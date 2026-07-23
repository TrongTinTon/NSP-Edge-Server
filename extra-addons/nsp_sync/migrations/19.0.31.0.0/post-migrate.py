# -*- coding: utf-8 -*-
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    """Remove the obsolete device-status sync job once during upgrade."""
    env = api.Environment(cr, SUPERUSER_ID, {})
    jobs = env["nsp.sync.job"].search([
        ("sync_action_id.route_suffix", "=", "devices-status/sync"),
    ])
    if jobs:
        jobs.unlink()
