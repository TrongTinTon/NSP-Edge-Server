from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_friendship_pull_projection_exists():
    source = (ROOT / "models" / "sync_business_adapter.py").read_text()
    assert 'def _apply_friendship(self, item, cache=None):' in source
    assert '"friendship": self._apply_friendship' in source
    assert 'if kind == "friendship":' in source


def test_edge_master_data_is_read_only_for_users():
    source = (ROOT / "models" / "master_data_projection.py").read_text()
    assert 'Cloud Master Data is read-only on Edge' in source
    assert '_inherit = "nsp.user"' in source
    assert '_inherit = "nsp.user.friendship"' in source
    assert '_inherit = "nsp.vehicle"' in source
    assert '_inherit = "nsp.vehicle.borrow"' in source


def test_no_reverse_master_data_route():
    all_source = "\n".join(path.read_text() for path in (ROOT / "models").glob("*.py"))
    assert "self-service-changes" not in all_source


def test_manifest_version():
    assert "'version': '19.0.10.69.0'" in (ROOT / "__manifest__.py").read_text()


def test_friendship_has_stable_sync_record_key():
    source = (ROOT / "models" / "sync_business_adapter.py").read_text()
    assert 'return "friendship:%s:%s" % (first, second)' in source
    block = source.split('def _record_key_from_item(self, item):', 1)[1].split('def _lane_calibration_event_payload', 1)[0]
    assert 'if not isinstance(item, dict):\n            return False' in block
