from datetime import datetime, timedelta
import importlib.util
from pathlib import Path
from types import SimpleNamespace


_SERVICE = Path(__file__).resolve().parents[1] / "services" / "antenna_sequence_matcher.py"
_SPEC = importlib.util.spec_from_file_location("nsp_antenna_sequence_matcher", _SERVICE)
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
match_ordered_sequence = _MODULE.match_ordered_sequence


BASE = datetime(2026, 8, 13, 0, 0, 0)


def event(key, seconds, event_id):
    return SimpleNamespace(key=key, detected_at=BASE + timedelta(seconds=seconds), id=event_id)


def match(events, expected, allowed):
    return match_ordered_sequence(
        events,
        expected,
        allowed,
        key_of=lambda item: item.key,
        time_of=lambda item: item.detected_at,
    )


def ids(path):
    return [item.id for item in path]


def test_repeated_first_point_uses_latest_anchor_before_next_transition():
    # Old collapse-first behavior kept A@0 and incorrectly rejected B@12.
    events = [event("A", 0, 1), event("A", 10, 2), event("B", 12, 3)]

    matches = match(events, ["A", "B"], [0, 3])

    assert [ids(path) for path in matches] == [[2, 3]]


def test_repeated_internal_point_replaces_only_when_previous_transition_stays_valid():
    # B@3 would be a better anchor for C but violates A->B <= 2s, so B@1 must stay.
    events = [
        event("A", 0, 1),
        event("B", 1, 2),
        event("B", 3, 3),
        event("C", 4, 4),
    ]

    matches = match(events, ["A", "B", "C"], [0, 2, 5])

    assert [ids(path) for path in matches] == [[1, 2, 4]]


def test_repeated_internal_point_prefers_latest_valid_anchor():
    events = [
        event("A", 0, 1),
        event("B", 1, 2),
        event("B", 1.5, 3),
        event("C", 2.0, 4),
    ]

    matches = match(events, ["A", "B", "C"], [0, 2, 1])

    assert [ids(path) for path in matches] == [[1, 3, 4]]


def test_out_of_order_lane_point_preserves_strict_sequence_semantics():
    events = [
        event("A", 0, 1),
        event("C", 0.5, 2),
        event("B", 1.0, 3),
        event("C", 1.5, 4),
    ]

    assert match(events, ["A", "B", "C"], [0, 2, 2]) == []


def test_too_late_next_point_does_not_prevent_new_repeated_start_anchor():
    events = [
        event("A", 0, 1),
        event("B", 10, 2),
        event("A", 11, 3),
        event("B", 12, 4),
    ]

    matches = match(events, ["A", "B"], [0, 3])

    assert [ids(path) for path in matches] == [[3, 4]]


def test_late_next_point_then_internal_repeat_does_not_bypass_strict_order():
    # A->B is valid, but the first C arrives too late. A later B/C pair must not
    # reuse the old A and manufacture a non-contiguous A->B->C traversal.
    events = [
        event("A", 0, 1),
        event("B", 1, 2),
        event("C", 10, 3),
        event("B", 11, 4),
        event("C", 12, 5),
    ]

    assert match(events, ["A", "B", "C"], [0, 5, 2]) == []


def test_multiple_crossings_return_non_overlapping_matches_in_time_order():
    events = [
        event("A", 0, 1), event("A", 0.2, 2), event("B", 1, 3),
        event("A", 10, 4), event("B", 11, 5),
    ]

    matches = match(events, ["A", "B"], [0, 2])

    assert [ids(path) for path in matches] == [[2, 3], [4, 5]]


def match_details(events, expected, allowed):
    return _MODULE.match_ordered_sequence_details(
        events,
        expected,
        allowed,
        key_of=lambda item: item.key,
        time_of=lambda item: item.detected_at,
    )


def test_successful_match_claims_replaced_first_point_for_cleanup():
    events = [event("A", 0, 1), event("A", 1, 2), event("B", 2, 3)]

    details = match_details(events, ["A", "B"], [0, 3])

    assert len(details) == 1
    assert ids(details[0]["path"]) == [2, 3]
    assert ids(details[0]["consumed_events"]) == [1, 2, 3]


def test_successful_match_claims_internal_repeated_read_even_when_not_selected_anchor():
    # B@3 cannot replace B@1 because A->B <= 2s, but it is still a repeated
    # observation inside the successful A/B/C traversal and must be cleaned.
    events = [
        event("A", 0, 1),
        event("B", 1, 2),
        event("B", 3, 3),
        event("C", 4, 4),
    ]

    details = match_details(events, ["A", "B", "C"], [0, 2, 5])

    assert len(details) == 1
    assert ids(details[0]["path"]) == [1, 2, 4]
    assert ids(details[0]["consumed_events"]) == [1, 2, 3, 4]


def test_invalidated_candidate_reads_are_not_claimed_by_later_successful_crossing():
    events = [
        event("A", 0, 1),
        event("C", 0.5, 2),  # invalidates A@0 candidate
        event("A", 10, 3),
        event("B", 11, 4),
        event("C", 12, 5),
    ]

    details = match_details(events, ["A", "B", "C"], [0, 2, 2])

    assert len(details) == 1
    assert ids(details[0]["path"]) == [3, 4, 5]
    assert ids(details[0]["consumed_events"]) == [3, 4, 5]
