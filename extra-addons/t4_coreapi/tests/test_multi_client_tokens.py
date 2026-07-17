# Part of T4 Core API. See LICENSE file for full copyright and licensing details.

from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged("post_install", "-at_install")
class TestMultiClientTokens(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.application = cls.env["core.api.application"].create({
            "name": "Shared Multi-client Application",
            "service_code": "shared-multi-client-test",
            "token_ttl_hours": 24,
            "refresh_token_ttl_hours": 168,
        })

    def test_new_authentication_preserves_existing_token_pairs(self):
        Token = self.env["core.api.token"]

        first = Token.issue_for_application(self.application, client_instance_id="client-a")
        second = Token.issue_for_application(self.application, client_instance_id="client-b")

        active_tokens = Token.search([
            ("application_id", "=", self.application.id),
            ("active", "=", True),
        ])
        self.assertEqual(len(active_tokens), 4)
        self.assertTrue(first["access_token_rec"].active)
        self.assertTrue(first["refresh_token_rec"].active)
        self.assertTrue(second["access_token_rec"].active)
        self.assertTrue(second["refresh_token_rec"].active)
        self.assertEqual(first["access_token_rec"].client_instance_id, "client-a")
        self.assertEqual(second["access_token_rec"].client_instance_id, "client-b")
        self.assertNotEqual(
            first["access_token_rec"].token_pair_id,
            second["access_token_rec"].token_pair_id,
        )

    def test_refresh_rotates_only_the_calling_client_pair(self):
        Token = self.env["core.api.token"]

        first = Token.issue_for_application(self.application, client_instance_id="client-a")
        second = Token.issue_for_application(self.application, client_instance_id="client-b")
        refreshed = Token.refresh_for_application(first["refresh_token"])

        self.assertTrue(refreshed)
        self.assertFalse(first["access_token_rec"].active)
        self.assertFalse(first["refresh_token_rec"].active)
        self.assertTrue(second["access_token_rec"].active)
        self.assertTrue(second["refresh_token_rec"].active)
        self.assertTrue(refreshed["access_token_rec"].active)
        self.assertTrue(refreshed["refresh_token_rec"].active)
        self.assertEqual(refreshed["access_token_rec"].client_instance_id, "client-a")
