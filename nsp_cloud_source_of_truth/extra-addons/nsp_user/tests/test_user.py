from odoo.exceptions import ValidationError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestNspUser(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.User = cls.env["nsp.user"]
        cls.Friendship = cls.env["nsp.user.friendship"]

    def test_contact_values_are_normalized(self):
        user = self.User.create(
            {
                "name": "Clean User",
                "email": "  USER@EXAMPLE.COM ",
                "phone": " 0909000000 ",
            }
        )
        self.assertEqual(user.email, "user@example.com")
        self.assertEqual(user.phone, "0909000000")
        self.assertTrue(user.user_code.startswith("USER"))

    def test_user_code_is_immutable(self):
        user = self.User.create({"name": "Immutable User"})
        with self.assertRaises(ValidationError):
            user.write({"user_code": "USER-MANUAL"})

    def test_friendship_pair_is_unique_in_both_directions(self):
        first = self.User.create({"name": "First User"})
        second = self.User.create({"name": "Second User"})
        self.Friendship.create(
            {"requester_id": first.id, "addressee_id": second.id}
        )
        with self.assertRaises(ValidationError):
            self.Friendship.create(
                {"requester_id": second.id, "addressee_id": first.id}
            )

    def test_user_cannot_friend_themselves(self):
        user = self.User.create({"name": "Single User"})
        with self.assertRaises(ValidationError):
            self.Friendship.create(
                {"requester_id": user.id, "addressee_id": user.id}
            )
