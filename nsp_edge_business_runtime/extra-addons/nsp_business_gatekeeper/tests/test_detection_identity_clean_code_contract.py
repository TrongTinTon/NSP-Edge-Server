from pathlib import Path


def _root():
    return Path(__file__).resolve().parents[1]


def test_detection_ui_exposes_one_resolved_identity_column():
    root = _root()
    model = (root / "models" / "parking_detection_event.py").read_text(encoding="utf-8")
    view = (root / "views" / "parking_detection_event_views.xml").read_text(encoding="utf-8")

    assert 'resolved_identity = fields.Char(' in model
    list_start = view.index('<list string="Detection Logs"')
    list_end = view.index('</list>', list_start)
    list_view = view[list_start:list_end]
    assert '<field name="resolved_identity" widget="nsp_detection_identity"' in list_view
    assert '<field name="vehicle_id" optional="show"/>' not in list_view
    assert '<field name="user_id" optional="show"/>' not in list_view


def test_detection_runtime_keeps_typed_relations_and_xor_integrity():
    root = _root()
    model = (root / "models" / "parking_detection_event.py").read_text(encoding="utf-8")

    assert 'user_id = fields.Many2one(' in model
    assert 'vehicle_id = fields.Many2one(' in model
    assert 'CHECK(NOT (user_id IS NOT NULL AND vehicle_id IS NOT NULL))' in model


def test_detection_correlation_does_not_own_owner_borrow_authorization():
    root = _root()
    detection = (root / "models" / "parking_detection_event.py").read_text(encoding="utf-8")
    business = (root / "models" / "parking_log_business.py").read_text(encoding="utf-8")

    process_start = detection.index("def _process_sequence_matches")
    process_end = detection.index("def _process_pending_for_lane", process_start)
    process = detection[process_start:process_end]
    assert "_authorized_user_borrow_map" not in process
    assert "authorized_borrow_map" not in process
    assert "authorized, borrow = self._resolve_user_authorization(" in business


def test_match_window_is_named_as_window_not_observed_duration():
    root = _root()
    detection = (root / "models" / "parking_detection_event.py").read_text(encoding="utf-8")

    assert '"window_seconds": total_allowed' in detection
    assert 'match["duration_seconds"]' not in detection
