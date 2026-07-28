# NSP Gatekeeper fresh-install architecture

## Modules

- Cloud: `nsp_master_gatekeeper` owns Edge Servers, master Controllers/Readers/Antennas, Branches, Parking Configuration, Device Whitelist, Measurement management and Cloud history mirrors.
- Edge: `nsp_business_gatekeeper` owns the local Controller/Reader/Antenna runtime cache, parking detection/business processing, Parking Transactions, Measurement runtime, Live Monitor and Live Measurement. **Edge has no `nsp.edge.server` model.**
- `nsp_business_gatekeeper` depends on `nsp_sync`; installing Business Gatekeeper installs Sync automatically.
- `nsp_sync` is the shared transport layer. Edge uses Cloud Connection, jobs, durable retry/ACK and snapshot application. Cloud uses its source/receive endpoints.
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
- `nsp_master_gatekeeper` also uses `nsp_sync` for Cloud-side authoritative snapshot/business-event endpoints.
- `nsp_mobile` depends on `nsp_master_gatekeeper`, never the reverse. Installing Master must not auto-install Mobile (`auto_install=False`).
- Cloud and Edge Gatekeeper modules are deployment-role alternatives and are not intended to be installed together in the same database.

## Odoo model extension rule

Configuration revision hooks extend existing Odoo models using `models.Model` only. Plain Python mixins must not be added as additional bases to `_inherit` extension classes because Odoo rebuilds model bases dynamically during registry setup. Shared revision behavior is implemented with module-level helper functions instead.

