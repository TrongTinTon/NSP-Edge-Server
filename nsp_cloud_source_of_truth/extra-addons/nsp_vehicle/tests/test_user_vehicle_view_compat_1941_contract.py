from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_vehicle_user_inherit_does_not_depend_on_new_nsp_user_ui_fields():
    xml = (ROOT / "views" / "user_vehicle_views.xml").read_text(encoding="utf-8")
    assert "can_edit_profile" not in xml
    assert "can_manage_vehicles" in xml
    assert "//header" in xml
    assert "//page[@name='nsp_user_friends']" in xml


def test_vehicle_extension_owns_compatibility_visibility_field():
    source = (ROOT / "models" / "user_ext.py").read_text(encoding="utf-8")
    assert "can_manage_vehicles = fields.Boolean" in source
    assert "def _compute_can_manage_vehicles" in source
    assert "odoo_user_id" in source


def test_identity_resolution_has_backward_compatible_fallback():
    for rel in ("models/vehicle.py", "models/vehicle_borrow.py"):
        source = (ROOT / rel).read_text(encoding="utf-8")
        assert 'getattr(User, "_current_nsp_identity", None)' in source
        assert '("odoo_user_id", "=", self.env.user.id)' in source
