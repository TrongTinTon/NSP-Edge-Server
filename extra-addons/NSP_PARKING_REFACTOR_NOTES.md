# NSP Parking Layout & Lane Calibration Refactor

## Modules

- `nsp_master_gatekeeper` → `19.0.7.0.0`
- `nsp_business_gatekeeper` → `19.0.7.0.0`
- `nsp_sync` → `19.0.5.0.0`

## Breaking changes

- Removed legacy Directed Antenna Transition model and payload.
- Removed Lane `direction`.
- Parking Layout now publishes `antenna_timeline`, `event_sequences`, and `timing_tolerance` only.
- Accepted Lane Calibration imports Timeline only and clears existing Event Sequences.
- Sync routes were renamed to the `/edge/...` resource contract.
- Controller application was intentionally not changed.

## Runtime protections

- Contiguous Timeline and Sequence order.
- Positive calibrated durations after the first point.
- Sequence adjacency validation in both directions.
- Opposite Timeline orientation is required when both Check-in and Check-out are configured.
- Exclusive Antenna ownership across operational Lanes.
- Snapshot revision guard and full stale reconciliation.
- Detection and transaction idempotency.

## Publish and apply safety

- Operational publishes only a fully validated Cloud draft.
- Maintenance/Blocked changes only the latest immutable published snapshot.
- Edge replaces Timeline/Event Sequence child rows atomically for every full snapshot, preventing transient unique-key conflicts during route reversal or Antenna swaps.
- Existing legacy published JSON is rejected and must be revised and republished.
- Reader/Antenna device configuration is projected once in `controllers[].devices`; Lane payloads reference the runtime topology without embedding a duplicate device tree.
