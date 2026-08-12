# -*- coding: utf-8 -*-

from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase, tagged

from ..models.measurement_state_policy import _MEASUREMENT_STATUS_TRANSITIONS
from ..models.parking_state_policy import _PARKING_AREA_STATE_TRANSITIONS
from ..models.state_policy import (
    classify_idempotent_replay,
    compare_revision,
    validate_state_transition,
)


@tagged("post_install", "-at_install")
class TestGatekeeperContractPolicies(TransactionCase):

    def test_happy_path_state_transition(self):
        target = validate_state_transition(
            "draft",
            "ready",
            _MEASUREMENT_STATUS_TRANSITIONS,
            label="Lane Calibration",
        )
        self.assertEqual(target, "ready")

    def test_invalid_state_transition(self):
        with self.assertRaises(ValidationError):
            validate_state_transition(
                "completed",
                "running",
                _MEASUREMENT_STATUS_TRANSITIONS,
                label="Lane Calibration",
            )

    def test_duplicate_request_contract(self):
        self.assertEqual(
            classify_idempotent_replay("event-001", payload_matches=True),
            "duplicate",
        )
        self.assertEqual(
            classify_idempotent_replay("event-001", payload_matches=False),
            "conflict",
        )

    def test_stale_revision_contract(self):
        self.assertEqual(compare_revision(4, 5), "stale")
        self.assertEqual(compare_revision(5, 5), "current")
        self.assertEqual(compare_revision(6, 5), "future")

    def test_parking_state_policy(self):
        self.assertEqual(
            validate_state_transition(
                "draft",
                "operational",
                _PARKING_AREA_STATE_TRANSITIONS,
                label="Parking Area",
            ),
            "operational",
        )
        with self.assertRaises(ValidationError):
            validate_state_transition(
                "draft",
                "blocked",
                _PARKING_AREA_STATE_TRANSITIONS,
                label="Parking Area",
            )
    def test_lane_master_is_independent_from_parking_layout(self):
        Lane = self.env["nsp.parking.lane"]
        self.assertIn("branch_id", Lane._fields)
        self.assertIn("layout_lane_ids", Lane._fields)
        for forbidden in (
            "parking_area_id", "edge_server_id", "controller_id",
            "reader_config_ids", "antenna_sequence_ids",
            "tolerance_type", "tolerance_value",
        ):
            self.assertNotIn(forbidden, Lane._fields)

    def test_parking_layout_owns_contextual_lane_configuration(self):
        Parking = self.env["nsp.parking.area"]
        LayoutLane = self.env["nsp.parking.layout.lane"]
        self.assertEqual(
            Parking._fields["layout_lane_ids"].comodel_name,
            "nsp.parking.layout.lane",
        )
        self.assertEqual(LayoutLane._fields["lane_id"].comodel_name, "nsp.parking.lane")
        self.assertEqual(
            LayoutLane._fields["parking_area_id"].comodel_name, "nsp.parking.area"
        )
        for contextual in (
            "edge_server_id", "controller_id", "reader_config_ids",
            "antenna_sequence_ids", "tolerance_type", "tolerance_value",
        ):
            self.assertIn(contextual, LayoutLane._fields)

    def test_device_configuration_owns_reader_ports(self):
        ReaderConfig = self.env["nsp.parking.layout.lane.reader.config"]
        ReaderPort = self.env["nsp.parking.layout.lane.reader.port"]
        Sequence = self.env["nsp.parking.layout.lane.sequence"]
        self.assertEqual(
            ReaderConfig._fields["port_ids"].comodel_name,
            "nsp.parking.layout.lane.reader.port",
        )
        self.assertEqual(
            ReaderPort._fields["layout_lane_id"].comodel_name,
            "nsp.parking.layout.lane",
        )
        self.assertEqual(
            Sequence._fields["layout_lane_id"].comodel_name,
            "nsp.parking.layout.lane",
        )

    def test_parking_runtime_history_keeps_master_and_context_identity(self):
        Detection = self.env["nsp.parking.detection.event"]
        Transaction = self.env["nsp.parking.transaction"]
        self.assertEqual(Detection._fields["lane_id"].comodel_name, "nsp.parking.lane")
        self.assertEqual(
            Detection._fields["layout_lane_id"].comodel_name,
            "nsp.parking.layout.lane",
        )
        self.assertEqual(Transaction._fields["lane_id"].comodel_name, "nsp.parking.lane")
        self.assertEqual(
            Transaction._fields["layout_lane_id"].comodel_name,
            "nsp.parking.layout.lane",
        )

