# Detection → Parking Log RFID overlap fix — 19.0.10.57.0

## Confirmed code-level blocking point

Parking Log creation is reachable only after `_build_vehicle_sequence_matches()` returns a full Vehicle Antenna Sequence. The previous pure matcher treated any configured Lane point other than the current or immediate next point as an out-of-order failure/reset.

That rule is too strict for raw RFID. Reader zones overlap, so a valid physical traversal such as `A -> B -> C` can produce raw reads like:

- `A, B, A, C` (previous antenna keeps seeing the tag), or
- `A, C, B, C` (future antenna sees the tag briefly before the selected transition).

The old matcher could therefore keep valid Detection Logs pending until expiry and never call `ParkingLogBusiness`.

## New matcher contract

- Only the immediate next configured Antenna advances sequence state.
- Selected transition gaps still require `0 <= gap <= Max Duration`.
- Repeated current-point reads may refresh the anchor when valid.
- Already-passed Antenna reads are treated as RF overlap and do not rewind progress.
- Premature future Antenna reads are treated as RF overlap and do not invalidate progress.
- Overlap reads are claimed and removed when the traversal succeeds.
- When the active next-transition deadline expires, the partial traversal is dropped.
- A new first-point read after expiry may immediately begin a new crossing.
- Reverse traffic that never produces the configured ordered progression still does not match.

## Business boundary unchanged

Once a full Vehicle sequence matches:

1. determine Check-in / Check-out from latest Allowed Parking Log;
2. Check-in creates the Parking Log immediately;
3. Check-out correlates one nearest User detection and validates Owner/Borrow;
4. Parking Log is appended immutably;
5. successful Detection working-buffer rows are consumed.
