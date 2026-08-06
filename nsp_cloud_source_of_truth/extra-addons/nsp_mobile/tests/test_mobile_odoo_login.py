# -*- coding: utf-8 -*-
from odoo.fields import Command
from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestMobileOdooLogin(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.internal_group = cls.env.ref('base.group_user')
        cls.portal_group = cls.env.ref('base.group_portal')

    def _create_odoo_user(self, suffix, password='StrongPassword123!'):
        return self.env['res.users'].with_context(no_reset_password=True).create({
            'name': f'Mobile User {suffix}',
            'login': f'mobile.user.{suffix}@example.com',
            'password': password,
            'group_ids': [Command.set(self.internal_group.ids)],
        })

    def _create_profile(self, odoo_user, active=True):
        return self.env['nsp.user'].create({
            'name': odoo_user.name,
            'email': odoo_user.email,
            'odoo_user_id': odoo_user.id,
            'active': active,
        })

    def test_standard_odoo_password_auth_resolves_linked_profile(self):
        password = 'StrongPassword123!'
        odoo_user = self._create_odoo_user('linked', password=password)
        profile = self._create_profile(odoo_user)

        auth_info = self.env['res.users'].authenticate(
            {
                'type': 'password',
                'login': odoo_user.login,
                'password': password,
            },
            {'interactive': True},
        )

        self.assertEqual(auth_info['uid'], odoo_user.id)
        self.assertEqual(odoo_user._nsp_mobile_business_user(), profile)

    def test_unlinked_odoo_user_is_rejected(self):
        odoo_user = self._create_odoo_user('unlinked')
        with self.assertRaises(AccessError):
            odoo_user._nsp_mobile_business_user()

    def test_inactive_profile_is_rejected(self):
        odoo_user = self._create_odoo_user('inactive-profile')
        self._create_profile(odoo_user, active=False)
        with self.assertRaises(AccessError):
            odoo_user._nsp_mobile_business_user()

    def test_relinking_profile_revokes_active_mobile_session(self):
        first_user = self._create_odoo_user('first')
        second_user = self._create_odoo_user('second')
        profile = self._create_profile(first_user)
        device = self.env['nsp.mobile.device'].sudo().register_or_update(profile, {
            'device_uid': 'test-device-relink',
            'platform': 'android',
        })
        session = self.env['nsp.mobile.session'].sudo().open_session(profile, device)

        profile.write({'odoo_user_id': second_user.id})
        session.invalidate_recordset(['state'])

        self.assertEqual(session.state, 'revoked')

    def test_removing_internal_access_revokes_active_mobile_session(self):
        odoo_user = self._create_odoo_user('group-change')
        profile = self._create_profile(odoo_user)
        device = self.env['nsp.mobile.device'].sudo().register_or_update(profile, {
            'device_uid': 'test-device-group-change',
            'platform': 'ios',
        })
        session = self.env['nsp.mobile.session'].sudo().open_session(profile, device)

        odoo_user.write({'group_ids': [Command.set(self.portal_group.ids)]})
        session.invalidate_recordset(['state'])

        self.assertEqual(session.state, 'revoked')
        with self.assertRaises(AccessError):
            odoo_user._nsp_mobile_business_user()
