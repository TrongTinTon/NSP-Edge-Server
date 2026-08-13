# Parking Log Clean Code — 19.0.10.55.0

## Scope

This revision keeps `nsp.parking.log` as immutable Edge business history while simplifying its UI and reducing duplicated/over-broad authorization work.

## Business UI

Parking Logs expose only the operational columns:

`Event Time | Parking Area | Lane | Vehicle | User | Event | Decision | Reason`

Technical evidence remains persisted in the model (`log_uid`, layout revision/configuration, Vehicle/User TID and Borrow reference) but is no longer exposed in the normal list/form/search UI.

## Check-out authorization

Detection correlation selects exactly one supporting User read. Parking business validates only that selected User:

1. Active Vehicle owner -> authorized.
2. Otherwise query one active Borrow for `(vehicle, selected user, event_time)`.
3. No matching Borrow -> `unauthorized_vehicle_user`.

The previous all-borrower authorization map was removed.

## Decision reason

Runtime now carries one `reason_code` instead of accumulating a list and selecting the first element. The immutable log model constrains the outcome contract:

- Allowed -> no Reason.
- Denied -> Reason required.

## Live Monitor

The main grid represents Vehicle entries. `Check-in + Allowed` remains an entry. `Check-out + Allowed` now emits `display_kind=clear`: it clears an outstanding alert for that Vehicle but does not create another entry card. Denied events remain alerts.
