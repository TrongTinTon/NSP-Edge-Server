from pathlib import Path
import xml.etree.ElementTree as ET


def _root():
    return Path(__file__).resolve().parents[1]


def _view_node(record_id, node_name):
    tree = ET.parse(_root() / "views" / "parking_detection_event_views.xml")
    for record in tree.getroot().findall("record"):
        if record.get("id") == record_id:
            arch = record.find("field[@name='arch']")
            return arch.find(node_name)
    raise AssertionError(f"view {record_id} not found")


def test_identity_keeps_one_column_and_uses_type_only_badge_widget():
    list_node = _view_node("view_nsp_parking_detection_event_list", "list")
    identity = list_node.find("field[@name='resolved_identity']")
    assert identity is not None
    assert identity.get("widget") == "nsp_detection_identity"
    assert identity.get("decoration-info") is None
    assert identity.get("decoration-success") is None

    user = list_node.find("field[@name='user_id']")
    vehicle = list_node.find("field[@name='vehicle_id']")
    assert user is not None and user.get("column_invisible") in ("True", "1")
    assert vehicle is not None and vehicle.get("column_invisible") in ("True", "1")


def test_backend_identity_value_contains_label_only_without_type_prefix():
    model = (_root() / "models" / "parking_detection_event.py").read_text(encoding="utf-8")
    assert "record.resolved_identity = label" in model
    assert "record.resolved_identity = record.user_id.display_name" in model
    assert '_("Vehicle: %s")' not in model
    assert '_("User: %s")' not in model


def test_identity_widget_renders_only_type_as_badge_and_label_as_plain_text():
    js = (_root() / "static/src/js/detection_identity_field.js").read_text(encoding="utf-8")
    template = ET.parse(_root() / "static/src/xml/detection_identity_field.xml").getroot()
    xml_text = ET.tostring(template, encoding="unicode")

    assert 'return "User"' in js
    assert 'return "Vehicle"' in js
    assert "badge rounded-pill" in js
    assert 't-att-class="badgeClass"' in xml_text
    assert 't-esc="identityType"' in xml_text
    assert 't-esc="identityLabel"' in xml_text


def test_detection_form_uses_same_identity_widget():
    form = _view_node("view_nsp_parking_detection_event_form", "form")
    identity = form.find(".//field[@name='resolved_identity']")
    assert identity is not None
    assert identity.get("widget") == "nsp_detection_identity"
