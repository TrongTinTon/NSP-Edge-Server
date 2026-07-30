# NSP Gatekeeper fresh-install architecture

## Modules

- Cloud: `nsp_master_gatekeeper` owns Edge Servers, master Controllers/Readers/Antennas, Branches, Parking Configuration, Device Whitelist, Measurement management and Cloud history mirrors.
- Edge: `nsp_business_gatekeeper` owns the local Controller/Reader/Antenna runtime cache, parking detection/business processing, Parking Transactions, Measurement runtime, Live Monitor and Live Measurement. **Edge has no `nsp.edge.server` model.**
- `nsp_business_gatekeeper` depends on `nsp_sync`; installing Business Gatekeeper installs Sync automatically.
- `nsp_sync` is the Edge transport layer. Edge uses Cloud Connection, jobs, durable retry/ACK and authoritative snapshot application. Cloud source/receive endpoints are owned directly by `nsp_master_gatekeeper`.
- Legacy `nsp_gatekeeper` is removed. This package targets fresh install only.

## Cloud -> Edge master/config synchronization

`gatekeeper-config/sync` is an authoritative snapshot scoped to the authenticated Edge Server. The Cloud returns a monotonically increasing `revision` and the complete current Branch cache, Controllers, Readers, Antennas, Parking topology and Device Whitelist for that Edge.

Edge applies one snapshot atomically:

1. Reject an older revision.
2. Upsert current records.
3. Treat absence from the snapshot as the delete/revoke signal.
4. Remove transient cache rows or archive history-referenced runtime rows (`active=False`, `cloud_removed=True`).
5. Persist `snapshot_revision` only after the transaction succeeds.

Cloud hard-delete therefore converges without `write_date` polling or deletion tombstones. A Cloud archive also converges when the record is still present with an inactive state.

Users, Vehicles, RFID Cards, Vehicle Configuration, Vehicle Borrow and Measurement Configuration are full authoritative snapshots as well. Empty snapshots are meaningful and remove/archive all stale Edge cache rows for that resource.

## Edge -> Cloud business events

Parking Transactions, Measurement observations/status and runtime reports are not snapshots. They use durable push + stable UID + ACK/retry. Business events are never discarded merely because Cloud is unavailable.

- Parking Transaction: every record is delivered; no coalescing.
- Measurement observations: stable `event_uid`, revision/power snapshot retained until ACK.
- Runtime status: current state can be refreshed without becoming business history.

Raw parking detections remain Edge-local with retention cleanup; Cloud receives final Parking Transactions instead of raw detection history.

## Historical transaction durability

Cloud does not re-run current Parking topology rules when receiving a final Parking Transaction from Edge. A transaction may arrive after its Controller/Lane/Reader/Antenna was changed or deleted on Cloud. The Cloud therefore stores compact immutable identifiers (`controller_code`, `parking_area_code`, `lane_code`, `serial_number`, `antenna_no`, vehicle/license snapshots) and links to current master records only when they still exist. Master topology can be deleted without destroying or blocking delayed Parking history.

## Installation dependency boundary

- `nsp_business_gatekeeper` depends on `nsp_sync`; installing the Edge business module installs Sync automatically.
- `nsp_master_gatekeeper` does not depend on `nsp_sync`; it owns the Cloud-side authoritative snapshot and business-event endpoints directly.
- `nsp_mobile` depends on `nsp_master_gatekeeper`, never the reverse. Installing Master must not auto-install Mobile (`auto_install=False`).
- Cloud and Edge Gatekeeper modules are deployment-role alternatives and are not intended to be installed together in the same database.

## Odoo model extension rule

Configuration revision hooks extend existing Odoo models using `models.Model` only. Plain Python mixins must not be added as additional bases to `_inherit` extension classes because Odoo rebuilds model bases dynamically during registry setup. Shared revision behavior is implemented with module-level helper functions instead.

## Parking antenna transition timing

Parking movement validation is configured as directed antenna transitions on Cloud and synchronized to Edge.

Example:

```text
ANT 1 -- Check-in / 2.0s --> ANT 2
ANT 2 -- Check-out / 2.0s --> ANT 1
```

Each transition stores only the source antenna, destination antenna, business event type, and measured Duration. Edge creates a Parking Transaction only when the same Vehicle RFID is detected on the configured source antenna and then on the configured destination antenna within that transition Duration. There is no lane-level fixed timing window. Antenna records contain physical antenna identity only; RSSI remains observation/measurement data and is not an operational validity threshold.

The transition Event Type is authoritative for Check-in/Check-out. Lane direction is not stored. Raw Detection Events remain Edge-only.


## Measurement sessions

A Measurement Session is a test plan, not a single-tag Reader command. It supports:

- one or many RFID target lines;
- one or many Controllers;
- one or many Readers per Controller;
- an explicit antenna subset per Reader;
- temporary Reader Power and Read Interval per Reader;
- a shared revision for every released configuration.

RFID Cards use `is_measurement_card` only as an eligibility switch. Current Usage State is derived instead of stored: a card is `Measurement / Test` only while it belongs to a released/running Measurement Session, then returns to `Used` or `Available` when the session ends or the line is removed.

Cloud sends each Edge the full target list and only the Reader lines owned by that Edge. Edge sends each Controller only its own Reader subset. Controller drops non-target TIDs before durable Measurement persistence. Detection Timeline presents the chronological RFID path using First Detected, RFID Tag, Controller, Reader, Antenna, Reads, Last Detected and Duration. Reader Power, Read Interval and RSSI are not timeline dimensions; Reader settings are shown on compact Reader cards, and RSSI-over-time visualization is intentionally removed.
## Sync API deployment boundary

- Cloud source APIs are owned by `nsp_master_gatekeeper`; their availability is determined by installing the Master module.
- Edge outbound transport is owned by `nsp_sync`; installing `nsp_business_gatekeeper` installs it automatically.
- Cloud source APIs do not depend on the optional `nsp.deployment_role` parameter. Authentication still requires a valid Core API Application, an allowed route, and a declared active Edge Server code.
- `nsp_sync` must not be installed on the Cloud database for normal fresh-install deployment.

