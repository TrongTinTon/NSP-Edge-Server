from datetime import datetime, timedelta
import importlib.util
from pathlib import Path
from types import SimpleNamespace

_SERVICE = Path(__file__).resolve().parents[1] / "services" / "antenna_sequence_matcher.py"
_SPEC = importlib.util.spec_from_file_location("nsp_overlap_matcher", _SERVICE)
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)

BASE = datetime(2026, 8, 13, 0, 0, 0)


def event(key, seconds, event_id):
    return SimpleNamespace(key=key, detected_at=BASE + timedelta(seconds=seconds), id=event_id)


def details(events, expected=("A", "B", "C"), allowed=(0, 2, 2)):
    return _MODULE.match_ordered_sequence_details(
        events,
        expected,
        allowed,
        key_of=lambda item: item.key,
        time_of=lambda item: item.detected_at,
    )


def ids(items):
    return [item.id for item in items]


def test_previous_antenna_reread_after_progress_does_not_reset_sequence():
    # Real RFID overlap: A continues reading the tag after B has already progressed.
    result = details([
        event("A", 0.0, 1),
        event("B", 0.8, 2),
        event("A", 1.0, 3),
        event("C", 1.5, 4),
    ])
    assert len(result) == 1
    assert ids(result[0]["path"]) == [1, 2, 4]
    assert ids(result[0]["consumed_events"]) == [1, 2, 3, 4]


def test_premature_future_antenna_read_does_not_poison_ordered_progression():
    # C can briefly see the tag before B due to RF overlap; only B->C advances.
    result = details([
        event("A", 0.0, 1),
        event("C", 0.3, 2),
        event("B", 0.8, 3),
        event("C", 1.2, 4),
    ])
    assert len(result) == 1
    assert ids(result[0]["path"]) == [1, 3, 4]
    assert ids(result[0]["consumed_events"]) == [1, 2, 3, 4]


def test_expired_partial_path_allows_new_first_point_to_restart_crossing():
    result = details([
        event("A", 0.0, 1),
        event("B", 0.8, 2),
        # Old B->C deadline is 2.8. A@10 is therefore a new traversal, not overlap.
        event("A", 10.0, 3),
        event("B", 10.8, 4),
        event("C", 11.5, 5),
    ])
    assert len(result) == 1
    assert ids(result[0]["path"]) == [3, 4, 5]


def test_reverse_direction_without_ordered_progress_still_does_not_match():
    result = details([
        event("C", 0.0, 1),
        event("B", 0.5, 2),
        event("A", 1.0, 3),
    ])
    assert result == []


def test_selected_transition_gaps_still_enforce_max_duration():
    result = details([
        event("A", 0.0, 1),
        event("B", 3.0, 2),  # A->B exceeds 2 sec.
        event("C", 3.5, 3),
    ])
    assert result == []
