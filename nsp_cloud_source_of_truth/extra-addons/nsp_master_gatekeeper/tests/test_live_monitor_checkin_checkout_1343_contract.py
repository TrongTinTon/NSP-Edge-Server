# -*- coding: utf-8 -*-
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(relative):
    return (ROOT / relative).read_text(encoding="utf-8")


def test_master_checkout_guard_contract():
    py = _read("models/parking_log.py")
    xml = _read("static/src/xml/parking_live_monitor.xml")
    config = _read("models/parking_config.py")
    assert '"display_kind": "ignore"' in py
    assert '"display_kind": "entry"' in py
    assert "CẢNH BÁO CHECK-OUT" in xml
    assert "NGƯỜI ĐANG LẤY XE" in xml
    assert '("event_type", "=", event_type)' in config
    assert "owner = vehicle.owner_id" not in py
