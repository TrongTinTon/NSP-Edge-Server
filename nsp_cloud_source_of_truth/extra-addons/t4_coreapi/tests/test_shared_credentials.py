# Part of T4 Core API. See LICENSE file for full copyright and licensing details.

from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged('post_install', '-at_install')
class TestSharedCredentialTokens(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.application = cls.env['core.api.application'].create({
            'name': 'Shared Credential Application',
            'token_ttl_hours': 24,
        })

    def test_authentication_issues_independent_access_tokens(self):
        Token = self.env['core.api.token']

        first = Token.issue_for_application(self.application)
        second = Token.issue_for_application(self.application)

        active_tokens = Token.search([
            ('application_id', '=', self.application.id),
            ('active', '=', True),
        ])
        self.assertEqual(len(active_tokens), 2)
        self.assertTrue(first['access_token_rec'].active)
        self.assertTrue(second['access_token_rec'].active)
        self.assertNotEqual(first['access_token'], second['access_token'])
        self.assertNotEqual(first['refresh_token'], second['refresh_token'])

    def test_revoking_one_token_preserves_other_clients(self):
        Token = self.env['core.api.token']

        first = Token.issue_for_application(self.application)
        second = Token.issue_for_application(self.application)
        first['access_token_rec'].action_revoke()

        self.assertFalse(first['access_token_rec'].active)
        self.assertTrue(second['access_token_rec'].active)
    def test_refresh_rotation_preserves_other_client_access(self):
        Token = self.env['core.api.token']
        first = Token.issue_for_application(self.application)
        second = Token.issue_for_application(self.application)

        application, source = Token.consume_refresh_token(first['refresh_token'])
        self.assertEqual(application, self.application)
        self.assertEqual(source, first['access_token_rec'])
        self.assertFalse(source.refresh_token_hash)
        self.assertTrue(source.active)
        self.assertTrue(second['access_token_rec'].active)

        rotated = Token.issue_for_application(application)
        self.assertNotEqual(rotated['access_token'], first['access_token'])
        self.assertNotEqual(rotated['refresh_token'], first['refresh_token'])

