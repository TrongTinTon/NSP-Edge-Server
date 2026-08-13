from pathlib import Path


def test_detection_working_buffer_keeps_known_tid_resolution_errors():
    root = Path(__file__).resolve().parents[1]
    api = (root / 'models' / 'api_service.py').read_text(encoding='utf-8')
    model = (root / 'models' / 'parking_detection_event.py').read_text(encoding='utf-8')
    view = (root / 'views' / 'parking_detection_event_views.xml').read_text(encoding='utf-8')

    assert 'accepted = [' not in api
    assert 'assignment_by_tid.get(payload["tid"], RuntimeAssignment.browse())' in api
    assert '"error_records_created"' in api
    assert '"persisted"' in api
    assert 'Parking detections accepted by Edge.' in api

    assert 'def _unresolved_detection_values' in model
    # Legacy selection may remain for upgrade compatibility, but new unknown TIDs
    # are filtered before any Detection Log row is created.
    assert 'known_detections = []' in model
    assert 'ignored_unknown_tid_detections += 1' in model
    assert '"device_not_found"' in model
    assert '"no_reader_port_timeline"' in model
    assert '"controller_not_in_scope"' in model
    assert '"ambiguous_reader_port_layout"' in model
    assert 'nsp_parking_detection_unresolved_uid_unique' in model
    assert 'string="Controller"' in model
    assert 'string="Reader Serial"' in model

    # Known-TID resolution errors can still exist without a resolved Lane/Reader.
    assert '"nsp.parking.layout.lane", string="Lane Configuration", required=True' not in model
    assert '"nsp.device", string="Reader", required=True' not in model

    assert '<field name="controller_id" optional="show"/>' in view
    assert '<field name="serial_number" optional="show"/>' in view
