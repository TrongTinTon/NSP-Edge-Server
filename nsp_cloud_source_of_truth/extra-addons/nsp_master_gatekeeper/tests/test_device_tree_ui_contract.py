from pathlib import Path
# -*- coding: utf-8 -*-
from odoo.tests.common import TransactionCase, tagged


def _read(relative_path):
    root = Path(__file__).resolve().parents[1]
    return (root / relative_path).read_text(encoding="utf-8")


@tagged("post_install", "-at_install")
class TestDeviceTreeUiContract(TransactionCase):

    def test_context_models_expose_device_tree_anchor(self):
        self.assertIn("device_tree_anchor", self.env["nsp.parking.lane"]._fields)
        self.assertIn("device_tree_anchor", self.env["nsp.measurement.session"]._fields)

    def test_contextual_lines_expose_independent_operational_status(self):
        lane_config = self.env["nsp.parking.lane.reader.config"]
        calibration_line = self.env["nsp.measurement.reader.line"]
        for field_name in ("reader_name", "reader_serial_number", "reader_status"):
            self.assertIn(field_name, lane_config._fields)
        for field_name in (
            "edge_server_status", "controller_status", "reader_name", "reader_status"
        ):
            self.assertIn(field_name, calibration_line._fields)

    def test_device_tree_does_not_reintroduce_inventory_ownership(self):
        self.assertNotIn("edge_server_id", self.env["nsp.controller"]._fields)
        self.assertNotIn("controller_id", self.env["nsp.device"]._fields)
        self.assertNotIn("edge_server_id", self.env["nsp.device"]._fields)

    def test_parking_lane_form_uses_tree_as_only_visible_mapping_editor(self):
        view = self.env.ref("nsp_master_gatekeeper.view_nsp_parking_lane_form")
        self.assertIn('widget="nsp_device_tree_view"', view.arch_db)
        self.assertNotIn('string="Infrastructure"', view.arch_db)
        self.assertNotIn("Manage Device Mapping", view.arch_db)
        self.assertIn('widget="nsp_lane_sequence_preview"', view.arch_db)
        self.assertIn("nsp-parking-sequence-grid", view.arch_db)

    def test_lane_calibration_form_uses_tree_as_only_visible_mapping_editor(self):
        view = self.env.ref("nsp_master_gatekeeper.view_nsp_measurement_session_form")
        self.assertIn('widget="nsp_device_tree_view"', view.arch_db)
        self.assertNotIn("Manage Device Mapping", view.arch_db)

    def test_parking_lane_exposes_sequence_preview_anchor(self):
        self.assertIn(
            "antenna_sequence_preview_anchor",
            self.env["nsp.parking.lane"]._fields,
        )


def test_device_tree_edit_mode_is_not_blocked_by_computed_anchor_readonly():
    root = Path(__file__).resolve().parents[1]
    js = (root / "static/src/js/device_tree_view.js").read_text(encoding="utf-8")
    xml = (root / "static/src/xml/device_tree_view.xml").read_text(encoding="utf-8")

    assert "if (this.props.readonly)" not in js
    assert 'data.parking_area_state === "draft"' in js
    assert 'data.status === "draft"' in js
    assert "Edit Configuration" in xml
    assert "Edit mode" in xml
    assert "View mode" in xml


def test_device_tree_node_crud_and_name_labels_contract():
    root = Path(__file__).resolve().parents[1]
    js = (root / "static/src/js/device_tree_view.js").read_text(encoding="utf-8")
    xml = (root / "static/src/xml/device_tree_view.xml").read_text(encoding="utf-8")

    title_start = xml.index('<div class="nsp-device-tree__tree-title-row">')
    title_end = xml.index('</div>', title_start) + len('</div>')
    title_block = xml[title_start:title_end]
    assert "Add Reader" not in title_block

    for label in (
        "Add Server", "Edit Server", "Remove Server",
        "Add Controller", "Edit Controller", "Remove Controller",
        "Add Reader", "Edit Reader", "Remove Reader",
    ):
        assert label in xml

    assert 'SelectCreateDialog' in js
    assert 'resModel: "nsp.edge.server"' in js
    assert 'resModel: "nsp.controller"' in js
    assert 'resModel: "nsp.device"' in js
    assert 'multiSelect: false' in js
    assert 'noCreate: true' in js
    assert 'NspDeviceNodeDialog' not in js
    assert 'NspDeviceMappingDialog' not in js
    assert 't-esc="server.name"' in xml
    assert 't-esc="controller.name"' in xml
    assert 't-esc="entry.reader.name"' in xml



def test_device_tree_add_reader_uses_odoo19_x2many_add_params():
    js = _read("static/src/js/device_tree_view.js")
    assert "list.addNewRecord();" not in js
    assert "list.addNewRecord({" in js
    assert 'mode: "edit"' in js
    assert 'position: "bottom"' in js
    assert "context: list.context || {}" in js


def test_device_tree_add_edit_uses_native_odoo_search_view_dialog():
    js = _read("static/src/js/device_tree_view.js")
    xml = _read("static/src/xml/device_tree_view.xml")
    assert 'from "@web/views/view_dialogs/select_create_dialog"' in js
    assert "this.dialog.add(SelectCreateDialog" in js
    assert 'multiSelect: false' in js
    assert 'noCreate: true' in js
    assert 'NspDeviceNodeDialog' not in js
    assert 'NspDeviceMappingDialog' not in js
    assert 'DeviceNodeDialog' not in xml
    assert 'DeviceMappingDialog' not in xml
    assert "_openServerSearch" in js
    assert "_openControllerSearch" in js
    assert "_openReaderSearch" in js
