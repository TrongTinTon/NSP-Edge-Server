# Detection Identity Type Badge — 19.0.10.54.0

Detection Logs keep one `Identity` column.

The identity type alone is rendered as a badge:

- `[User] Tôn Trọng Tín`
- `[Vehicle] 62A1-154.23`

The User name or Vehicle license plate remains normal text. No additional Type column is introduced.

Backend `user_id` and `vehicle_id` remain separate typed relations. `resolved_identity` contains only the display label; the field widget derives the badge type from the already-loaded invisible relations.
