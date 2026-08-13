# Nearest User RFID — 19.0.10.46.0

> **Historical behavior:** This document is superseded by `CHECKOUT_SINGLE_VEHICLE_USER_19.0.10.47.0.md`. From 19.0.10.47.0 onward, any present User detection resolves Check-out immediately; only missing User waits until the Lane deadline.

## Rule

Check-out no longer treats more than one User RFID identity in the Lane window as an ambiguity error.
For a matched Vehicle crossing, Edge selects the unused User RFID detection with the smallest absolute time distance to the Vehicle sequence completion (`match.end_at`).

- No candidate by the Lane deadline: `missing_user_tid`.
- Nearest candidate authorized for the Vehicle: allowed.
- Nearest candidate unauthorized at the Lane deadline: `unauthorized_vehicle_user`.
- Other User candidates are not attached to this Parking Log and are not consumed by this crossing.

Before the deadline, an unauthorized/missing nearest candidate keeps the existing wait behavior so a later detection can become the nearer candidate. An already-authorized nearest candidate can resolve immediately, preserving the previous fast path.

The `multiple_user_tags` runtime decision path, Live Monitor mapping, and Parking Log search filter are removed. The old selection value remains hidden as a legacy compatibility value only, so immutable historical Parking Logs created before 19.0.10.46.0 stay readable after upgrade.
