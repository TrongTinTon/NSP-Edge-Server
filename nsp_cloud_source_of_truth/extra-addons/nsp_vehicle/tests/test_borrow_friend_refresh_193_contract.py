from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_allowed_borrowers_depend_on_friendship_state():
    source = (ROOT / "models" / "vehicle_borrow.py").read_text()
    assert '"vehicle_id.owner_id.friendship_sent_ids.state"' in source
    assert '"vehicle_id.owner_id.friendship_received_ids.state"' in source
    assert 'def _onchange_vehicle_id_refresh_allowed_borrowers' in source
    assert 'accepted_friends_map(owners)' in source


def test_manifest_version():
    assert '"version": "19.0.19.3.0"' in (ROOT / "__manifest__.py").read_text()
