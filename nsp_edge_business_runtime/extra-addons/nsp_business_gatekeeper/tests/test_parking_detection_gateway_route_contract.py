from pathlib import Path


def test_parking_detection_endpoint_contract():
    root = Path(__file__).resolve().parents[1]
    api = (root / 'models' / 'api_service.py').read_text(encoding='utf-8')
    xml = (root / 'data' / 'controller_api_endpoints.xml').read_text(encoding='utf-8')

    assert 'route_path="parking/detections/push"' in api
    assert 'code="nsp_controller_parking_detection_push"' in api
    assert '<field name="endpoint_code">nsp_controller_parking_detection_push</field>' in xml
    assert '<field name="route_suffix">parking/detections/push</field>' in xml
