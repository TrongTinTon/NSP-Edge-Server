from pathlib import Path


def test_raw_detection_is_persisted_before_business_resolution():
    root = Path(__file__).resolve().parents[1]
    api = (root / 'models' / 'api_service.py').read_text(encoding='utf-8')
    model = (root / 'models' / 'parking_detection_event.py').read_text(encoding='utf-8')
    view = (root / 'views' / 'parking_detection_event_views.xml').read_text(encoding='utf-8')

    assert 'accepted = [' not in api
    assert 'assignment_by_tid.get(payload["tid"], RuntimeAssignment.browse())' in api
    assert '"error_records_created"' in api
    assert '"persisted"' in api
    assert 'Parking detections accepted and persisted on Edge.' in api

    assert 'def _persist_unresolved_detection' in model
    assert '"rfid_assignment_not_found"' in model
    assert '"device_not_found"' in model
    assert '"no_reader_port_timeline"' in model
    assert '"controller_not_in_scope"' in model
    assert '"ambiguous_reader_port_layout"' in model
    assert 'nsp_parking_detection_unresolved_uid_unique' in model
    assert 'string="Controller"' in model
    assert 'string="Reader Serial"' in model

    # Lane/Reader resolution is no longer required to prove raw receipt.
    assert '"nsp.parking.layout.lane", string="Lane Configuration", required=True' not in model
    assert '"nsp.device", string="Reader", required=True' not in model

    assert '<field name="controller_id" optional="show"/>' in view
    assert '<field name="serial_number" optional="show"/>' in view
