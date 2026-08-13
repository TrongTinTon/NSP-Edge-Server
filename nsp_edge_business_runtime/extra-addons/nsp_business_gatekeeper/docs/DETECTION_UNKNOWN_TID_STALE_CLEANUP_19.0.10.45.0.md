# Detection unknown-TID filtering and Lane-scoped stale cleanup — 19.0.10.45.0

## Runtime rule

Detection Logs are an Edge Parking working buffer, not a raw RFID receipt archive.
A Controller observation whose normalized TID has no `nsp.rfid.runtime.assignment`
is ignored before topology resolution and before persistence.

This means an unknown TID creates neither:

- a Lane candidate row; nor
- an `rfid_assignment_not_found` error row.

The response statistic `ignored_unknown_tid_detections` reports how many transport-valid
observations were intentionally ignored in the batch.

A TID that *does* exist in runtime assignment can still produce a short-lived
configuration diagnostic when its assignment is invalid/inactive or when its
Reader/Port/Lane topology cannot be resolved.

## Stale candidate cleanup

`_drop_expired_pending(lane, now, identity_field)` remains intentionally scoped to
one `layout_lane_id` and computes its cutoff from that Lane Configuration's own
`max_sequence_window()`.

For the same physical `event_uid` fanned out to two Lane candidates:

- Lane A may expire and delete only the Lane A candidate;
- Lane B remains pending if its own larger sequence window has not expired.

Stale cleanup must therefore **not** delete by `event_uid`. Cross-Lane `event_uid`
cleanup is reserved for a successfully consumed physical traversal, where the Lane
has already been resolved by sequence matching.
