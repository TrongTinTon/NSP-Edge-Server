# -*- coding: utf-8 -*-
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(relative):
    return (ROOT / relative).read_text(encoding="utf-8")


def test_live_monitor_has_two_operation_modes():
    js = _read("static/src/js/parking_live_monitor.js")
    xml = _read("static/src/xml/parking_live_monitor.xml")
    assert 'Object.freeze(["check_in", "check_out"])' in js
    assert "CHECK-IN" in xml and "CHECK-OUT" in xml
    assert "selectDisplayMode('check_in')" in xml
    assert "selectDisplayMode('check_out')" in xml


def test_checkin_has_no_alert_surface():
    py = _read("models/parking_log_live.py")
    xml = _read("static/src/xml/parking_live_monitor.xml")
    assert 'if self.event_type == "check_in"' in py
    assert '"display_kind": "ignore"' in py
    assert 't-if="isCheckInMode"' in xml
    assert "Check-in intentionally has no alert surface" in xml


def test_checkout_is_guard_two_panel_layout():
    xml = _read("static/src/xml/parking_live_monitor.xml")
    scss = _read("static/src/scss/parking_live_monitor.scss")
    assert "nsp-parking-live-monitor__checkout-list-panel" in xml
    assert "nsp-parking-live-monitor__guard-alert-panel" in xml
    assert "XE RA" in xml
    assert "CẢNH BÁO CHECK-OUT" in xml
    assert "grid-template-columns: minmax(0, 2.15fr) minmax(340px, .85fr)" in scss


def test_checkout_payload_uses_actual_detected_user_only():
    py = _read("models/parking_log_live.py")
    assert 'gate_user = self.user_id if self.event_type == "check_out" and self.user_id else self.env["nsp.user"].browse()' in py
    assert '"has_checkout_user": bool(gate_user)' in py
    assert "owner = vehicle.owner_id" not in py


def test_snapshot_is_filtered_by_selected_event_type():
    py = _read("models/parking_config.py")
    js = _read("static/src/js/parking_live_monitor.js")
    assert 'def get_live_monitor_snapshot(self, parking_area_id, limit=16, event_type="check_in")' in py
    assert '("event_type", "=", event_type)' in py
    assert '[this.parkingAreaId, SNAPSHOT_LIMIT, this.displayMode]' in js
