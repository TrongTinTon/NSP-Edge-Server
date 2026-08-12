# -*- coding: utf-8 -*-
from pathlib import Path

from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestLaneMasterLayoutContext(TransactionCase):

    def test_lane_master_has_no_layout_or_runtime_configuration_ownership(self):
        Lane = self.env["nsp.parking.lane"]
        self.assertIn("branch_id", Lane._fields)
        self.assertIn("layout_lane_ids", Lane._fields)
        for field_name in (
            "parking_area_id", "edge_server_id", "controller_id",
            "reader_config_ids", "antenna_sequence_ids",
            "tolerance_type", "tolerance_value", "setup_state",
        ):
            self.assertNotIn(field_name, Lane._fields)

    def test_layout_lane_owns_contextual_configuration(self):
        LayoutLane = self.env["nsp.parking.layout.lane"]
        for field_name in (
            "parking_area_id", "lane_id", "edge_server_id", "controller_id",
            "reader_config_ids", "antenna_sequence_ids",
            "tolerance_type", "tolerance_value", "setup_state",
        ):
            self.assertIn(field_name, LayoutLane._fields)
        self.assertEqual(LayoutLane._fields["lane_id"].comodel_name, "nsp.parking.lane")
        self.assertEqual(LayoutLane._fields["parking_area_id"].comodel_name, "nsp.parking.area")

    def test_parking_layout_references_contextual_layout_lanes(self):
        Area = self.env["nsp.parking.area"]
        self.assertIn("layout_lane_ids", Area._fields)
        self.assertNotIn("lane_ids", Area._fields)
        self.assertEqual(
            Area._fields["layout_lane_ids"].comodel_name,
            "nsp.parking.layout.lane",
        )

    def test_contextual_child_models_do_not_belong_to_lane_master(self):
        ReaderConfig = self.env["nsp.parking.layout.lane.reader.config"]
        Sequence = self.env["nsp.parking.layout.lane.sequence"]
        self.assertIn("layout_lane_id", ReaderConfig._fields)
        self.assertIn("layout_lane_id", Sequence._fields)
        self.assertNotIn("lane_id", ReaderConfig._fields)
        self.assertNotIn("lane_id", Sequence._fields)


def test_source_contract_has_no_runtime_configuration_on_lane_master():
    root = Path(__file__).resolve().parents[1]
    parking = (root / "models/parking_config.py").read_text(encoding="utf-8")
    start = parking.index("class NspParkingLane(models.Model):")
    end = parking.index("class NspParkingLayoutLane(models.Model):")
    lane_master = parking[start:end]
    assert "parking_area_id = fields.Many2one" not in lane_master
    assert "edge_server_id = fields.Many2one" not in lane_master
    assert "controller_id = fields.Many2one" not in lane_master
    assert "antenna_sequence_ids = fields.One2many" not in lane_master
    assert "reader_config_ids = fields.One2many" not in lane_master


def test_business_terminology_distinguishes_lane_master_from_lane_configuration():
    root = Path(__file__).resolve().parents[1]
    parking_model = (root / "models/parking_config.py").read_text(encoding="utf-8")
    parking_view = (root / "views/parking_area_views.xml").read_text(encoding="utf-8")
    lane_view = (root / "views/parking_lane_views.xml").read_text(encoding="utf-8")

    assert 'string="Lane Configurations"' in parking_model
    assert '<h3>Lane Configurations</h3>' in parking_view
    assert '<create string="Add Lane"/>' in parking_view
    assert '<form string="Lane Configuration"' in lane_view
    assert '<list string="Lanes"' in lane_view
    assert '<h3>Parking Lanes</h3>' not in parking_view
    assert 'string="Parking Lanes"' not in parking_model
