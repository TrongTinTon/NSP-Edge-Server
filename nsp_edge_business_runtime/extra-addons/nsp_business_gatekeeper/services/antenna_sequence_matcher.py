# -*- coding: utf-8 -*-
"""Pure ordered Antenna Sequence matcher used by the Edge parking runtime.

The matcher deliberately operates on the raw event stream. Repeated reads are
interpreted only after a Lane Configuration supplies the expected Reader/Port
sequence and each transition's Max Duration.
"""


def match_ordered_sequence_details(events, expected_keys, allowed_durations, *, key_of, time_of):
    """Return ordered Lane matches while tolerating normal RFID overlap.

    ``expected_keys`` is the configured ordered Reader/Port path.
    ``allowed_durations[i]`` is the Max Duration from selected step ``i - 1``
    to selected step ``i``; index 0 is ignored.

    RFID read zones overlap in real installations. After a Vehicle has progressed
    from A to B, Reader A can still report the same TID; likewise Reader C can be
    observed briefly before B produces the selected transition read. Those overlap
    observations must not rewind or invalidate an otherwise ordered traversal.

    Matching therefore advances only on the *immediate next* configured point.
    Reads of already-passed points and premature future points are claimed as RF
    overlap and ignored for progression while the active transition is still
    within its Max Duration. Once the active transition deadline has expired, the
    candidate is dropped; a new read of the first point may immediately start a
    new traversal.

    ``path`` contains only selected observations. ``consumed_events`` additionally
    contains repeated/overlap reads claimed by the successful traversal so they can
    be removed from the short-lived Detection buffer after the Parking Log commits.
    """
    expected_keys = tuple(expected_keys or ())
    allowed_durations = tuple(allowed_durations or ())
    if len(expected_keys) < 2:
        return []
    if len(allowed_durations) != len(expected_keys):
        raise ValueError("allowed_durations must align with expected_keys")
    if len(set(expected_keys)) != len(expected_keys):
        raise ValueError("expected_keys must be unique")

    key_index = {key: index for index, key in enumerate(expected_keys)}
    matches = []
    path = []
    claimed = []
    step_index = -1
    last_index = len(expected_keys) - 1

    def reset():
        nonlocal path, claimed, step_index
        path = []
        claimed = []
        step_index = -1

    def start(event):
        nonlocal path, claimed, step_index
        path = [event]
        claimed = [event]
        step_index = 0

    for event in events:
        key = key_of(event)
        detected_at = time_of(event)
        event_index = key_index.get(key)

        if not path:
            if event_index == 0:
                start(event)
            continue

        # Before interpreting overlap, expire a candidate once the immediate next
        # transition can no longer satisfy its configured Max Duration. This also
        # lets a new first-point read begin a later physical crossing cleanly.
        next_index = step_index + 1
        if next_index <= last_index:
            current_at = time_of(path[step_index])
            next_allowed = max(0.001, float(allowed_durations[next_index] or 0.0))
            if (detected_at - current_at).total_seconds() > next_allowed:
                reset()
                if event_index == 0:
                    start(event)
                continue

        current_key = expected_keys[step_index]
        if key == current_key:
            claimed.append(event)

            # Repeated current-point reads may improve the selected transition
            # anchor. The first point can always move forward. Internal points can
            # move only while their preceding transition remains valid.
            if step_index == 0:
                path[0] = event
                continue

            previous_at = time_of(path[step_index - 1])
            gap = (detected_at - previous_at).total_seconds()
            allowed = max(0.001, float(allowed_durations[step_index] or 0.0))
            if 0 <= gap <= allowed:
                path[step_index] = event
            continue

        next_index = step_index + 1
        if next_index <= last_index and event_index == next_index:
            current_at = time_of(path[step_index])
            gap = (detected_at - current_at).total_seconds()
            allowed = max(0.001, float(allowed_durations[next_index] or 0.0))
            if 0 <= gap <= allowed:
                path.append(event)
                claimed.append(event)
                step_index = next_index
                if step_index == last_index:
                    matches.append({
                        "path": tuple(path),
                        "consumed_events": tuple(claimed),
                    })
                    reset()
            else:
                # Defensive fallback; the deadline check above normally handles
                # this branch before we reach it.
                reset()
                if event_index == 0:
                    start(event)
            continue

        if event_index is not None:
            # Same TID observed by an already-passed antenna or by a future antenna
            # before the immediate next transition. This is normal RFID overlap,
            # not evidence that the Vehicle reversed direction. Claim the read so
            # a later successful traversal can clean it, but do not alter progress.
            claimed.append(event)
            continue

        # Topology normally guarantees every candidate Reader/Port belongs to this
        # Lane sequence. An unrelated key is ignored rather than poisoning progress.

    return matches


def match_ordered_sequence(events, expected_keys, allowed_durations, *, key_of, time_of):
    """Backward-compatible path-only view of :func:`match_ordered_sequence_details`."""
    return [
        item["path"]
        for item in match_ordered_sequence_details(
            events,
            expected_keys,
            allowed_durations,
            key_of=key_of,
            time_of=time_of,
        )
    ]
