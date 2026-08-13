# NSP Business Gatekeeper 19.0.10.56.0

## Live Monitor

The Edge Live Monitor frontend is aligned with the Cloud Master Live Monitor UI.
The module namespace and asset URLs remain Edge-local. The Edge Parking Log live
payload now also exposes `decision` and an optional User avatar URL using whichever
supported avatar/image field exists on `nsp.user`.

## Detection -> Parking Log reliability

Detection acquisition remains durable and independent from Parking business
processing. A deterministic Odoo `ValidationError` while resolving a matched
traversal is terminal and the claimed detections are marked `processing_error`.
Unexpected runtime/database exceptions are retryable: the processing savepoint is
rolled back and the Detection rows remain pending (`error_code IS NULL`) so a later
processor run can retry the same traversal.

The ingest acknowledgement includes `parking_logs_created` and
`processing_deferred_lanes` for operational diagnosis. The scheduled Detection
processor isolates failures per Lane so one Lane cannot block processing of others.
