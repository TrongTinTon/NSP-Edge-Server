from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_master_live_monitor_high_density_contract():
    scss = (ROOT / 'static/src/scss/parking_live_monitor.scss').read_text()
    xml = (ROOT / 'static/src/xml/parking_live_monitor.xml').read_text()
    js = (ROOT / 'static/src/js/parking_live_monitor.js').read_text()
    model = (ROOT / 'models/parking_config.py').read_text()

    assert 'background: linear-gradient(135deg, #0d1b2a 0%, #1b263b 100%);' in scss
    assert 'nsp_master_gatekeeper.ParkingLiveMonitor' in js
    assert 'nsp_master_gatekeeper.ParkingLiveMonitor' in xml
    assert '/nsp_master_gatekeeper/static/' in xml
    assert 'const DISPLAY_ROWS = 4;' in js
    assert 'Object.freeze([8, 16, 24])' in js
    assert 'const DEFAULT_COLUMNS = 16;' in js
    assert 'const MAX_HISTORY = 96;' in js
    assert 'const SNAPSHOT_LIMIT = 96;' in js
    assert '[8, 16, 24]' in xml
    assert 'row.item.license_plate' in xml
    assert 'event_time' not in xml
    assert 'limit = min(max(int(limit or 16), 3), 96)' in model
