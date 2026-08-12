# Reader Status lock isolation — 19.0.10.32.0

## Problem

`/v1/devices/report`, `/v1/parking/detections/push`, and Lane Calibration raw-event ingestion could all update the same `nsp.reader.observation` row. In Odoo/PostgreSQL, a successful row update holds its row lock until the outer HTTP transaction commits. A long Parking/Calibration request could therefore block `/devices/report` long enough for the Controller HttpClient timeout (15s by default).

## Fix

- `/v1/devices/report` is now the single writer of `nsp.reader.observation`.
- Parking raw detections no longer mutate Reader Observation.
- Lane Calibration raw events no longer mutate Reader Observation.
- Raw acquisition remains independent from telemetry/status reporting.
- Parking/Calibration business processing remains Edge-owned.

This implements the boundary: Controller reports raw observations; Reader status reporting is telemetry; Parking and Calibration business logic remain independent Edge/Cloud pipelines.
