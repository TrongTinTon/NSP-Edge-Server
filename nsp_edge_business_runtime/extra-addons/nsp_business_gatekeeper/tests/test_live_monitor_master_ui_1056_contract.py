# -*- coding: utf-8 -*-
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_business_live_monitor_uses_master_ui_contract():
    js = (ROOT / 'static/src/js/parking_live_monitor.js').read_text(encoding='utf-8')
    xml = (ROOT / 'static/src/xml/parking_live_monitor.xml').read_text(encoding='utf-8')
    scss = (ROOT / 'static/src/scss/parking_live_monitor.scss').read_text(encoding='utf-8')
    live = (ROOT / 'models/parking_log_live.py').read_text(encoding='utf-8')

    assert 'nsp_business_gatekeeper.ParkingLiveMonitor' in js
    assert 'nsp_business_gatekeeper.ParkingLiveMonitor' in xml
    assert 'toggleSettings' in js
    assert 'settingsOpen' in js
    assert 'NEW_CARD_HOLD_MS = 12000' in js
    assert 'ĐANG XÁC MINH' in js
    assert 'MỚI VÀO' in js
    assert 'nsp-parking-live-monitor__settings-popover' in xml
    assert 'nsp-parking-live-monitor__avatar' in xml
    assert 'avatar_url' in live
    assert '"decision": self.decision' in live
    assert '/nsp_master_gatekeeper/static/' not in xml
    assert 'nsp_master_gatekeeper.ParkingLiveMonitor' not in js + xml
    assert '.nsp-parking-live-monitor__settings-popover' in scss


def test_live_payload_supports_avatar_fallback_without_schema_dependency():
    live = (ROOT / 'models/parking_log_live.py').read_text(encoding='utf-8')
    assert '("avatar_128", "image_128", "image_1920")' in live
    assert 'if name in gate_user._fields' in live
