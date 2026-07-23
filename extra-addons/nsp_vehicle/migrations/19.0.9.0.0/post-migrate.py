# -*- coding: utf-8 -*-

def migrate(cr, version):
    from odoo import api, SUPERUSER_ID
    env = api.Environment(cr, SUPERUSER_ID, {})
    record = env.ref("nsp_vehicle.menu_nsp_vehicle_cards", raise_if_not_found=False)
    if record:
        record.unlink()
    record = env.ref("nsp_vehicle.menu_nsp_vehicle_borrow_requests", raise_if_not_found=False)
    if record:
        record.unlink()
