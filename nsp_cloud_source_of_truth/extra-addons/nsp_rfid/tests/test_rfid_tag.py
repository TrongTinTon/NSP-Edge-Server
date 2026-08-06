from odoo.exceptions import ValidationError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestRfidTag(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Tag = cls.env["nsp.rfid.tag"]

    def test_equivalent_formats_resolve_to_one_canonical_tid(self):
        tag = self.Tag.create({"tid": "0xAA-BB:CC"})
        self.assertEqual(tag.tid, "AABBCC")
        with self.assertRaises(ValidationError):
            self.Tag.create({"tid": "aa bb cc"})

    def test_batch_duplicate_is_rejected_before_insert(self):
        with self.assertRaises(ValidationError):
            self.Tag.create([{"tid": "ABCDEF"}, {"tid": "AB-CD-EF"}])

    def test_unicode_and_separator_normalization(self):
        tag = self.Tag.create({"tid": "０ｘ aa-bb：ＣＣ_01"})
        self.assertEqual(tag.tid, "AABBCC01")

    def test_invalid_tid_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.Tag.create({"tid": "TAG-123"})

    def test_write_cannot_create_duplicate_tid(self):
        first = self.Tag.create({"tid": "AABBCC"})
        second = self.Tag.create({"tid": "DDEEFF"})
        with self.assertRaises(ValidationError):
            second.write({"tid": first.tid})

    def test_new_tid_validation_allows_current_record(self):
        tag = self.Tag.create({"tid": "AABBCC"})
        result = self.Tag.nsp_validate_new_tid("aa-bb-cc", current_id=tag.id)
        self.assertTrue(result["valid"])
        self.assertEqual(result["tid"], "AABBCC")

    def test_get_or_create_reuses_existing_tag(self):
        first = self.Tag.get_or_create_by_tid("0x12-34-56")
        second = self.Tag.get_or_create_by_tid("12 34 56")
        self.assertEqual(first, second)
