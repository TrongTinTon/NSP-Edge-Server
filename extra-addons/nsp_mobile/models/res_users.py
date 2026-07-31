# -*- coding: utf-8 -*-
from odoo import models


class ResUsers(models.Model):
    _inherit = 'res.users'

    def write(self, vals):
        result = super().write(vals)
        credential_changed = bool({'active', 'login', 'password'} & set(vals))
        if credential_changed and 'nsp.mobile.session' in self.env.registry.models:
            business_users = self.env['nsp.user'].sudo().search([
                ('odoo_user_id', 'in', self.ids),
            ])
            if business_users:
                sessions = self.env['nsp.mobile.session'].sudo().search([
                    ('user_id', 'in', business_users.ids),
                    ('state', '=', 'active'),
                ])
                if sessions:
                    sessions.revoke()
        return result
