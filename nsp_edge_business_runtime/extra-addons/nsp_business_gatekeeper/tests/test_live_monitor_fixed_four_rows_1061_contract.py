# -*- coding: utf-8 -*-
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_live_monitor_uses_fixed_four_rows():
    js = (ROOT / "static/src/js/parking_live_monitor.js").read_text(encoding="utf-8")
    scss = (ROOT / "static/src/scss/parking_live_monitor.scss").read_text(encoding="utf-8")
    assert "const DISPLAY_ROWS = 4;" in js
    assert "DISPLAY_COLUMN_OPTIONS = Object.freeze([8, 16, 24])" in js
    assert "return this.displayColumns * DISPLAY_ROWS;" in js
    assert "grid-template-rows: repeat(4, 68px);" in scss
    assert "columns-8 .nsp-parking-live-monitor__grid { grid-template-rows: repeat(4, 82px);" in scss
    assert "columns-16 .nsp-parking-live-monitor__grid { grid-template-rows: repeat(4, 68px);" in scss
    assert "columns-24 .nsp-parking-live-monitor__grid { grid-template-rows: repeat(4, 58px);" in scss
    assert "overflow: hidden;" in scss


def test_live_monitor_capacities_are_32_64_96():
    js = (ROOT / "static/src/js/parking_live_monitor.js").read_text(encoding="utf-8")
    assert "const MAX_HISTORY = 96;" in js
    assert "const SNAPSHOT_LIMIT = 96;" in js
    assert "32/64/96 entries" in js


def test_switching_column_density_does_not_destroy_retained_history():
    js = (ROOT / "static/src/js/parking_live_monitor.js").read_text(encoding="utf-8")
    start = js.index("    setDisplayColumns(value) {")
    end = js.index("    _eventTimeMs(value) {", start)
    block = js[start:end]
    assert "this.state.entries = this.state.entries.slice" not in block
