from pathlib import Path
import xml.etree.ElementTree as ET


def _root():
    return Path(__file__).resolve().parents[1]


def test_detection_list_has_exact_operator_columns_in_order():
    root = _root()
    view_path = root / "views" / "parking_detection_event_views.xml"
    tree = ET.parse(view_path)
    list_node = None
    for record in tree.getroot().findall("record"):
        if record.get("id") == "view_nsp_parking_detection_event_list":
            arch = record.find("field[@name='arch']")
            list_node = arch.find("list")
            break

    assert list_node is not None
    visible_fields = [
        field.get("name")
        for field in list_node.findall("field")
        if field.get("column_invisible") not in ("True", "1")
    ]
    assert visible_fields == [
        "detected_at",
        "controller_id",
        "reader_id",
        "port_no",
        "tid",
        "resolved_identity",
        "error_code",
    ]


def test_detection_ui_labels_identity_and_issue():
    model = (_root() / "models" / "parking_detection_event.py").read_text(encoding="utf-8")
    view = (_root() / "views" / "parking_detection_event_views.xml").read_text(encoding="utf-8")

    assert 'resolved_identity = fields.Char(\n        string="Identity"' in model
    assert 'string="Issue", readonly=True, copy=False' in model
    assert 'string="Processing Result"' not in model
    assert '<filter string="Issues" name="issue"' in view
    assert '<field name="serial_number"' not in view
    assert '<field name="layout_lane_id"' not in view
    assert '<field name="layout_revision"' not in view
    assert '<field name="event_uid"' not in view
