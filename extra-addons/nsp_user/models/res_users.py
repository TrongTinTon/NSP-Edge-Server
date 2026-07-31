# -*- coding: utf-8 -*-
from odoo import api, models, _


class ResUsers(models.Model):
    _inherit = 'res.users'

    def _ensure_nsp_user_profile(self):
        """Create the mandatory NSP business profile for internal Odoo Users.

        Portal and public accounts are not Mobile identities and are intentionally
        excluded. The unique constraint on ``nsp.user.odoo_user_id`` guarantees
        that one internal Odoo User can own only one NSP User profile.
        """
        internal_users = self.sudo().filtered(lambda user: not user.share)
        if not internal_users:
            return self.env['nsp.user']

        Profile = self.env['nsp.user'].sudo().with_context(active_test=False)
        existing = Profile.search([('odoo_user_id', 'in', internal_users.ids)])
        linked_ids = set(existing.mapped('odoo_user_id').ids)
        missing = internal_users.filtered(lambda user: user.id not in linked_ids)
        if missing:
            created = Profile.create([
                {
                    'name': user.name or user.login or _('Odoo User'),
                    'email': user.email or False,
                    'odoo_user_id': user.id,
                    'active': True,
                }
                for user in missing
            ])
            existing |= created
        return existing

    @api.model_create_multi
    def create(self, vals_list):
        users = super().create(vals_list)
        users._ensure_nsp_user_profile()
        return users

    def write(self, vals):
        result = super().write(vals)
        # Group changes may convert a portal account into an internal account.
        if {'groups_id', 'share'} & set(vals):
            self._ensure_nsp_user_profile()
        return result
