# -*- coding: utf-8 -*-
from odoo import models


class NspUser(models.Model):
    _inherit = 'nsp.user'

    def write(self, vals):
        result = super().write(vals)
        if vals.get('active') is False and 'nsp.mobile.session' in self.env.registry.models:
            sessions = self.env['nsp.mobile.session'].sudo().search([
                ('user_id', 'in', self.ids),
                ('state', '=', 'active'),
            ])
            if sessions:
                sessions.revoke()
        return result
