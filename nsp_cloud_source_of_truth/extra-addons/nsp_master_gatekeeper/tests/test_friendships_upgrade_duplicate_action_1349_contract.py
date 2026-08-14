from pathlib import Path
import ast

ROOT = Path(__file__).resolve().parents[1]


def test_manifest_version_1349():
    manifest = ast.literal_eval((ROOT / "__manifest__.py").read_text())
    assert manifest["version"] == "19.0.13.49.0"


def test_pre_migration_adopts_existing_action_before_xml_load():
    source = (ROOT / "migrations/19.0.13.49.0/pre-migrate.py").read_text()
    assert 'ACTION_NAME = "Friendships Snapshot"' in source
    assert 'ENDPOINT_CODE = "nsp_edge_friendships_snapshot"' in source
    assert 'ROUTE_SUFFIX = "edge/friendships/snapshot"' in source
    assert 'XML_NAME = "api_master_friendships"' in source
    assert '("endpoint_manager_id", "=", manager.id)' in source
    assert '("name", "=", ACTION_NAME)' in source
    assert '"res_id": canonical.id' in source
    assert 'ModelData.create' in source


def test_post_migration_does_not_regenerate_api_actions():
    source_1348 = (ROOT / "migrations/19.0.13.48.0/post-migrate.py").read_text()
    source_1349 = (ROOT / "migrations/19.0.13.49.0/post-migrate.py").read_text()
    assert "_generate_core_api_action" not in source_1348
    assert "_generate_core_api_action" not in source_1349
    assert 'env["core.api.endpoint"]' in source_1349
    assert 'FRIENDSHIP_CODE = "nsp_edge_friendships_snapshot"' in source_1349


def test_friendship_xml_identity_remains_stable():
    xml = (ROOT / "data/cloud_sync_api_endpoints.xml").read_text()
    assert 'id="api_master_friendships"' in xml
    assert '<field name="name">Friendships Snapshot</field>' in xml
    assert '<field name="endpoint_code">nsp_edge_friendships_snapshot</field>' in xml
