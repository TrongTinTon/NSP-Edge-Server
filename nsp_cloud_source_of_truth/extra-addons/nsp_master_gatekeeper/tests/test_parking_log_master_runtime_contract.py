# -*- coding: utf-8 -*-
from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestParkingLogMasterRuntimeContract(TransactionCase):

    def test_allowed_log_cannot_have_reason(self):
        log = self.env["nsp.parking.log"].new({
            "decision": "allowed",
            "reason_code": "unknown",
        })
        with self.assertRaises(ValidationError):
            log._check_decision_reason_consistency()

    def test_denied_log_requires_reason(self):
        log = self.env["nsp.parking.log"].new({
            "decision": "denied",
            "reason_code": False,
        })
        with self.assertRaises(ValidationError):
            log._check_decision_reason_consistency()

    def test_decision_reason_valid_pairs(self):
        allowed = self.env["nsp.parking.log"].new({
            "decision": "allowed",
            "reason_code": False,
        })
        denied = self.env["nsp.parking.log"].new({
            "decision": "denied",
            "reason_code": "unauthorized_vehicle_user",
        })
        allowed._check_decision_reason_consistency()
        denied._check_decision_reason_consistency()

    def test_live_monitor_check_in_allowed_is_entry(self):
        log = self.env["nsp.parking.log"].new({
            "event_type": "check_in",
            "decision": "allowed",
        })
        self.assertEqual(log._live_monitor_display_meta()["display_kind"], "entry")

    def test_live_monitor_check_out_allowed_is_clear(self):
        log = self.env["nsp.parking.log"].new({
            "event_type": "check_out",
            "decision": "allowed",
        })
        self.assertEqual(log._live_monitor_display_meta()["display_kind"], "clear")

    def test_live_monitor_denied_is_alert(self):
        log = self.env["nsp.parking.log"].new({
            "event_type": "check_out",
            "decision": "denied",
            "reason_code": "unauthorized_vehicle_user",
        })
        self.assertEqual(log._live_monitor_display_meta()["display_kind"], "alert")
