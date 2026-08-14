from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_friendship_snapshot_is_cloud_to_edge_only():
    source = (ROOT / "models" / "sync_api_service.py").read_text()
    xml = (ROOT / "data" / "cloud_sync_api_endpoints.xml").read_text()
    assert 'def api_friendships_snapshot(self):' in source
    assert 'edge/friendships/snapshot' in xml
    assert 'self-service-changes' not in source
    assert 'self-service-changes' not in xml


def test_parking_log_requires_referenced_borrow():
    source = (ROOT / "models" / "sync_api_service.py").read_text()
    assert 'if borrow_code and not borrow:' in source
    assert 'raise ValueError("borrow_not_found")' in source


def test_manifest_version():
    assert "'version': '19.0.13.47.0'" in (ROOT / "__manifest__.py").read_text()
