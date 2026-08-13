# Detection Sequence Matcher refactor — 19.0.10.43.0

## Problem

`nsp.parking.detection.event._build_vehicle_sequence_matches()` previously
collapsed consecutive detections with the same Reader/Port before applying the
Lane Configuration. The collapse kept the first observation. A later repeated
read that was the correct anchor could therefore be discarded before
`duration_from_previous` was evaluated.

Example with `A -> B <= 3s`:

- A at 00s
- A at 10s
- B at 12s

The old matcher kept A@00 and rejected the 12-second gap. The valid A@10 -> B@12
transition was lost.

## Refactor

- Raw Detection Log order is kept intact.
- Lane Configuration is loaded first: ordered Reader/Port keys plus each
  transition Max Duration.
- Matching is delegated to the pure `services/antenna_sequence_matcher.py`
  stateful matcher.
- A repeated read of the current sequence point refreshes the anchor only when
  the transition from the previous matched point remains valid.
- A repeated first point always refreshes the start anchor.
- Out-of-order Lane points and next points outside Max Duration preserve the
  existing strict sequence semantics and invalidate the candidate traversal.
- No global duplicate-TID window or tolerance is introduced.

## Runtime boundaries kept unchanged

- `event_uid` remains transport idempotency.
- `Lane Configuration -> Antenna Sequence -> Max Duration` remains the source of
  truth for movement matching.
- Existing duplicate-crossing suppression and Parking Log processing are
  unchanged.
- Successful Detection rows remain short-lived working-buffer records.

## Regression coverage

Targeted tests cover:

1. latest repeated first-point anchor;
2. latest valid repeated internal anchor;
3. rejection of an internal replacement that violates the prior Max Duration;
4. strict out-of-order rejection;
5. restart after a timed-out first transition;
6. no reuse of a partial path after a later transition timed out;
7. multiple independent crossings.

A randomized equivalence check over 40,000 timelines without consecutive
repeated points produced the same matches as the previous strict matcher.


## Superseded overlap behavior — 19.0.10.57.0

The original 19.0.10.43.0 strict out-of-order invalidation rule is superseded for raw RFID overlap. Already-passed Reader/Port reads and premature future Reader/Port reads no longer rewind or invalidate active ordered progress. Only the immediate next configured point advances the sequence, and selected transition gaps still obey Max Duration. See `DETECTION_TO_PARKING_LOG_RFID_OVERLAP_19.0.10.57.0.md`.
