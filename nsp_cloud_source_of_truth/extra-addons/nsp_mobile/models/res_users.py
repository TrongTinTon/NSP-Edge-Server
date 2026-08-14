# -*- coding: utf-8 -*-
from odoo import _, models
from odoo.exceptions import AccessError


class ResUsers(models.Model):
    _inherit = 'res.users'

    def _nsp_mobile_business_user(self):
        """Return the single active NSP identity linked to this Odoo account."""
        self.ensure_one()
        odoo_user = self.sudo().exists()
        if not odoo_user or not odoo_user.active:
            raise AccessError(_('Odoo User is inactive or unavailable.'))
        if not odoo_user._is_internal():
            raise AccessError(_('NSP Mobile requires an internal Odoo User.'))

        profiles = self.env['nsp.user'].sudo().with_context(active_test=False).search([
            ('odoo_user_id', '=', odoo_user.id),
        ], limit=2)
        if len(profiles) != 1:
            raise AccessError(_(
                'This Odoo User must be linked to exactly one NSP User profile.'
            ))
        profile = profiles[0]
        if not profile.active:
            raise AccessError(_('The linked NSP User profile is inactive.'))
        return profile

    def write(self, vals):
        result = super().write(vals)
        authentication_changed = bool(
            {'active', 'login', 'password', 'group_ids'} & set(vals)
        )
        if authentication_changed and 'nsp.mobile.session' in self.env.registry.models:
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
