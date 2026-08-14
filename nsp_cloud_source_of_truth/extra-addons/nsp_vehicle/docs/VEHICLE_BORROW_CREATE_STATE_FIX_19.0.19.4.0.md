# Vehicle Borrow create-state fix — 19.0.19.4.0

## Symptom
A newly created Vehicle Access/Borrow could appear as `Returned` immediately.

## Root cause
`nsp.vehicle.borrow.create()` forced `Active` only for non-admin self-service users. HR/IT/System users were allowed to persist a `state` value received from the client. The same model is also used by Cloud -> Edge snapshot application, so the old implementation left admin creates permissive to preserve synced historical states.

## Fix
Creation now distinguishes interactive/master-data creation from snapshot application:

- normal Cloud/UI create always starts with `state = active` and `returned_at = False` for every role, including HR/IT/System;
- only `vehicle_borrow_sync=True` may create records directly as `returned` or `cancelled` when applying the authoritative Cloud snapshot on Edge;
- `active` and `cancelled` records always clear `returned_at` during create;
- the existing explicit `End` action remains the interactive transition to `returned`.

This preserves Cloud as Master Data source of truth and keeps Edge as a read-only runtime projection.
