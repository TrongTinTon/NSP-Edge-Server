# Detection Match Consumption — 19.0.10.44.0

## Problem

The 19.0.10.43.0 matcher correctly stopped collapsing the raw RFID timeline before Lane Configuration matching. However, a repeated first-point read that had been replaced by a newer anchor could survive a successful crossing in the Detection working buffer.

Example:

- Lane Configuration: `A -> B`, Max Duration `3s`
- Raw reads: `A@0, A@1, B@2`
- Winning path: `A@1 -> B@2`

The old cleanup range started at the winning path's `A@1`, so `A@0` could remain pending until stale expiry.

## Resolution

The pure matcher now returns two views of every successful traversal:

- `path`: only observations selected for the Antenna Sequence match.
- `consumed_events`: all raw observations claimed by that traversal, including repeated current-point reads that were replaced or ignored by the matcher.

`nsp.parking.detection.event` carries those claimed events as `consume_events` and passes them to `_consume_detection_group()` only after the movement is committed, ignored, or suppressed as a duplicate crossing.

Cleanup also includes the claimed physical `event_uid` values so all sibling Lane fan-out copies of those repeated reads are removed.

## Boundaries preserved

- No raw timeline collapse before Lane Configuration matching.
- No global duplicate-TID time window.
- Antenna Sequence and Max Duration remain the only sequence timing rules.
- Abandoned/invalidated candidates are not claimed by a later successful match; they remain pending until they match independently or expire.
- Parking Log creation still uses only the selected sequence events plus supporting User events; ignored repeated reads are cleanup metadata only.
- If Parking business processing fails after a sequence match, every claimed Vehicle read is marked `processing_error` so an ignored repeat cannot remain pending and be reused as a new traversal.
