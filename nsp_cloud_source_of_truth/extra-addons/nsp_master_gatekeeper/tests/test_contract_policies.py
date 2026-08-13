# -*- coding: utf-8 -*-

from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase, tagged

from ..models.lane_calibration.calibration_status import (
    CalibrationStatusPolicy,
    _PASS_STATE_TRANSITIONS,
    _RESULT_STATE_TRANSITIONS,
)
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
            CalibrationStatusPolicy.TRANSITIONS,
            label="Lane Calibration",
        )
        self.assertEqual(target, "ready")

    def test_invalid_state_transition(self):
        with self.assertRaises(ValidationError):
            validate_state_transition(
                "completed",
                "running",
                CalibrationStatusPolicy.TRANSITIONS,
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

    def test_calibration_runtime_path_and_revise_sources(self):
        self.assertEqual(
            CalibrationStatusPolicy.validate_transition(
                "ready", "completed", allow_same=False
            ),
            "completed",
        )
        with self.assertRaises(ValidationError):
            CalibrationStatusPolicy.validate_transition("failed", "ready")
        self.assertEqual(
            CalibrationStatusPolicy.REVISION_SOURCES,
            frozenset({"ready", "completed"}),
        )

    def test_lane_calibration_lifecycle_requires_actions(self):
        Session = self.env["nsp.measurement.session"]
        session = Session.create({})
        try:
            self.assertEqual(session.status, "draft")
            with self.assertRaises(ValidationError):
                session.write({"status": "ready"})
            with self.assertRaises(ValidationError):
                session.write({"revision": 2})
        finally:
            session.unlink()

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

    def test_child_workflow_state_policies(self):
        self.assertEqual(
            validate_state_transition(
                "running", "completed", _PASS_STATE_TRANSITIONS, label="Calibration Run"
            ),
            "completed",
        )
        self.assertEqual(
            validate_state_transition(
                "draft", "accepted", _RESULT_STATE_TRANSITIONS, label="Calibration Result"
            ),
            "accepted",
        )

