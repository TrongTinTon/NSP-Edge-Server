from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "models" / "vehicle_borrow.py"
TEXT = SOURCE.read_text(encoding="utf-8")


def test_interactive_create_forces_active_state():
    assert 'sync_create = bool(self.env.context.get("vehicle_borrow_sync"))' in TEXT
    assert 'if not sync_create:' in TEXT
    assert 'vals["state"] = "active"' in TEXT
    assert 'vals["returned_at"] = False' in TEXT


def test_sync_create_can_preserve_snapshot_state():
    marker = '# A user-created Cloud authorization always starts as Active.'
    assert marker in TEXT
    assert 'Only Cloud -> Edge snapshot application may' in TEXT


def test_returned_state_remains_explicit_action_only_for_interactive_flow():
    assert 'def action_return_vehicle(self):' in TEXT
    assert '{"state": "returned", "returned_at": fields.Datetime.now()}' in TEXT


def test_upgrade_repairs_impossible_returned_rows():
    migration = (SOURCE.parents[1] / "migrations" / "19.0.19.4.0" / "post-migrate.py").read_text(encoding="utf-8")
    assert "state = 'returned'" in migration
    assert "returned_at IS NULL" in migration
    assert "SET state = 'active'" in migration
