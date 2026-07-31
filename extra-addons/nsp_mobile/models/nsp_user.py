# -*- coding: utf-8 -*-
from odoo import models


class NspUser(models.Model):
    _inherit = 'nsp.user'

    def write(self, vals):
        previous_links = {record.id: record.odoo_user_id.id for record in self}
        result = super().write(vals)
        revoke_ids = set()
        if vals.get('active') is False:
            revoke_ids.update(self.ids)
        if 'odoo_user_id' in vals:
            for record in self:
                if previous_links.get(record.id) != record.odoo_user_id.id:
                    revoke_ids.add(record.id)
        if revoke_ids and 'nsp.mobile.session' in self.env.registry.models:
            sessions = self.env['nsp.mobile.session'].sudo().search([
                ('user_id', 'in', list(revoke_ids)),
                ('state', '=', 'active'),
            ])
            if sessions:
                sessions.revoke()
        return result
