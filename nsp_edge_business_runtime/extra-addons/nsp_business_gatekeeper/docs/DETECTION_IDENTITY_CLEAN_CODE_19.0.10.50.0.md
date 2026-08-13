# Detection Identity / Clean Code — 19.0.10.50.0

## Decision

`Resolved Vehicle` and `Resolved User` are one conceptual UI attribute: **Resolved Identity**.

Runtime persistence still keeps `vehicle_id` and `user_id` as mutually-exclusive typed Many2one relations because they point to different business models, support efficient partial indexes, preserve FK integrity, and snapshot the identity resolved at ingest even if the runtime RFID assignment later changes.

The Detection Logs list/form now exposes one display-only `resolved_identity` field. Existing Vehicle/User search filters continue to use the typed backend relations.

## Clean-code boundary

- Detection runtime owns: receipt, topology resolution, sequence matching, User time correlation, consumption/expiry.
- Parking business owns: Check-in/Check-out state, Owner/Borrow authorization, final Parking Log decision.
- Detection no longer calls `_authorized_user_borrow_map`.
- `create_from_detection_group` accepts at most one supporting User read and performs authorization itself.
- The matcher key `duration_seconds` is renamed to `window_seconds` because the value is the configured correlation/sequence window, not the observed traversal duration.

## Integrity

A SQL CHECK prevents a Detection Log from containing both `user_id` and `vehicle_id`. Error/unresolved rows may still contain neither.
