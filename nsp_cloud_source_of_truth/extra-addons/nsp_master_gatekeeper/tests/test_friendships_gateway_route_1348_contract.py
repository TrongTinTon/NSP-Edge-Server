from pathlib import Path
import ast

ROOT = Path(__file__).resolve().parents[1]


def test_manifest_version():
    manifest = ast.literal_eval((ROOT / "__manifest__.py").read_text())
    assert manifest["version"] == "19.0.13.49.0"


def test_gateway_route_migration_contract():
    source = (ROOT / "migrations/19.0.13.48.0/post-migrate.py").read_text()
    assert 'FRIENDSHIP_ROUTE = "edge/friendships/snapshot"' in source
    assert '"edge/users/snapshot"' in source
    assert '"edge/vehicle-borrows/snapshot"' in source
    assert 'env["core.api.endpoint"]' in source
    assert '"application_id": application_id' in source
    assert '"version_id": version_id' in source
    assert '"action_id": friendship_action.id' in source
    assert 'Endpoint.create(values)' in source


def test_cloud_endpoint_still_exists():
    xml = (ROOT / "data/cloud_sync_api_endpoints.xml").read_text()
    py = (ROOT / "models/sync_api_service.py").read_text()
    assert 'id="api_master_friendships"' in xml
    assert '<field name="route_suffix">edge/friendships/snapshot</field>' in xml
    assert 'route_path="edge/friendships/snapshot"' in py
    assert "def api_friendships_snapshot" in py
