# Lane Calibration simplification — 19.0.13.39.0

## Workflow

User-facing lifecycle is reduced to five statuses:

- Draft
- Released
- Running
- Completed
- Stopped (`failed` / `cancelled` protocol outcomes)

Primary actions:

- Draft: Save / Release
- Released: Revise
- Running: Stop
- Completed: Revise

`Stop` completes a normal running calibration. `Stopped` is reserved for abnormal or cancelled execution outcomes.

`Revise` is Cloud-owned. It increments the revision, returns the same Lane Calibration to Draft, keeps historical detections/results under their original revision, preserves the current Device Tree as the starting configuration, and does not publish Draft changes. Release publishes the new revision.

## Device topology

- Server is always a Tree root.
- Controller always requires a Server parent.
- Reader always requires a Controller parent.
- Draft may be incomplete only by missing child levels; orphan nodes are never valid.
- Release validates that every Server has a Controller, every Controller has a Reader, and every Reader has at least one Port.

## Runtime evidence

Cloud accepts the actual Reader runtime settings captured by Edge instead of requiring equality with the released base settings. Detection events persist:

- Power
- Read Interval
- TID Start
- TID Length

Lane Setup uses the selected event snapshots and rejects a selected path that mixes multiple runtime profiles for one Reader.

## Refactor / performance

- Removed obsolete Lane Calibration `applied` state; historical rows migrate to `completed`.
- Removed unused live-form/popup compatibility actions, obsolete Apply/Measure-Again helpers, and the dead Lane Direction Setup alias.
- Removed unused `applied_at` and redundant Session UI counters.
- Release completeness uses parent-id sets instead of nested recordset filters.
- Detection Timeline queries only requested revisions instead of scanning all historical revisions for each Session.
