# -*- coding: utf-8 -*-

def migrate(cr, version):
    from odoo import api, SUPERUSER_ID

    env = api.Environment(cr, SUPERUSER_ID, {})
    env["nsp.parking.lane"].sudo().search([])._backfill_reader_configs_from_runtime_devices()
