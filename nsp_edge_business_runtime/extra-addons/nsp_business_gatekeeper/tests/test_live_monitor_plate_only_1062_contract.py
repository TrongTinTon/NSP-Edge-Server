# -*- coding: utf-8 -*-
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_entry_card_renders_only_license_plate():
    xml = (ROOT / "static/src/xml/parking_live_monitor.xml").read_text(encoding="utf-8")
    start = xml.index('<article class="nsp-parking-live-monitor__entry-card"')
    end = xml.index('</article>', start)
    card = xml[start:end]
    assert 'nsp-parking-live-monitor__plate' in card
    assert 'row.item.license_plate' in card
    for forbidden in (
        'entry-time', 'event_time', 'formatEventTime', 'avatar', 'person-name',
        'lane_name', 'display_title', 'display_reason',
    ):
        assert forbidden not in card


def test_plate_only_card_keeps_fixed_four_row_density_modes():
    js = (ROOT / "static/src/js/parking_live_monitor.js").read_text(encoding="utf-8")
    scss = (ROOT / "static/src/scss/parking_live_monitor.scss").read_text(encoding="utf-8")
    assert 'DISPLAY_COLUMN_OPTIONS = Object.freeze([8, 16, 24])' in js
    assert 'const DISPLAY_ROWS = 4;' in js
    assert 'nsp-parking-live-monitor__entry-time' not in scss
    assert 'justify-content: center;' in scss
