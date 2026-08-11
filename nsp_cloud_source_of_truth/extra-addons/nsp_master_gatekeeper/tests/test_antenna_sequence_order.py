# -*- coding: utf-8 -*-
from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestAntennaSequenceOrder(TransactionCase):

    def test_odoo_sort_priority_does_not_need_to_be_contiguous(self):
        """The handle's sequence is UI ordering, not the published business index."""
        reader_1 = self.env["nsp.device"].create({"name": "Order Test Reader 1"})
        reader_2 = self.env["nsp.device"].create({"name": "Order Test Reader 2"})
        lane = self.env["nsp.parking.lane"].new({
            "antenna_sequence_ids": [
                (0, 0, {
                    "sequence": 10,
                    "reader_id": reader_1.id,
                    "port_no": 1,
                    "duration_from_previous": 0.0,
                }),
                (0, 0, {
                    "sequence": 20,
                    "reader_id": reader_2.id,
                    "port_no": 1,
                    "duration_from_previous": 2.0,
                }),
            ],
        })

        with patch.object(type(lane), "_validate_whitelist_identity", return_value=True):
            self.assertTrue(lane._validate_antenna_sequence())
