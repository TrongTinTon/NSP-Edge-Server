# Gatekeeper Check-out — Single Vehicle + Single User — 19.0.10.47.0

## Operating rule

A Lane read zone is occupied by one Vehicle and one accompanying User at a time. Other Vehicles and Users must remain outside the RFID read zone until the current crossing completes.

## Runtime decision

1. Match the Vehicle against the published Lane Configuration Antenna Sequence.
2. For Check-out, search known User RFID detections inside the Lane correlation window.
3. If at least one User detection exists, select the detection whose timestamp is nearest to `match["end_at"]`.
4. Resolve Owner/Borrower authorization immediately:
   - authorized → `allowed`;
   - unauthorized → `denied / unauthorized_vehicle_user`.
5. If no User detection exists yet, keep the Vehicle pending until `match["end_at"] + lane.max_sequence_window()`.
6. If the window closes with no User detection, create `denied / missing_user_tid`.

RSSI is not used to select the User. `multiple_user_tags` is not a runtime decision path.

## Repeated User reads

The nearest detection is the only User detection supplied to Parking business history. After a crossing resolves, all repeated detections of that same selected User within the crossing window are consumed from the Detection working buffer so they cannot leak into the next Vehicle crossing. Detections belonging to a different User are not deleted by this cleanup.
