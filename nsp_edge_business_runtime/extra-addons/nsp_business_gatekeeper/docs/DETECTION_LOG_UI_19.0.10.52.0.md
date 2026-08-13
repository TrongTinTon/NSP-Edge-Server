# Detection Log UI — 19.0.10.52.0

Detection Logs are intentionally reduced to the seven operator-facing fields required for runtime inspection:

1. Detected At
2. Controller
3. Reader
4. Port
5. RFID TID
6. Identity
7. Issue

## UI semantics

- `resolved_identity` is displayed as **Identity**. The backend continues to keep typed `vehicle_id` / `user_id` relations.
- `error_code` is displayed as **Issue**. The backend technical name remains unchanged to avoid a needless schema migration.
- Reader Serial, Lane Configuration, Layout Revision, Detection UID, Resolved Vehicle and Resolved User are not shown in the Detection Logs list/form.
- The search filter is renamed from **Errors** to **Issues**.
- Controller and Reader grouping remain available because they are useful for runtime diagnosis.
