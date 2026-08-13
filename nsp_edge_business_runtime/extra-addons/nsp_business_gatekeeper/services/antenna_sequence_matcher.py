# -*- coding: utf-8 -*-
"""Pure ordered Antenna Sequence matcher used by the Edge parking runtime.

The matcher deliberately operates on the raw event stream. Repeated reads are
interpreted only after a Lane Configuration supplies the expected Reader/Port
sequence and each transition's Max Duration.
"""


def match_ordered_sequence_details(events, expected_keys, allowed_durations, *, key_of, time_of):
    """Return ordered matches plus every raw read claimed by each traversal.

    ``expected_keys`` contains the Lane Configuration Reader/Port path.
    ``allowed_durations[i]`` is the Max Duration from step ``i - 1`` to step
    ``i``; index 0 is ignored.

    ``path`` contains only the observations that become the matched Antenna
    Sequence. ``consumed_events`` additionally contains repeated observations of
    the current sequence point that the matcher intentionally ignored or replaced
    while building that successful traversal. This lets the working-buffer cleanup
    remove those reads after the crossing is committed without collapsing the raw
    timeline before matching.

    Reads from another sequence point keep the strict-order contract: the first
    point starts/restarts a candidate and any other out-of-order point invalidates
    the current candidate. Observing the immediate next point after its Max
    Duration also invalidates that traversal.
    """
    expected_keys = tuple(expected_keys or ())
    allowed_durations = tuple(allowed_durations or ())
    if len(expected_keys) < 2:
        return []
    if len(allowed_durations) != len(expected_keys):
        raise ValueError("allowed_durations must align with expected_keys")

    matches = []
    path = []
    claimed = []
    step_index = -1
    last_index = len(expected_keys) - 1

    for event in events:
        key = key_of(event)
        detected_at = time_of(event)

        if not path:
            if key == expected_keys[0]:
                path = [event]
                claimed = [event]
                step_index = 0
            continue

        current_key = expected_keys[step_index]
        if key == current_key:
            # This physical read belongs to the active traversal even when it does
            # not replace the selected anchor. Keep it in ``claimed`` so a
            # successful crossing can clean it from the working buffer later.
            claimed.append(event)

            # Repeated observation of the current physical point. For the first
            # point, the newest observation is always the best anchor. For a later
            # point, replace only if its preceding transition stays valid.
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
        if next_index <= last_index and key == expected_keys[next_index]:
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
                    path = []
                    claimed = []
                    step_index = -1
            else:
                # The immediate next Lane point was observed, but outside its
                # configured Max Duration. Preserve strict-order semantics: this
                # candidate is no longer a valid traversal and none of its reads
                # are claimed by a later successful match.
                path = []
                claimed = []
                step_index = -1
            continue

        if key == expected_keys[0]:
            # A new first point starts a distinct candidate. Abandoned reads from
            # the invalidated candidate remain pending until their own expiry.
            path = [event]
            claimed = [event]
            step_index = 0
        else:
            # Preserve strict ordered-sequence semantics for other Lane points.
            path = []
            claimed = []
            step_index = -1

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
