# -*- coding: utf-8 -*-

from odoo.tests.common import TransactionCase, tagged

from ..services.calibration_timeline_builder import CalibrationTimelineBuilder


@tagged("post_install", "-at_install")
class TestCalibrationTimelineBuilder(TransactionCase):

    def test_collapses_consecutive_reads_and_calculates_duration(self):
        events = [
            {
                "id": 1, "tid": "TID01", "serial_number": "R01", "port_no": 1,
                "timestamp": "2026-08-07T01:00:00.000Z", "observed_seconds": 100.0, "rssi_dbm": -62.0,
            },
            {
                "id": 2, "tid": "TID01", "serial_number": "R01", "port_no": 1,
                "timestamp": "2026-08-07T01:00:00.100Z", "observed_seconds": 100.1, "rssi_dbm": -48.0,
            },
            {
                "id": 3, "tid": "TID01", "serial_number": "R01", "port_no": 2,
                "timestamp": "2026-08-07T01:00:01.250Z", "observed_seconds": 101.25,
            },
        ]
        steps = CalibrationTimelineBuilder.build(
            events,
            readers_by_serial={
                "R01": {"controller_code": "CTRL01", "reader_name": "Reader 01"}
            },
        )
        self.assertEqual(len(steps), 2)
        self.assertEqual(steps[0]["read_count"], 2)
        self.assertEqual(steps[0]["last_seen_at"], "2026-08-07T01:00:00.100Z")
        self.assertEqual(steps[1]["duration_from_previous"], 1.25)
        self.assertEqual(steps[1]["elapsed_from_start"], 1.25)
        self.assertEqual(steps[0]["controller_code"], "CTRL01")
        self.assertEqual(steps[0]["tid"], "TID01")
        self.assertEqual(steps[0]["rssi_dbm"], -48.0)

    def test_non_consecutive_same_point_is_not_collapsed(self):
        events = [
            {"id": 1, "tid": "T", "serial_number": "R", "port_no": 1, "observed_seconds": 1.0},
            {"id": 2, "tid": "T", "serial_number": "R", "port_no": 2, "observed_seconds": 2.0},
            {"id": 3, "tid": "T", "serial_number": "R", "port_no": 1, "observed_seconds": 3.0},
        ]
        self.assertEqual(len(CalibrationTimelineBuilder.build(events)), 3)

    def test_unique_reader_port_path_keeps_first_occurrence_only(self):
        steps = [
            {"first_event_id": 1, "serial_number": "R01", "port_no": 1},
            {"first_event_id": 2, "serial_number": "R01", "port_no": 2},
            {"first_event_id": 3, "serial_number": "R01", "port_no": 1},
            {"first_event_id": 4, "serial_number": "R02", "port_no": 1},
        ]
        path = CalibrationTimelineBuilder.unique_reader_port_path(steps)
        self.assertEqual([row["first_event_id"] for row in path], [1, 2, 4])

