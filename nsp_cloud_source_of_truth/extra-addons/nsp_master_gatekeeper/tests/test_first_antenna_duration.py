# -*- coding: utf-8 -*-
from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestFirstAntennaDuration(TransactionCase):

    def test_first_point_is_system_owned_zero_duration(self):
        reader_1 = self.env["nsp.device"].create({"name": "Duration Reader 1"})
        reader_2 = self.env["nsp.device"].create({"name": "Duration Reader 2"})
        lane = self.env["nsp.parking.lane"].new({
            "antenna_sequence_ids": [
                (0, 0, {
                    "sequence": 10,
                    "reader_id": reader_1.id,
                    "port_no": 1,
                    "duration_from_previous": 9.0,
                }),
                (0, 0, {
                    "sequence": 20,
                    "reader_id": reader_2.id,
                    "port_no": 1,
                    "duration_from_previous": 2.0,
                }),
            ],
        })

        # Validation no longer treats user input on the first point as a business
        # error. ORM persistence normalizes that point to zero before publishing.
        with patch.object(type(lane), "_validate_whitelist_identity", return_value=True):
            self.assertTrue(lane._validate_antenna_sequence())

    def test_publish_contract_forces_first_duration_to_zero(self):
        service = self.env["nsp.lane.setup.sequence.line"]
        line_1 = service.new({"sequence": 10, "duration_ms": 9000})
        line_2 = service.new({"sequence": 20, "duration_ms": 2500})
        line_1.reader_id = self.env["nsp.device"].create({"name": "Build Reader 1"})
        line_2.reader_id = self.env["nsp.device"].create({"name": "Build Reader 2"})
        line_1.port_no = 1
        line_2.port_no = 1
        from ..services.lane_setup_service import LaneSetupService
        commands = LaneSetupService._build_sequence_commands(line_1 | line_2)
        self.assertEqual(commands[1][2]["duration_from_previous"], 0.0)
        self.assertEqual(commands[2][2]["duration_from_previous"], 2.5)
