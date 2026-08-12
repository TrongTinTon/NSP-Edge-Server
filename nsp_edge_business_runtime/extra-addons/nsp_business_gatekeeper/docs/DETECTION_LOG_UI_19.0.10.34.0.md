# Detection Log UI — 19.0.10.34.0

## Purpose

Expose existing `nsp.parking.detection.event` records as **Detection Logs** without creating another persistence table or moving technical evidence into long-lived `nsp.parking.log`.

## Data boundary

- **Detection Logs**: short-lived technical RFID evidence on Edge.
- **Parking Logs**: long-lived immutable business history (Check-in / Check-out + decision).
- **Parking Layout / Lane Configuration**: runtime configuration/context.

## UI

A new top-level **Detection Logs** menu is available to Parking Operator / Parking IT users.

List view shows the operational fields needed for diagnosis: detected time, Lane, Reader, Port, TID, RSSI, resolved Vehicle/User, processing state, linked Parking Log and error state.

Form view provides three clear sections:

1. Detection identity/state
2. Physical observation
3. Edge resolution and Parking Log link

Parking Log form continues to expose `source_detection_ids`, now with state and resolved identity columns for direct audit tracing.

## Storage

No new model/table/column was introduced. The change is presentation-only apart from the model description label. Existing short-retention policy for Detection Events remains unchanged.
