from odoo.exceptions import UserError, ValidationError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestRfidAssignment(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Tag = cls.env["nsp.rfid.tag"]
        cls.Assignment = cls.env["nsp.rfid.tag.assignment"]
        cls.user_a = cls.env["nsp.user"].create({"name": "RFID User A"})
        cls.user_b = cls.env["nsp.user"].create({"name": "RFID User B"})

    def test_equivalent_tid_formats_share_one_whitelist_tag(self):
        first = self.Tag.get_or_create_by_tid("0xAA-BB:CC")
        second = self.Tag.get_or_create_by_tid("aa bb cc")
        self.assertEqual(first, second)
        self.assertEqual(first.tid, "AABBCC")

    def test_tag_and_target_can_only_have_one_active_assignment(self):
        self.Assignment.assign_tid(self.user_a, "AABBCC")
        with self.assertRaises(ValidationError):
            self.Assignment.assign_tid(self.user_b, "AABBCC")
        with self.assertRaises(ValidationError):
            self.Assignment.assign_tid(self.user_a, "112233")

    def test_revoke_preserves_history_and_releases_tag(self):
        assignment = self.Assignment.assign_tid(self.user_a, "AABBCC")
        assignment.action_revoke()

        self.assertEqual(assignment.state, "revoked")
        self.assertTrue(assignment.revoked_at)
        replacement = self.Assignment.assign_tid(self.user_b, "AABBCC")
        self.assertEqual(replacement.state, "active")
        self.assertEqual(replacement.user_id, self.user_b)

    def test_assignment_is_immutable_and_cannot_be_deleted(self):
        assignment = self.Assignment.assign_tid(self.user_a, "112233")
        other_tag = self.Tag.get_or_create_by_tid("445566")

        with self.assertRaises(UserError):
            assignment.write({"tag_id": other_tag.id})
        with self.assertRaises(ValidationError):
            assignment.unlink()

    def test_archiving_user_revokes_active_assignment(self):
        assignment = self.Assignment.assign_tid(self.user_a, "778899")
        self.user_a.write({"active": False})
        self.assertEqual(assignment.state, "revoked")

    def test_revoke_clears_target_scan_fields(self):
        self.Assignment.assign_tid(self.user_a, "ABCDEF")
        self.user_a.invalidate_recordset(
            [
                "active_rfid_assignment_id",
                "rfid_tag_id",
                "rfid_tid",
                "rfid_tid_input",
            ]
        )
        self.assertEqual(self.user_a.rfid_tid_input, "ABCDEF")

        action = self.user_a.action_revoke_rfid_tag()

        self.assertEqual(action.get("tag"), "reload")
        self.assertFalse(self.user_a.active_rfid_assignment_id)
        self.assertFalse(self.user_a.rfid_tag_id)
        self.assertFalse(self.user_a.rfid_tid)
        self.assertFalse(self.user_a.rfid_tid_input)

    def test_runtime_projection_contains_only_active_assignments(self):
        active = self.Assignment.assign_tid(self.user_a, "010203")
        revoked = self.Assignment.assign_tid(self.user_b, "040506")
        revoked.action_revoke()

        projection = self.Assignment.prepare_runtime_projection()
        self.assertEqual(projection["summary"]["active_assignments"], 1)
        self.assertEqual(projection["items"][0]["tid"], active.tid)
