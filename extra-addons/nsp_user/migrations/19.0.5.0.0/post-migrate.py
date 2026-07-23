# -*- coding: utf-8 -*-

def migrate(cr, version):
    from odoo import api, SUPERUSER_ID
    env = api.Environment(cr, SUPERUSER_ID, {})
    record = env.ref("nsp_user.menu_nsp_user_cards", raise_if_not_found=False)
    if record:
        record.unlink()
    record = env.ref("nsp_user.menu_nsp_user_friendships", raise_if_not_found=False)
    if record:
        record.unlink()
