# Detection / Parking Log optimization — 19.0.10.39.0

## Storage policy

`nsp.parking.detection.event` is an Edge working buffer, not Parking history.

- Pending RFID candidates exist only while they can still complete an Antenna Sequence.
- Successful, duplicate, ignored, and stale/incomplete candidates are consumed and deleted.
- Only structured resolution/processing errors are retained temporarily; default retention is 24 hours (`nsp.parking_detection_error_retention_hours`).
- There is no Detection `state`, `error_message`, or presentation-only `lane_id`; `error_code IS NULL` means pending and a non-null `error_code` is the complete structured error state. Lane is already represented by `layout_lane_id`.
- RSSI is accepted on the Controller API for wire compatibility but is not persisted by Parking Detection runtime.
- `parking_log_id` and Parking Log `source_detection_ids` are removed. A final Parking Log is authoritative and self-contained.
- Resolved candidates do not repeat Controller ID or raw Reader serial. Error rows store raw serial only when Reader resolution fails, and do not duplicate resolved User/Vehicle references because TID is sufficient for short-lived diagnostics.
- Sequence timeout/noise is not retained as an error history.

`nsp.parking.log` remains the durable business history. It intentionally keeps direct Parking Area, Lane, Lane Configuration, revision, Vehicle/User identity snapshots and final decision fields because those values are historical evidence and/or hot-query dimensions.

## Query / write-path policy

- Detection has only partial/composite indexes that serve pending matching, error cleanup, and UID idempotency. Resolved and unresolved UIDs use separate partial unique indexes instead of one full-table unique index.
- Candidate rows are prepared in memory and inserted with one ORM batch instead of one savepoint/INSERT per Lane candidate.
- Existing event UIDs are prefetched once per batch; unique indexes remain the concurrency guard with a per-row fallback only for a rare race.
- Runtime revision/state changes directly DELETE obsolete pending work instead of converting normal lifecycle churn into retained error logs; expired pending cleanup also uses direct SQL.
- Successful movement consumes all sibling Lane copies by `event_uid`; repeated Vehicle reads inside the matched movement window are also deleted.
- Logical Lane processing inside one Parking Area is serialized with an advisory transaction lock so sibling fan-out candidates cannot create competing Parking Logs.
- Parking Log creation no longer re-browses Lane Configuration after the business helper has already derived Parking Area/Lane/revision.
- Cloud sync no longer traverses Detection rows for Parking Log serialization.
- Per-detection unresolved/rejected application messages are DEBUG only; the structured Detection Error row is the diagnostic source. Exceptions remain ERROR-level.

## Result

The Detection table now scales with **current work + short-lived errors**, instead of RFID traffic history. This reduces row count, row width, index write amplification, WAL/VACUUM pressure, ORM allocations and reverse-relation queries while preserving Controller idempotency and durable Parking business history.
