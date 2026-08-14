from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_master_data_routes_are_pull_only():
    source = (ROOT / "models" / "sync_job.py").read_text()
    for route in (
        "edge/users/snapshot",
        "edge/friendships/snapshot",
        "edge/vehicles/snapshot",
        "edge/vehicle-borrows/snapshot",
    ):
        assert f'"{route}"' in source
    assert "self-service-changes" not in source
    assert '"direction": "pull", "interval": 1, "batch_size": 500, "kind": "friendship"' in source


def test_pull_full_snapshot_supports_friendship():
    source = (ROOT / "models" / "sync_job.py").read_text()
    assert 'full_snapshot = kind in ("user", "friendship", "vehicle", "vehicle_borrow", "lane_calibration")' in source


def test_manifest_version():
    assert '"version": "19.0.9.1.6"' in (ROOT / "__manifest__.py").read_text()
