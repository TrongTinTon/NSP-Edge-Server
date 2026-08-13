# -*- coding: utf-8 -*-
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _text(path):
    return (ROOT / path).read_text(encoding="utf-8")


def test_parking_log_ui_is_business_focused():
    xml = _text("views/parking_log_views.xml")
    list_start = xml.index('<list string="Parking Logs"')
    list_end = xml.index('</list>', list_start)
    list_xml = xml[list_start:list_end]
    expected = [
        'name="event_time"',
        'name="parking_area_id"',
        'name="lane_id"',
        'name="vehicle_display"',
        'name="user_id"',
        'name="event_type"',
        'name="decision"',
        'name="reason_code"',
    ]
    positions = [list_xml.index(token) for token in expected]
    assert positions == sorted(positions)
    for technical in (
        'name="log_uid"', 'name="layout_revision"', 'name="layout_lane_id"',
        'name="vehicle_tid"', 'name="user_tid"', 'name="borrow_id"',
    ):
        assert technical not in list_xml


def test_decision_reason_contract_is_strict_on_cloud():
    model = _text("models/parking_log.py")
    sync = _text("models/sync_api_service.py")
    assert '@api.constrains("decision", "reason_code")' in model
    assert 'denied_event_requires_reason' in sync
    assert 'reason_code = "unknown"' not in sync
    assert 'allowed_event_cannot_have_reason' in sync


def test_historical_log_no_longer_depends_on_layout_lane():
    model = _text("models/parking_log.py")
    sync = _text("models/sync_api_service.py")
    business_start = model.index('def _business_values')
    business_end = model.index('def create_idempotent', business_start)
    business = model[business_start:business_end]
    prepare_start = sync.index('def _prepare_parking_log_values')
    prepare_end = sync.index('@endpoint("NSP Edge Parking Logs"', prepare_start)
    prepare = sync[prepare_start:prepare_end]
    assert '"layout_lane_id": int(value("layout_lane_id")' not in business
    assert '"layout_lane_id": layout_lane.id' not in prepare
    assert '"lane_id": lane.id' in prepare
    assert 'ondelete="set null"' in model


def test_duplicate_is_checked_before_current_route_validation():
    sync = _text("models/sync_api_service.py")
    start = sync.index('def _prepare_parking_log_values')
    end = sync.index('@endpoint("NSP Edge Parking Logs"', start)
    body = sync[start:end]
    assert body.index('if existing:') < body.index('if (area_code, lane_code, revision) not in cache["valid_route_keys"]:')
    assert 'stale_parking_layout_revision' in body
    assert 'log_uid_conflict' in body


def test_live_monitor_check_out_allowed_is_clear():
    model = _text("models/parking_log.py")
    js = _text("static/src/js/parking_live_monitor.js")
    assert 'if self.event_type == "check_out":' in model
    assert '"display_kind": "clear"' in model
    assert 'payload.display_kind === "clear"' in js
    assert 'this.clearVehicleAlert(payload);' in js


def test_manifest_version():
    assert "'version': '19.0.13.41.0'" in _text("__manifest__.py")
