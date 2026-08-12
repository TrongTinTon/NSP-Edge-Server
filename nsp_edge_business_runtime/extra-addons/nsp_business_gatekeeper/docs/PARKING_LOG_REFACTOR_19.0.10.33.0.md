# Parking Log refactor — 19.0.10.33.0

## Decision

The Edge model previously named `nsp.parking.transaction` represented an immutable resolved business event, not an accounting/ACID transaction. It is now `nsp.parking.log` / **Parking Logs**.

The external Cloud transport kind `parking_transaction` is temporarily supported for rolling compatibility. It is a route/contract name only; Edge no longer models the business history as a Transaction.

## Storage boundary

Long-lived Parking Log keeps only:

- `log_uid`
- `event_time`, `event_type`
- `decision`, `reason_code`
- `parking_area_id`, `layout_lane_id`, `lane_id`, `layout_revision`
- `vehicle_id`, `vehicle_tid`
- `user_id`, `user_tid`
- `borrow_id`

Removed from long-lived Parking Log:

- Controller/code snapshots
- Parking Area/Lane code snapshots
- Reader/Serial/Port
- primary detection pointer
- sequence path and timing diagnostics
- vehicle/user/borrow code snapshots
- license plate snapshot
- observed user lists
- stored detection count

Technical evidence remains in `nsp.parking.detection.event` under short retention. Configuration remains in Parking Layout/Lane Configuration. Stable direct FKs (`parking_area_id`, `lane_id`) are intentionally retained as query keys.

`nsp.parking.log` uses `_log_access = False`: immutable business history already has `event_time` and `log_uid`, so `create_uid/create_date/write_uid/write_date` are not stored. This leaves 14 stored business columns plus the database `id`; One2many/computed display fields do not add table columns.

## Business logic corrections

1. Check-in/Check-out is resolved once from the latest **allowed** Parking Log while holding the Vehicle advisory transaction lock.
2. Denied logs never establish Vehicle presence.
3. A late event older than the latest allowed log is ignored rather than rewriting current continuity.
4. Physical duplicate suppression runs **before** Check-in/Check-out resolution and is event-type agnostic. This prevents a duplicate Check-in sequence from becoming a false Check-out after state changes.
5. After acquiring the Vehicle advisory lock, detection state is invalidated/re-read. A sibling `event_uid` already consumed by another logical Lane wins the crossing, preventing stale ORM cache from creating a second movement when one physical Reader/Antenna is shared by multiple logical Lanes.
6. Log UID is deterministic (`uuid5`) from Lane context + revision + source detection UIDs. Idempotent retry is therefore real, rather than using a newly generated random UUID on every attempt.
7. A final continuity/configuration denial does not wait for User authorization that cannot change the outcome.
8. Check-out authorization loads active owner/borrower authorization once and reuses the resolved Borrow record for final log creation.

## Clean-code split

Parking Log responsibilities are separated without changing the Odoo model API:

- `parking_log.py`: lean schema, indexes, constraints, immutable persistence
- `parking_log_business.py`: Vehicle state, Check-in/Check-out, authorization, deterministic idempotency
- `parking_log_live.py`: Live Monitor projection and bus broadcast only

The Detection processor remains acquisition/matching orchestration and calls the same `nsp.parking.log` model contract.

## Query changes

Purpose-built indexes:

- latest Vehicle state: `(vehicle_id, event_time DESC, id DESC) WHERE decision='allowed'`
- Live Monitor/history: `(parking_area_id, event_time DESC, id DESC)`
- duplicate suppression: `(layout_lane_id, layout_revision, vehicle_id, event_time DESC, id DESC)`

Other changes:

- Live Monitor uses direct `parking_area_id` instead of an OR domain with related-field join.
- User detection pool is time-bounded to the match range instead of loading every pending User read for a Lane.
- runtime-revision invalidation is performed in PostgreSQL with `layout_revision != current`, not ORM `.filtered()` over all pending records.
- per-Lane processing invalidates only the current Lane; full-area invalidation remains reserved for runtime snapshot changes.
- Cloud legacy serialization explicitly batch-prefetches referenced context and detection relations.
- New detection/log idempotency uses optimistic INSERT and performs a SELECT only after an actual unique-key conflict.
- Parking Log Cloud push uses the immutable monotonic `id` as cursor (`id > last_push_record_id`) instead of `write_date + id`, allowing `_log_access = False`.
- Low-cardinality/redundant standalone indexes for `event_type`, `decision`, `layout_revision`, and the duplicate `log_uid` btree are removed; the unique/composite indexes cover the hot paths.

## Migration

Upgrade from 19.0.10.32.0 requires a real Odoo module upgrade (`-u nsp_business_gatekeeper`). Migration 19.0.10.33.0:

- renames table `nsp_parking_transaction` -> `nsp_parking_log`
- renames `transaction_uid` -> `log_uid`
- renames `status` -> `decision`
- renames `error_code` -> `reason_code`; legacy free-form `error_message` is intentionally removed
- renames detection FK `transaction_id` -> `parking_log_id`
- preserves the existing Odoo model/field metadata identity and renames internal view/action/menu/ACL XML IDs from `transaction` to `parking_log`
- backfills direct Parking Area/Lane FKs
- removes obsolete snapshot/technical columns and Odoo log-access columns (`create_uid/create_date/write_uid/write_date`)

Do not deploy the source by restart only. The schema migration is mandatory.
