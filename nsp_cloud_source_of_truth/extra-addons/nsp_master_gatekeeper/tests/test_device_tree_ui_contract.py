# -*- coding: utf-8 -*-
from pathlib import Path

from odoo.tests.common import TransactionCase, tagged


def _read(relative_path):
    root = Path(__file__).resolve().parents[1]
    return (root / relative_path).read_text(encoding="utf-8")


@tagged("post_install", "-at_install")
class TestDeviceTreeUiContract(TransactionCase):

    def test_context_models_expose_device_tree_anchor(self):
        self.assertIn("device_tree_anchor", self.env["nsp.parking.layout.lane"]._fields)
        self.assertIn("device_tree_anchor", self.env["nsp.measurement.session"]._fields)
        self.assertIn("device_tree_anchor", self.env["nsp.lane.setup.wizard"]._fields)

    def test_device_tree_does_not_reintroduce_inventory_ownership(self):
        self.assertNotIn("edge_server_id", self.env["nsp.controller"]._fields)
        self.assertNotIn("controller_id", self.env["nsp.device"]._fields)
        self.assertNotIn("edge_server_id", self.env["nsp.device"]._fields)


    def test_reader_master_drops_obsolete_runtime_connection_and_antenna_fields(self):
        Reader = self.env["nsp.device"]
        for field_name in (
            "runtime_detected_serial_number",
            "connection_type",
            "antennas",
            "antennas_ids",
            "antenna_numbers",
        ):
            self.assertNotIn(field_name, Reader._fields)

    def test_lane_calibration_form_uses_tree_as_visible_device_editor(self):
        view = self.env.ref("nsp_master_gatekeeper.view_nsp_measurement_session_form")
        self.assertIn('widget="nsp_device_tree_view"', view.arch_db)
        self.assertNotIn("Manage Device Mapping", view.arch_db)

    def test_device_node_exposes_display_meta_for_tree(self):
        Node = self.env["nsp.measurement.device.node"]
        for field_name in ("device_name", "device_status", "serial_number", "port_numbers"):
            self.assertIn(field_name, Node._fields)



def test_lane_setup_reuses_device_tree_instead_of_reader_table():
    view = _read("views/lane_setup_wizard_views.xml")
    js = _read("static/src/js/device_tree_view.js")

    assert 'widget="nsp_device_tree_view"' in view
    assert '<field name="device_line_ids" invisible="1">' in view
    assert 'return "lane_setup";' in js
    assert 'this.props.record?.data?.device_line_ids || null' in js
    assert 'this.props.record?.data?.source_scope !== "calibration"' in js


def test_lane_setup_calibration_scope_is_branch_specific():
    wizard = _read("wizards/lane_setup.py")
    timeline = _read("models/lane_calibration/calibration_timeline.py")

    assert "def _calibration_branch_nodes" in wizard
    assert "node.parent_id == controller_node" in wizard
    assert "for node in reader_nodes" in wizard
    assert "lambda node: node.parent_id == controller_node" in timeline
    assert "self._reader_nodes():" not in timeline[timeline.index("def action_open_lane_setup"):timeline.index("def action_open_lane_direction_setup")]


def test_parking_layout_lane_form_keeps_same_device_tree_component():
    view = _read("views/parking_lane_views.xml")
    js = _read("static/src/js/device_tree_view.js")

    assert 'widget="nsp_device_tree_view"' in view
    assert 'model === "nsp.parking.layout.lane"' in js
    assert 'return "parking_lane";' in js
    assert 'this.mode === "parking_lane" || this.mode === "lane_setup"' in js

def test_calibration_tree_crud_is_direct_orm_not_hidden_x2many():
    js = _read("static/src/js/device_tree_view.js")
    view = _read("views/measurement_session_views.xml")

    assert 'this.orm.create("nsp.measurement.device.node"' in js
    assert 'this.orm.write("nsp.measurement.device.node"' in js
    assert 'this.orm.unlink("nsp.measurement.device.node"' in js
    assert 'this.orm.searchRead(\n                "nsp.measurement.device.node"' in js
    assert "serverScopeList" not in js
    assert "controllerScopeList" not in js
    assert "reader_line_ids" not in js
    assert "list.addNewRecord" not in js[js.index("async _createCalibrationNode"):js.index("async _createFlatReader")]
    assert '<field name="device_node_ids" invisible="1">' not in view


def test_lane_calibration_keeps_original_nested_add_buttons():
    js = _read("static/src/js/device_tree_view.js")
    xml = _read("static/src/xml/device_tree_view.xml")

    assert 'class="nsp-device-tree__independent-actions"' not in xml
    assert 'title="Add Controller" t-on-click="() => this.addController(server)"' in xml
    assert 'title="Add Reader" t-on-click="() => this.addReader(server, controller)"' in xml
    assert "Assign to Server" not in xml
    assert "Assign to Controller" not in xml
    assert "Unassigned Controllers" not in xml
    assert "Unassigned Readers" not in xml

    add_controller = js[js.index("async addController(") : js.index("async addReader(")]
    add_reader = js[js.index("async addReader(") : js.index("async _createFlatReader")]
    assert '_createCalibrationNode("controller", controller.id, server.nodeId)' in add_controller
    assert '_createCalibrationNode("reader", reader.id, controller.nodeId)' in add_reader


def test_calibration_node_persistence_uses_parent_id_for_tree_context():
    js = _read("static/src/js/device_tree_view.js")
    create = js[js.index("async _createCalibrationNode") : js.index("async addServer")]
    assert "session_id: sessionId" in create
    assert "device_type: deviceType" in create
    assert "[fieldName]: Number(masterId)" in create
    assert "parent_id: Number(parentNodeId || 0) || false" in create


def test_tree_mapping_is_created_by_add_context_without_assign_controls():
    js = _read("static/src/js/device_tree_view.js")
    node_model = _read("models/lane_calibration/calibration_reader.py")

    assert "async assignController" not in js
    assert "async assignReader" not in js
    assert "async unassignController" not in js
    assert "async unassignReader" not in js
    assert 'parent_id = fields.Many2one(' in node_model
    parent_block = node_model[node_model.index("parent_id = fields.Many2one"):node_model.index("parent_path = fields.Char")]
    assert 'ondelete="cascade"' in parent_block


def test_release_is_the_topology_completeness_gate_and_supports_many_nodes():
    status = _read("models/lane_calibration/calibration_status.py")
    reader = _read("models/lane_calibration/calibration_reader.py")

    assert "server_nodes = self._server_nodes()" in status
    assert "controller_nodes = self._controller_nodes()" in status
    assert "reader_nodes = self._reader_nodes()" in status
    assert "unassigned_controllers" in status
    assert "unassigned_readers" in status
    assert "missing_ports" in status
    assert "len(self._server_nodes()) != 1" not in status
    assert "len(self.server_scope_ids) != 1" not in status
    assert "topology completeness belongs to Release" in reader


def test_reader_configuration_is_contextual_on_reader_node():
    model = _read("models/lane_calibration/calibration_reader.py")
    session = _read("models/lane_calibration/calibration_session.py")
    js = _read("static/src/js/device_tree_view.js")

    for field in ("power_dbm", "read_interval_ms", "tid_addr", "tid_len"):
        assert f"{field} = fields.Integer" in model
    assert 'allowed = {"power_dbm", "read_interval_ms", "tid_addr", "tid_len"}' in session
    assert '"action_save_device_configuration"' in js
    assert "node_id: nodeId" in js


def test_reader_port_crud_is_direct_orm_and_owned_by_reader_node():
    js = _read("static/src/js/device_tree_view.js")
    model = _read("models/lane_calibration/calibration_reader.py")
    xml = _read("static/src/xml/device_tree_view.xml")

    assert 'this.orm.create("nsp.measurement.reader.port"' in js
    assert 'this.orm.write("nsp.measurement.reader.port"' in js
    assert 'this.orm.unlink("nsp.measurement.reader.port"' in js
    assert "reader_node_id: this.selectedEntry.nodeId" in js
    assert 'reader_node_id = fields.Many2one(' in model
    for label in ("Add Port", "Edit Port", "Remove Port"):
        assert label in xml


def test_device_selectors_enforce_whitelist_and_no_duplicates():
    js = _read("static/src/js/device_tree_view.js")
    assert '["whitelist_id", "!=", false]' in js
    assert '["whitelist_id.active", "=", true]' in js
    assert '["whitelist_id.device_type_code", "=", code]' in js
    assert '["id", "not in", excludeIds]' in js
    assert 'resModel: "nsp.edge.server"' in js
    assert 'resModel: "nsp.controller"' in js
    assert 'resModel: "nsp.device"' in js


def test_sync_payload_separates_master_devices_from_flat_topology():
    sync = _read("models/lane_calibration/calibration_sync.py")
    assert '"schema_version": 4' in sync
    assert '"devices": {' in sync
    assert '"topology": {"nodes": topology}' in sync
    assert '"node_id": node.id' in sync
    assert '"device_type": node.device_type' in sync
    assert '"device_id": device_id' in sync
    assert '"parent_node_id": parent_node_id' in sync
    assert 'row["configuration"]' in sync
    assert 'row["ports"]' in sync


def test_device_tree_editability_comes_from_parent_business_state():
    js = _read("static/src/js/device_tree_view.js")
    xml = _read("static/src/xml/device_tree_view.xml")
    assert "if (this.props.readonly)" not in js
    assert 'data.parking_area_state === "draft"' in js
    assert "Boolean(data.device_configuration_editable)" in js
    assert "Edit Configuration" in xml


def test_device_tree_status_refresh_and_master_labels_remain_live():
    js = _read("static/src/js/device_tree_view.js")
    scss = _read("static/src/scss/device_tree_view.scss")
    assert 'this.orm.read("nsp.edge.server"' in js
    assert 'this.orm.read("nsp.controller"' in js
    assert 'this.orm.read("nsp.device"' in js
    assert "setInterval" in js
    assert "5000" in js
    assert "nsp-device-tree-dot-pulse" in scss


def test_legacy_calibration_scope_models_are_not_runtime_dependencies():
    runtime_files = [
        "models/lane_calibration/calibration_reader.py",
        "models/lane_calibration/calibration_session.py",
        "models/lane_calibration/calibration_status.py",
        "models/lane_calibration/calibration_timeline.py",
        "models/lane_calibration/calibration_sync.py",
        "models/sync_api_service.py",
        "services/lane_setup_service.py",
        "wizards/lane_setup.py",
        "static/src/js/device_tree_view.js",
        "views/measurement_session_views.xml",
    ]
    text = "\n".join(_read(path) for path in runtime_files)
    for legacy in (
        "nsp.measurement.server.scope",
        "nsp.measurement.controller.scope",
        "nsp.measurement.reader.line",
        "server_scope_ids",
        "controller_scope_ids",
        "reader_line_ids",
    ):
        assert legacy not in text


def test_lane_calibration_reader_information_hides_redundant_tree_ancestors():
    xml = _read("static/src/xml/device_tree_view.xml")
    assert "Reader Information" in xml
    assert "<label>Server Name</label>" not in xml
    assert "<label>Controller Name</label>" not in xml
    assert "<label>Reader Name</label>" in xml
    assert "<label>Serial</label>" in xml


def test_detection_timeline_tag_column_is_optional():
    session_view = _read("views/measurement_session_views.xml")
    event_view = _read("views/measurement_event_views.xml")
    expected = '<field name="tid" string="Tag" optional="show"/>'
    assert expected in session_view
    assert expected in event_view


def test_reader_list_keeps_only_status_reader_serial_last_seen():
    view = _read("views/device_views.xml")
    start = view.index('<record id="nsp_device_view_list"')
    end = view.index('<record id="nsp_device_view_form"')
    reader_list = view[start:end]

    assert '<field name="status" string="Status"' in reader_list
    assert '<field name="name" string="Reader"' in reader_list
    assert '<field name="serial_number" string="Serial"' in reader_list
    assert '<field name="last_seen" string="Last Seen"' in reader_list

    assert "runtime_detected_serial_number" not in reader_list
    assert "connection_type" not in reader_list
    assert "firmware_version" not in reader_list
    assert "antennas" not in reader_list


def test_device_tree_header_and_search_are_removed():
    xml = _read("static/src/xml/device_tree_view.xml")
    js = _read("static/src/js/device_tree_view.js")
    scss = _read("static/src/scss/device_tree_view.scss")
    assert "nsp-device-tree__tree-header" not in xml
    assert "nsp-device-tree__search" not in xml
    assert "NSP Tree View" not in xml
    assert "onSearchInput" not in js
    assert "state.query" not in js
    assert "_filterTree" not in js
    assert "&__tree-header" not in scss
    assert "&__search" not in scss


def test_reader_sync_contract_drops_obsolete_reader_fields():
    device = _read("models/device.py")
    calibration_sync = _read("models/lane_calibration/calibration_sync.py")
    sync_api = _read("models/sync_api_service.py")
    parking = _read("models/parking_config.py")

    for obsolete in (
        "runtime_detected_serial_number",
        "connection_type",
        "antennas_ids",
        "antenna_numbers",
        "_antenna_config_payload",
    ):
        assert obsolete not in device

    assert '"schema_version": 4' in calibration_sync
    assert '"physical_connection"' not in calibration_sync
    assert '"physical_connection"' not in parking
    assert '"detected_serial_number"' not in sync_api
    assert 'values["runtime_detected_serial_number"]' not in sync_api
    assert 'raise ValueError("reader_serial_mismatch")' in sync_api


def test_flat_reader_add_is_persisted_not_browser_only():
    js = _read("static/src/js/device_tree_view.js")
    block = js[js.index("async _createFlatReader"):js.index("async editServer")]
    assert "await this.props.record.save()" in block
    assert "Unable to persist Reader Configuration" in block


def test_device_configuration_is_independent_from_antenna_sequence():
    root = Path(__file__).resolve().parents[1]
    parking = (root / "models/parking_config.py").read_text(encoding="utf-8")
    service = (root / "services/lane_setup_service.py").read_text(encoding="utf-8")
    assert "def _sync_reader_configs_from_sequence" not in parking
    assert "Device Configuration contains Reader(s) not used by Antenna Sequence" not in parking
    assert "used_reader_ids = set(sequence_lines.mapped" not in service
    assert "effective_device_lines = device_lines" in service


def test_publish_remains_the_completeness_gate_for_lane_configuration():
    root = Path(__file__).resolve().parents[1]
    parking = (root / "models/parking_config.py").read_text(encoding="utf-8")
    create_write = parking[parking.index("class NspParkingLayoutLane(models.Model):"):parking.index("def action_open_lane_setup", parking.index("class NspParkingLayoutLane(models.Model):"))]
    operational = parking[parking.index("def _operational_issues"):parking.index("def _publish", parking.index("def _operational_issues"))]
    assert "_validate_antenna_sequence()" not in create_write
    assert "_validate_reader_configs()" not in create_write
    assert "lane._validate_antenna_sequence()" in operational
    assert "lane._validate_reader_configs()" in operational
