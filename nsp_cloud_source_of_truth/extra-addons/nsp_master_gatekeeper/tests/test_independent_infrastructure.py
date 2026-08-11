# -*- coding: utf-8 -*-
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestIndependentInfrastructure(TransactionCase):

    def test_inventory_models_have_no_cross_ownership_fields(self):
        Edge = self.env["nsp.edge.server"]
        Controller = self.env["nsp.controller"]
        Reader = self.env["nsp.device"]

        for field_name in (
            "controller_ids", "reader_ids", "antenna_ids",
            "controller_count", "reader_count", "antenna_count",
        ):
            self.assertNotIn(field_name, Edge._fields)
        for field_name in (
            "edge_server_id", "device_ids", "reader_count", "antenna_count",
        ):
            self.assertNotIn(field_name, Controller._fields)
        self.assertNotIn("controller_id", Reader._fields)
        self.assertNotIn("edge_server_id", Reader._fields)

    def test_lane_owns_runtime_association(self):
        Lane = self.env["nsp.parking.lane"]
        for field_name in (
            "edge_server_id",
            "controller_id",
            "reader_config_ids",
            "antenna_sequence_ids",
        ):
            self.assertIn(field_name, Lane._fields)

    def test_calibration_reader_line_owns_measurement_association(self):
        ReaderLine = self.env["nsp.measurement.reader.line"]
        for field_name in ("edge_server_id", "controller_id", "reader_id"):
            self.assertIn(field_name, ReaderLine._fields)

    def test_reader_code_is_global_identity(self):
        constraints = {name: sql for name, sql, _message in self.env["nsp.device"]._sql_constraints}
        self.assertIn("device_code_unique", constraints)
        self.assertNotIn("device_code_controller_unique", constraints)
