from pathlib import Path


def test_successful_sequence_consumes_matcher_claimed_repeated_reads():
    root = Path(__file__).resolve().parents[1]
    model = (root / "models" / "parking_detection_event.py").read_text(encoding="utf-8")
    matcher = (root / "services" / "antenna_sequence_matcher.py").read_text(encoding="utf-8")

    assert "def match_ordered_sequence_details" in matcher
    assert '"consumed_events": tuple(claimed)' in matcher
    assert '"consume_events": self.browse([event.id for event in consumed_events])' in model
    assert 'consume_vehicle_events = match.get("consume_events", source_events).exists()' in model
    assert "delete_events = (events | vehicle_events).exists()" in model
    assert "vehicle_events=consume_vehicle_events or vehicle_events" in model
    assert "failed_vehicle_events = (movement_events | consume_vehicle_events).exists().filtered(" in model


def test_cleanup_does_not_use_a_new_global_duplicate_window():
    root = Path(__file__).resolve().parents[1]
    model = (root / "models" / "parking_detection_event.py").read_text(encoding="utf-8")

    for forbidden in (
        "duplicate_tid_window",
        "duplicate_detection_window",
        "dedup_window",
    ):
        assert forbidden not in model
