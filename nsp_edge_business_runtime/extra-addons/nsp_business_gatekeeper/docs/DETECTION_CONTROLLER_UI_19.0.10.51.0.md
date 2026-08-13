# Detection Controller UI — 19.0.10.51.0

## Changes

- Removed `Reader Serial` from Detection Logs search, list, and form views.
- Kept `serial_number` only as backend raw evidence when Reader Master cannot be resolved.
- Fixed resolved Detection candidates to persist the source `controller_id`.
- Updated idempotency comparison so Controller remains immutable acquisition provenance.
- Backfilled existing resolved Detection rows with missing Controller from their Lane Configuration during module initialization/upgrade.

## Ownership

`controller_id` is physical acquisition provenance and belongs to the Detection observation. `layout_lane_id.controller_id` remains contextual topology. Topology resolution already guarantees they agree before a resolved candidate is created.
