from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_parking_lane_device_tree_is_controller_reader_only():
    view = (ROOT / "views" / "parking_lane_views.xml").read_text()
    js = (ROOT / "static" / "src" / "js" / "device_tree_view.js").read_text()
    xml = (ROOT / "static" / "src" / "xml" / "device_tree_view.xml").read_text()

    # Server stays backend/source context and must not be loaded just for Lane Configuration rendering.
    assert 'name="edge_server_name"' not in view
    assert 'name="edge_server_status"' not in view
    assert 'name="controller_name"' in view
    assert 'name="controller_status"' in view

    # Parking Lane visibility is driven by synchronized Reader rows, not a missing controller_id field.
    assert "get parkingControllers()" in js
    assert "parkingControllers.length" in xml
    assert "get parkingServers()" not in js
    assert "parkingServers.length" not in xml
    assert 'return `${entry.controller.name} > ${entry.reader.name}`;' in js
    assert "Controller → Reader mapping" in xml


def test_lane_calibration_device_tree_is_controller_reader_only():
    js = (ROOT / "static" / "src" / "js" / "device_tree_view.js").read_text()
    xml = (ROOT / "static" / "src" / "xml" / "device_tree_view.xml").read_text()
    model = (ROOT / "models" / "measurement.py").read_text()

    # Server remains backend source/topology context, but it is not a visible UI node.
    assert 'filter((node) => node.device_type === "controller")' in js
    assert 'key: `cal-controller-${controllerNode.id}`' in js
    assert 'cal-server-' not in js
    assert 'entry.server.name' not in js
    assert 'return `${entry.controller.name} > ${entry.reader.name}`;' in js
    assert 't-as="server"' not in xml
    assert 'fa fa-server' not in xml
    assert 'Server → Controller → Reader' not in xml
    assert 'Controller → Reader mapping' in xml

    # Do not remove backend Server context used by synchronized topology/release validation.
    assert 'def _server_nodes(self):' in model
    assert 'server_id = fields.Many2one("nsp.edge.server"' in model
