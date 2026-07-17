# -*- coding: utf-8 -*-

def post_init_hook(env):
    env["nsp.sync.auth"].sudo().search([("active", "=", True)])._ensure_controller_pairing_jobs()
