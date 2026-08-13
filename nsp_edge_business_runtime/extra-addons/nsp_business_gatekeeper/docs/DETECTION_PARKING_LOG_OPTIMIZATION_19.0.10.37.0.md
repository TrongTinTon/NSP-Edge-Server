# Detection / Parking Log optimization — 19.0.10.37.0

## Business boundary

### Detection Log

`nsp.parking.detection.event` is short-retention Edge technical evidence and runtime matching state. It is not business history and is never synchronized to Cloud as a Detection Log.

It keeps only data that is either:

- observed by Controller: `event_uid`, `detected_at`, `controller_id`, `serial_number`, `port_no`, `tid`, `rssi_dbm`; or
- required to freeze Edge runtime resolution while the detection is pending: `layout_lane_id`, `reader_id`, `layout_revision`, `user_id` / `vehicle_id`, `state`, `error_code`, `parking_log_id`.

`lane_id` is no longer persisted. It is a non-stored related projection of `layout_lane_id.lane_id`. The Lane Configuration is already the authoritative contextual identity for a Detection candidate.

Repeated human-readable `error_message` text is no longer stored. `error_code` is the compact processing result and the UI message is computed from it. Processed sibling/stale outcomes use explicit result codes (`consumed_by_other_lane`, `stale_movement`).

### Parking Log

`nsp.parking.log` remains immutable long-lived business history. Its direct `parking_area_id` and `lane_id` are intentionally retained even though they are derivable from `layout_lane_id`: they are stable business identities and leading keys for hot history/Live Monitor queries. This is purposeful denormalization, not redundant storage.

`vehicle_tid` / `user_tid` remain evidence snapshots because RFID assignments can change after the parking movement. `layout_revision` remains required to identify the runtime configuration that made the decision.

## Query / write-path optimization

### Detection indexes

Previous schema created standalone indexes on nearly every searchable Detection field in addition to partial/composite runtime indexes. On a high-write RFID table this causes unnecessary write amplification.

The optimized schema keeps only indexes aligned to runtime queries:

- pending Lane ordering: `(layout_lane_id, detected_at, id)` for unconsumed pending rows;
- Vehicle sequence matcher: `(layout_lane_id, layout_revision, tid, detected_at, id)` for pending Vehicle rows;
- User authorization window: `(layout_lane_id, layout_revision, detected_at, id)` for pending User rows;
- retention cleanup: `(detected_at, id)` for terminal rows;
- unresolved raw idempotency: unique `event_uid WHERE layout_lane_id IS NULL`;
- resolved candidate idempotency remains `unique(event_uid, layout_lane_id)`.

Legacy standalone Odoo indexes are dropped during module upgrade. The two obsolete physical columns (`lane_id`, stored `error_message`) are removed by the 19.0.10.37.0 post-migration rather than by normal runtime code.

### Batch runtime snapshot

A Controller batch may contain hundreds of reads for the same Parking Area. The old ingest path acquired the same Parking Area advisory lock and invalidated/read `state` + `published_revision` for every candidate row.

The new path:

1. resolves topology for the batch;
2. gathers unique Parking Areas;
3. acquires each shared Area lock once in deterministic ID order;
4. reads one immutable runtime snapshot per Area;
5. reuses the cached revision/state for all candidate inserts in the transaction.

This preserves the revision-race guarantee while removing repeated SQL round-trips.

### Removed duplicate validation

The per-row Detection constraint that traversed `Lane Configuration -> Reader Config -> Ports` was removed from the high-volume write path. Candidate rows are created only after `_resolve_topology_batch()` has already proven the Reader/Port belongs to that contextual Lane. UI users cannot create/edit Detection Logs.

### Retention cleanup

The previous cron loaded up to 20,000 ORM records and called `unlink()`. At RFID volume this can fall behind and wastes Python memory/prefetch work.

Cleanup now deletes terminal rows with an indexed PostgreSQL CTE in bounded batches (50,000 rows x up to 10 batches per cron run), then invalidates ORM caches. Retention semantics remain unchanged.

### Parking Log index

The standalone `reason_code` index is removed. It is low-cardinality and used primarily for operator filtering, not a business hot path. Existing purpose-built indexes for Vehicle continuity, Parking Area history/Live Monitor and duplicate suppression remain unchanged.

## Intentionally not changed

The flat per-Lane Detection candidate fan-out is retained. Splitting one physical read into a raw table plus a candidate table would reduce duplicated source columns when one antenna belongs to many logical Lanes, but it would also add a join and an extra row/table to every sequence-matching query. That normalization should only be introduced after measuring a real fan-out/storage problem in production (`avg candidates per event_uid`, table/index bytes, pending query plans).

No Check-in/Check-out, Vehicle continuity, authorization, deterministic log UID, Controller API payload or Cloud Parking Log contract is changed in this version.
