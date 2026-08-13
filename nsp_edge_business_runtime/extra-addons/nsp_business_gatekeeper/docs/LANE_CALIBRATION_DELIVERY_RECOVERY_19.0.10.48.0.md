# Lane Calibration delivery recovery — 19.0.10.48.0

## Root cause

The Detection/Check-out integration branch did not include the Lane Calibration delivery fix from the parallel runtime branch. Two defects reappeared:

1. `fields.Datetime.to_string()` was called with the normalized string returned by `_measurement_datetime()`, which can raise `AttributeError: 'str' object has no attribute 'strftime'` during idempotent replay or duplicate-in-batch comparison.
2. Edge treated Cloud `ignored` as local `synced`, which made transport look successful even though Cloud intentionally did not persist the event.

## Correct behavior

- Controller cares only about HTTP transport acknowledgement.
- Edge owns per-event validation and persists valid `nsp.measurement.event` records.
- Edge forwards valid events through `edge/lane-calibrations/events`.
- Cloud `processed` / `duplicate` are terminal successful delivery (`synced`).
- Cloud `ignored` is terminal non-persisted delivery (`skipped`) and is visible in Sync Records.
- Cloud `rejected` / transport failure remains `failed` and is retried.
- Datetime equality normalizes with `to_datetime()` or compares already-normalized strings directly; it never calls `to_string()` on a string.
