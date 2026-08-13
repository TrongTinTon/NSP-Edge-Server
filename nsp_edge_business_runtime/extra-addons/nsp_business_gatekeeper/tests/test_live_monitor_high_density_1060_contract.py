# -*- coding: utf-8 -*-
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_live_monitor_supports_only_8_16_24_columns():
    js = (ROOT / 'static/src/js/parking_live_monitor.js').read_text(encoding='utf-8')
    xml = (ROOT / 'static/src/xml/parking_live_monitor.xml').read_text(encoding='utf-8')
    assert 'DISPLAY_COLUMN_OPTIONS = Object.freeze([8, 16, 24])' in js
    assert 'const DEFAULT_COLUMNS = 16;' in js
    assert 't-foreach="[8, 16, 24]"' in xml


def test_entry_card_contains_only_plate():
    xml = (ROOT / 'static/src/xml/parking_live_monitor.xml').read_text(encoding='utf-8')
    start = xml.index('<article class="nsp-parking-live-monitor__entry-card"')
    end = xml.index('</article>', start)
    card = xml[start:end]
    assert 'nsp-parking-live-monitor__plate' in card
    assert 'nsp-parking-live-monitor__entry-time' not in card
    for forbidden in ('__avatar', '__person-name', '__lane', '__status-badge', 'employee_name', 'avatar_url', 'event_time'):
        assert forbidden not in card


def test_header_is_auto_hidden_until_hover_or_focus():
    scss = (ROOT / 'static/src/scss/parking_live_monitor.scss').read_text(encoding='utf-8')
    assert 'transform: translateY(calc(-100% + 8px));' in scss
    assert '.nsp-parking-live-monitor__header:hover' in scss
    assert '.nsp-parking-live-monitor__header:focus-within' in scss
