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

    def test_lane_calibration_owns_one_contextual_device_node_collection(self):
        Session = self.env["nsp.measurement.session"]
        Node = self.env["nsp.measurement.device.node"]

        self.assertIn("device_node_ids", Session._fields)
        self.assertEqual(Session._fields["device_node_ids"].comodel_name, Node._name)
        for field_name in (
            "session_id", "device_type", "server_id", "controller_id", "reader_id",
            "parent_id", "child_ids", "sequence",
        ):
            self.assertIn(field_name, Node._fields)

    def test_device_node_is_contextual_not_master_ownership(self):
        Node = self.env["nsp.measurement.device.node"]
        self.assertEqual(Node._fields["server_id"].comodel_name, "nsp.edge.server")
        self.assertEqual(Node._fields["controller_id"].comodel_name, "nsp.controller")
        self.assertEqual(Node._fields["reader_id"].comodel_name, "nsp.device")
        self.assertEqual(Node._fields["parent_id"].comodel_name, "nsp.measurement.device.node")

    def test_reader_contextual_configuration_belongs_to_reader_node(self):
        Node = self.env["nsp.measurement.device.node"]
        for field_name in (
            "power_dbm", "read_interval_ms", "tid_addr", "tid_len", "reader_port_ids",
        ):
            self.assertIn(field_name, Node._fields)
        self.assertEqual(
            Node._fields["reader_port_ids"].comodel_name,
            "nsp.measurement.reader.port",
        )

    def test_reader_port_points_to_reader_node(self):
        Port = self.env["nsp.measurement.reader.port"]
        self.assertIn("reader_node_id", Port._fields)
        self.assertEqual(
            Port._fields["reader_node_id"].comodel_name,
            "nsp.measurement.device.node",
        )

    def test_reader_code_is_global_identity(self):
        constraints = {name: sql for name, sql, _message in self.env["nsp.device"]._sql_constraints}
        self.assertIn("device_code_unique", constraints)
        self.assertNotIn("device_code_controller_unique", constraints)
