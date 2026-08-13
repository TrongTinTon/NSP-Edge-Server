from pathlib import Path
from xml.etree import ElementTree as ET


def _root():
    return Path(__file__).resolve().parents[1]


def test_parking_log_list_is_business_focused_eight_columns():
    root = _root()
    view_path = root / "views" / "parking_log_views.xml"
    tree = ET.parse(view_path)
    root_xml = tree.getroot()
    record = next(
        rec for rec in root_xml.findall("record")
        if rec.get("id") == "view_nsp_parking_log_list"
    )
    list_node = record.find("./field[@name='arch']/list")
    fields = [node.get("name") for node in list_node.findall("field")]
    assert fields == [
        "event_time",
        "parking_area_id",
        "lane_id",
        "vehicle_display",
        "user_id",
        "event_type",
        "decision",
        "reason_code",
    ]
    for technical in (
        "log_uid", "layout_revision", "layout_lane_id",
        "vehicle_tid", "user_tid", "borrow_id",
    ):
        assert technical not in fields


def test_checkout_authorization_targets_only_selected_user():
    business = (_root() / "models" / "parking_log_business.py").read_text(encoding="utf-8")
    assert "def _resolve_user_authorization(self, vehicle, user, event_time):" in business
    assert "def _authorized_user_borrow_map" not in business
    assert '("borrower_id", "=", user.id)' in business
    assert 'limit=1' in business
    assert "authorized, borrow = self._resolve_user_authorization(" in business


def test_parking_decision_uses_one_reason_code_not_reason_list():
    business = (_root() / "models" / "parking_log_business.py").read_text(encoding="utf-8")
    create_start = business.index("def create_from_detection_group")
    create_end = business.index("def _acquire_vehicle_continuity_lock", create_start)
    create_block = business[create_start:create_end]
    assert "reason_codes = []" not in create_block
    assert "reason_codes.append" not in create_block
    assert 'reason_code = False' in create_block
    assert '"decision": "denied" if reason_code else "allowed"' in create_block


def test_parking_log_enforces_decision_reason_invariant():
    model = (_root() / "models" / "parking_log.py").read_text(encoding="utf-8")
    assert '@api.constrains("decision", "reason_code")' in model
    assert 'record.decision == "allowed" and record.reason_code' in model
    assert 'record.decision == "denied" and not record.reason_code' in model


def test_allowed_checkout_is_not_live_monitor_entry():
    root = _root()
    live = (root / "models" / "parking_log_live.py").read_text(encoding="utf-8")
    js = (root / "static" / "src" / "js" / "parking_live_monitor.js").read_text(encoding="utf-8")
    xml = (root / "static" / "src" / "xml" / "parking_live_monitor.xml").read_text(encoding="utf-8")
    assert 'if self.event_type == "check_out":' in live
    assert '"display_kind": "clear"' in live
    assert 'payload.display_kind === "clear"' in js
    assert 'this.clearVehicleAlert(payload);' in js
    assert "row.item.event_type === 'check_out' ? 'RA' : 'VÀO'" not in xml
    assert '<span>VÀO</span>' in xml


def test_parking_log_search_hides_technical_identifiers():
    view = (_root() / "views" / "parking_log_views.xml").read_text(encoding="utf-8")
    search_start = view.index("<search>")
    search_end = view.index("</search>", search_start)
    search = view[search_start:search_end]
    for technical in ("log_uid", "vehicle_tid", "user_tid", "layout_revision", "layout_lane_id", "borrow_id"):
        assert f'name="{technical}"' not in search
