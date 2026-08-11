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
            "edge_server_name", "edge_server_status",
            "controller_name", "controller_status",
            "reader_name", "reader_status",
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


def test_lane_calibration_device_tree_port_crud_and_whitelist_contract():
    js = _read("static/src/js/device_tree_view.js")
    xml = _read("static/src/xml/device_tree_view.xml")
    for token in ("addPort", "startPortEdit", "savePort", "deletePort", "selectedPortList"):
        assert token in js
    for label in ("Add Port", "Edit Port", "Remove Port"):
        assert label in xml
    assert '["whitelist_id.active", "=", true]' in js
    assert '["whitelist_id.device_type_code", "=", "SERVER"]' in js
    assert '["whitelist_id.device_type_code", "=", "CONTROLLER"]' in js
    assert '["whitelist_id.device_type_code", "=", "RFID_READER"]' in js
    assert "return this.entries.length === 0;" in js


def test_calibration_tid_fields_are_contextual_not_related_master_fields():
    model = _read("models/lane_calibration/calibration_reader.py")
    tid_block = model[model.index("reader_tid_addr = fields.Integer"):model.index("reader_power_dbm = fields.Integer")]
    assert 'related="reader_id.tid_addr"' not in tid_block
    assert 'related="reader_id.tid_len"' not in tid_block
    assert "Device Configuration can be changed only while Lane Calibration is Draft" in model


def test_lane_calibration_api_requires_revision_contract():
    api = _read("models/sync_api_service.py")
    sync = _read("models/lane_calibration/calibration_sync.py")
    assert '["event_uid", "revision", "serial_number", "port_no", "tid", "read_at"]' in api
    assert 'enforce_current_snapshot=True' in api
    assert '["lane_calibration_code", "revision", "status", "occurred_at"]' in api
    assert 'raise ValueError("invalid_lane_calibration_revision")' in sync


def test_lane_calibration_reader_manual_save_uses_backend_and_db_readback():
    js = _read("static/src/js/device_tree_view.js")
    model = _read("models/lane_calibration/calibration_reader.py")

    assert '"action_save_device_configuration"' in js
    assert '"nsp.measurement.reader.line"' in js
    assert 'await this.orm.read(' in js
    assert 'Database verification failed after saving Reader configuration.' in js
    assert 'await this.props.record.load();' in js
    assert 'Reader configuration saved and verified in the database.' in js

    assert 'def action_save_device_configuration(self, values=None, port_numbers=None, identity=None, trace_id=None):' in model
    assert 'self._ensure_draft_session(self.session_id)' in model
    assert 'self.write(normalized)' in model
    assert '"edge_server_id": self.edge_server_id.id' in model
    assert '"controller_id": self.controller_id.id' in model
    assert '"reader_id": self.reader_id.id' in model
    assert 'def action_create_device_configuration' in model
    assert '"action_create_device_configuration"' in js
    assert 'identity: identityValues' in js
    assert '"reader_power_dbm": int(self.reader_power_dbm or 0)' in model
    assert '"port_numbers": sorted(int(port.port_no) for port in self.reader_port_ids)' in model


def test_device_tree_uses_explicit_master_names_and_visible_status_dots():
    js = _read("static/src/js/device_tree_view.js")
    scss = _read("static/src/scss/device_tree_view.scss")
    calibration_model = _read("models/lane_calibration/calibration_reader.py")
    parking_model = _read("models/parking_config.py")

    assert "data.edge_server_name || many2oneLabel" in js
    assert "data.controller_name || many2oneLabel" in js
    assert 'this.orm.read("nsp.edge.server", [...serverIds], ["name", "status"])' in js
    assert 'this.orm.read("nsp.controller", [...controllerIds], ["controller_name", "status"])' in js
    assert 'this.orm.read("nsp.device", [...readerIds], ["name", "serial_number", "status"])' in js
    assert "_masterName" in js
    assert 'related="edge_server_id.name"' in calibration_model
    assert 'related="controller_id.controller_name"' in calibration_model
    assert 'related="edge_server_id.name"' in parking_model
    assert 'related="controller_id.controller_name"' in parking_model
    assert "display: inline-block;" in scss
    assert "&.is-online" in scss
    assert "&.is-degraded" in scss
    assert "&.is-offline" in scss


def test_device_tree_status_dot_refreshes_and_pulses():
    js = _read("static/src/js/device_tree_view.js")
    scss = _read("static/src/scss/device_tree_view.scss")

    assert "refreshOperationalStatuses" in js
    assert 'this.orm.read("nsp.edge.server"' in js
    assert 'this.orm.read("nsp.controller"' in js
    assert 'this.orm.read("nsp.device"' in js
    assert "setInterval" in js
    assert "5000" in js
    assert "nsp-device-tree-dot-pulse" in scss
    assert "&::after" in scss
    assert "transform: scale(3.1)" in scss
    assert "prefers-reduced-motion" in scss


def test_device_configuration_explicitly_shows_server_controller_reader_names():
    xml = _read("static/src/xml/device_tree_view.xml")
    assert "<label>Server Name</label>" in xml
    assert "<label>Controller Name</label>" in xml
    assert "<label>Reader Name</label>" in xml
    assert 't-esc="selectedEntry.server.name"' in xml
    assert 't-esc="selectedEntry.controller.name"' in xml
    assert 't-esc="selectedEntry.reader.name"' in xml
