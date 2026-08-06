# -*- coding: utf-8 -*-
from odoo import models


class NspUser(models.Model):
    _inherit = 'nsp.user'

    def write(self, vals):
        revoke_mobile_sessions = (
            vals.get('active') is False or 'odoo_user_id' in vals
        )
        sessions = self.env['nsp.mobile.session']
        if revoke_mobile_sessions and 'nsp.mobile.session' in self.env.registry.models:
            sessions = self.env['nsp.mobile.session'].sudo().search([
                ('user_id', 'in', self.ids),
                ('state', '=', 'active'),
            ])

        result = super().write(vals)
        if sessions:
            sessions.revoke()
        return result
