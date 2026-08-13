# Lane Calibration runtime tuning — 19.0.10.43.0

## Edge lifecycle

The Edge UI mirrors five user-facing states: Draft / Released / Running / Completed / Stopped. The obsolete `applied` state is migrated to `completed`; `failed` and `cancelled` remain protocol outcomes presented as Stopped.

## Reader runtime override

While a released revision is Released or Running, authorized Edge users may tune only:

- Power
- Read Interval
- TID Start
- TID Length

Topology, Calibration Tag, lifecycle, Reader identity and Ports remain Cloud-owned. Edge model mutations are blocked at the ORM layer even if a caller bypasses the read-only UI. The only user-write path is the four-field Reader runtime override action. Overrides belong only to the current revision and are reset when a new revision is projected. Reset to Cloud restores the released base values.

Every stored Calibration detection snapshots the effective Reader settings used at acquisition time. Edge sends those snapshots to Cloud as technical evidence; Controller still sends raw TID/Reader/Port observations and does not own these business/runtime settings.

## Retry safety

A durable Controller outbox may contain events from a previous Calibration after Edge has already replaced or closed that session. Such obsolete batches are acknowledged as ignored instead of returning HTTP 500 forever.

## Refactor / performance

- Same-revision pulls use an exact projection comparison and update lifecycle only when topology/base configuration is unchanged; runtime overrides survive normal polling.
- Edge no longer `search([])` loads all Servers, Controllers and Readers for a Lane Calibration snapshot; lookups are scoped to the codes/serials in that snapshot.
- Topology rebuild creates Server, Controller and Reader nodes in three ORM batches rather than one `create()` per node.
- Calibration TID normalization no longer depends on RFID runtime assignment.
- Reader-node lookup is cached by Serial once per event batch instead of filtering the Device Tree for every detection.
- Detection Timeline queries only the requested revision set.
- Full-snapshot reconciliation queries only active stale sessions plus terminal sessions with no events; it no longer loads every historical calibration on each pull.
- A missing previously Running projection closes as Completed (normal Cloud Stop); a missing Released projection closes as Stopped/Cancelled (Revise/Cancel before acquisition).

Controller-facing APIs remain:

- `controller/lane-calibrations/pull`
- `controller/lane-calibrations/events`
- `controller/lane-calibrations/status`

Edge-to-Cloud APIs remain:

- `edge/lane-calibrations/snapshot`
- `edge/lane-calibrations/events`
- `edge/lane-calibrations/status`
