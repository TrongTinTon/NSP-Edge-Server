from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
VIEW = ROOT / "views" / "parking_lane_views.xml"


def _lane_form():
    root = ET.parse(VIEW).getroot()
    record = next(
        rec for rec in root.findall("record")
        if rec.get("id") == "view_nsp_parking_layout_lane_form"
    )
    return record.find("field[@name='arch']/form")


def test_lane_configuration_header_is_operational_only():
    form = _lane_form()
    xml = ET.tostring(form, encoding="unicode")
    assert 'alert alert-info' not in xml
    assert 'string="Context"' not in xml
    assert 'string="Configuration"' not in xml
    for field in ("parking_area_id", "branch_id", "active"):
        assert form.find(f".//field[@name='{field}']") is not None
    for field in ("controller_id", "configuration_state", "configuration_issue"):
        assert form.find(f".//field[@name='{field}']") is None


def test_antenna_sequence_table_hides_sequence_number():
    form = _lane_form()
    sequence_list = form.find(".//field[@name='antenna_sequence_ids']/list")
    assert sequence_list is not None
    visible = [field.get("name") for field in sequence_list.findall("field")]
    assert visible == ["reader_id", "port_no", "duration_from_previous"]
    assert sequence_list.get("default_order") == "sequence asc, id asc"
