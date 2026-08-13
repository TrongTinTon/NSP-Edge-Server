# Parking Log push diagnostics — 19.0.10.59.0

`edge/parking-logs` now distinguishes an empty Edge history from a fully-consumed cursor:

- `No Parking Logs exist on Edge...` means `nsp.parking.log` has no rows.
- `No new Parking Logs after cursor X (Edge max log id=Y).` means the cursor has consumed all local IDs.
- An impossible cursor above the append-only table maximum is reset locally to 0 for idempotent replay.
