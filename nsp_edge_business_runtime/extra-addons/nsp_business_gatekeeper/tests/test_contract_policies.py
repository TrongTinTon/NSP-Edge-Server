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
            "antenna_sequence_ids",
        ):
            self.assertIn(contextual, LayoutLane._fields)
        self.assertNotIn("tolerance_type", LayoutLane._fields)
        self.assertNotIn("tolerance_value", LayoutLane._fields)

    def test_device_configuration_owns_reader_ports(self):
        ReaderConfig = self.env["nsp.parking.layout.lane.reader.config"]
        ReaderPort = self.env["nsp.parking.layout.lane.reader.port"]
        Sequence = self.env["nsp.parking.layout.lane.sequence"]
        self.assertEqual(
            ReaderConfig._fields["port_ids"].comodel_name,
            "nsp.parking.layout.lane.reader.port",
        )
        self.assertEqual(
            ReaderPort._fields["reader_config_id"].comodel_name,
            "nsp.parking.layout.lane.reader.config",
        )
        self.assertNotIn("layout_lane_id", ReaderPort._fields)
        self.assertNotIn("reader_id", ReaderPort._fields)
        self.assertEqual(
            Sequence._fields["layout_lane_id"].comodel_name,
            "nsp.parking.layout.lane",
        )
        self.assertNotIn("cumulative_time", Sequence._fields)
        self.assertNotIn("is_first_point", Sequence._fields)

    def test_lane_calibration_keeps_only_runtime_fields(self):
        Session = self.env["nsp.measurement.session"]
        Target = self.env["nsp.measurement.target.line"]
        ReaderConfig = self.env["nsp.parking.layout.lane.reader.config"]
        Parking = self.env["nsp.parking.area"]

        for obsolete in (
            "target_count", "target_tag_count", "reader_count", "controller_ids",
            "controller_count", "event_count", "live_dashboard",
            "is_cloud_deployment", "applied_at",
        ):
            self.assertNotIn(obsolete, Session._fields)
        for obsolete in ("vehicle_id", "vehicle_tid", "detection_state", "detection_count"):
            self.assertNotIn(obsolete, Target._fields)
        for obsolete in ("source_type", "source_revision"):
            self.assertNotIn(obsolete, ReaderConfig._fields)
        for obsolete in ("is_published", "whitelist_count", "configuration_summary"):
            self.assertNotIn(obsolete, Parking._fields)

    def test_parking_runtime_history_keeps_master_and_context_identity(self):
        Detection = self.env["nsp.parking.detection.event"]
        ParkingLog = self.env["nsp.parking.log"]
        self.assertNotIn("lane_id", Detection._fields)
        self.assertNotIn("state", Detection._fields)
        self.assertNotIn("error_message", Detection._fields)
        self.assertNotIn("parking_log_id", Detection._fields)
        self.assertNotIn("rssi_dbm", Detection._fields)
        self.assertEqual(
            Detection._fields["layout_lane_id"].comodel_name,
            "nsp.parking.layout.lane",
        )
        self.assertEqual(ParkingLog._fields["lane_id"].comodel_name, "nsp.parking.lane")
        self.assertEqual(
            ParkingLog._fields["layout_lane_id"].comodel_name,
            "nsp.parking.layout.lane",
        )
        self.assertNotIn("source_detection_ids", ParkingLog._fields)
        self.assertFalse(ParkingLog._log_access)
        for required in (
            "log_uid", "event_time", "event_type", "decision", "reason_code",
            "parking_area_id", "layout_lane_id", "lane_id", "layout_revision",
            "vehicle_id", "vehicle_tid", "user_id", "user_tid", "borrow_id",
        ):
            self.assertIn(required, ParkingLog._fields)
        for redundant_snapshot in (
            "controller_id", "controller_code", "lane_code", "parking_area_code",
            "sequence_path", "observed_duration_seconds", "allowed_duration_seconds",
            "reader_id", "serial_number", "port_no", "primary_detection_id",
            "vehicle_code", "license_plate", "user_code", "observed_user_codes",
            "observed_user_tids", "borrow_code", "transaction_uid", "status",
            "error_code", "error_message", "reason_message",
        ):
            self.assertNotIn(redundant_snapshot, ParkingLog._fields)

    def test_controller_identity_has_no_persisted_topology_ownership(self):
        Controller = self.env["nsp.controller"]
        self.assertNotIn("edge_server_id", Controller._fields)
        self.assertNotIn("device_ids", Controller._fields)
        self.assertIn("reader_count", Controller._fields)

    def test_reader_master_has_no_controller_or_runtime_config_ownership(self):
        Reader = self.env["nsp.device"]
        for forbidden in (
            "controller_id", "power_dbm", "read_interval_ms", "tid_addr", "tid_len",
        ):
            self.assertNotIn(forbidden, Reader._fields)
        for observation_projection in (
            "status", "last_seen", "firmware_version", "runtime_power_dbm",
            "runtime_read_interval_ms", "runtime_ports_json",
        ):
            self.assertIn(observation_projection, Reader._fields)
            self.assertFalse(Reader._fields[observation_projection].store)

    def test_reader_observation_owns_physical_controller_reader_evidence(self):
        Observation = self.env["nsp.reader.observation"]
        self.assertEqual(Observation._fields["controller_id"].comodel_name, "nsp.controller")
        self.assertIn("serial_number", Observation._fields)
        self.assertIn("last_detection_at", Observation._fields)
        self.assertIn("ports_json", Observation._fields)

    def test_one_controller_two_logical_lanes_aggregate_one_physical_reader_profile(self):
        Branch = self.env["nsp.branch"].sudo()
        Edge = self.env["nsp.edge.server"].sudo()
        Controller = self.env["nsp.controller"].sudo()
        Reader = self.env["nsp.device"].sudo()
        Parking = self.env["nsp.parking.area"].sudo()
        Lane = self.env["nsp.parking.lane"].sudo()
        LayoutLane = self.env["nsp.parking.layout.lane"].sudo()
        ReaderConfig = self.env["nsp.parking.layout.lane.reader.config"].sudo()
        ReaderPort = self.env["nsp.parking.layout.lane.reader.port"].sudo()
        Sequence = self.env["nsp.parking.layout.lane.sequence"].sudo()

        branch = Branch.create({"name": "Test Branch", "code": "BRN-TEST-CONTEXT"})
        edge = Edge.create({"name": "Edge Test", "edge_server_code": "EDGE-TEST-CONTEXT"})
        controller = Controller.create({
            "controller_name": "Controller Test",
            "controller_id": "CTRL-TEST-CONTEXT",
        })
        reader = Reader.create({
            "name": "Reader Test",
            "serial_number": "SERIAL-TEST-CONTEXT",
            "device_code": "DEV-TEST-CONTEXT",
        })
        parking = Parking.create({
            "name": "Parking Test",
            "code": "PARK-TEST-CONTEXT",
            "branch_id": branch.id,
            "state": "maintenance",
            "published_revision": 1,
        })
        lane_in = Lane.create({
            "name": "Lane In",
            "code": "LANE-IN-TEST-CONTEXT",
            "branch_id": branch.id,
        })
        lane_out = Lane.create({
            "name": "Lane Out",
            "code": "LANE-OUT-TEST-CONTEXT",
            "branch_id": branch.id,
        })
        config_in = LayoutLane.create({
            "parking_area_id": parking.id,
            "lane_id": lane_in.id,
            "edge_server_id": edge.id,
            "controller_id": controller.id,
            "active": True,
        })
        config_out = LayoutLane.create({
            "parking_area_id": parking.id,
            "lane_id": lane_out.id,
            "edge_server_id": edge.id,
            "controller_id": controller.id,
            "active": True,
        })
        configs = ReaderConfig.create([
            {
                "layout_lane_id": config_in.id,
                "reader_id": reader.id,
                "power_dbm": 10,
                "read_interval_ms": 200,
                "tid_start_address": 0,
                "tid_length": 6,
            },
            {
                "layout_lane_id": config_out.id,
                "reader_id": reader.id,
                "power_dbm": 10,
                "read_interval_ms": 200,
                "tid_start_address": 0,
                "tid_length": 6,
            },
        ])
        ReaderPort.create([
            {"reader_config_id": configs[0].id, "port_no": 1},
            {"reader_config_id": configs[0].id, "port_no": 3},
            {"reader_config_id": configs[1].id, "port_no": 1},
            {"reader_config_id": configs[1].id, "port_no": 3},
        ])
        Sequence.create([
            {"layout_lane_id": config_in.id, "sequence": 1, "reader_id": reader.id, "port_no": 1, "duration_from_previous": 0.0},
            {"layout_lane_id": config_in.id, "sequence": 2, "reader_id": reader.id, "port_no": 3, "duration_from_previous": 15.0},
            {"layout_lane_id": config_out.id, "sequence": 1, "reader_id": reader.id, "port_no": 3, "duration_from_previous": 0.0},
            {"layout_lane_id": config_out.id, "sequence": 2, "reader_id": reader.id, "port_no": 1, "duration_from_previous": 15.0},
        ])

        self.assertEqual(
            [(row.reader_id.id, row.port_no) for row in config_in.antenna_sequence_ids.sorted("sequence")],
            [(reader.id, 1), (reader.id, 3)],
        )
        self.assertEqual(
            [(row.reader_id.id, row.port_no) for row in config_out.antenna_sequence_ids.sorted("sequence")],
            [(reader.id, 3), (reader.id, 1)],
        )
        self.assertEqual(controller._runtime_reader_records(), reader)
        profile = reader.runtime_profile_for_controller(controller)
        self.assertEqual(profile["ports"], [1, 3])
        self.assertEqual(profile["power_dbm"], 10)
        self.assertEqual(profile["read_interval_ms"], 200)
        payload = reader.build_controller_config_payload(controller)
        self.assertEqual(payload["serial_number"], "SERIAL-TEST-CONTEXT")
        self.assertNotIn("ports", payload)
        self.assertNotIn("parking_layouts", payload)
        self.assertNotIn("lane_code", payload)
