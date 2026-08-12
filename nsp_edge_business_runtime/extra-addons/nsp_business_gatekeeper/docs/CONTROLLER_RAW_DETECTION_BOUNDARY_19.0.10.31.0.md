# Controller raw-detection boundary fix — 19.0.10.31.0

## Problem

A valid `parking/detections/push` batch could return HTTP 500 when either:

- Reader observation/telemetry update failed; or
- synchronous Parking sequence/business processing failed after candidate detections were already accepted.

This coupled Controller acquisition transport to Edge telemetry/business execution and caused the Controller outbox to retry physical reads for failures outside Controller ownership.

## Rule

Controller sends what it sees. Edge interprets the detections.

## Fix

- Reader observation update is best-effort and cannot fail the raw detection transport.
- Candidate detection persistence remains transactional and idempotent.
- Sequence/business processing is isolated per contextual Lane in a savepoint.
- A business-processing exception leaves the accepted candidate detections pending on Edge for the periodic worker and does not force Controller transport retry.
- Fatal failures before candidate persistence still return HTTP 500 so the Controller outbox can retry safely.

## Deployment note

This version still requires a normal Odoo module upgrade (`-u nsp_business_gatekeeper`) because 19.0.10.30.0 introduced `nsp.reader.observation` and ownership/schema changes. The best-effort observation path protects raw detection traffic, but it is not a substitute for applying the database migration.
