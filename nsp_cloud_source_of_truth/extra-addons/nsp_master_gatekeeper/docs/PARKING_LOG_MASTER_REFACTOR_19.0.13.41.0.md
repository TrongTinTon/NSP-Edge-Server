# Parking Log Master Refactor — 19.0.13.41.0

## Contract

Cloud `nsp.parking.log` is an immutable mirror of the final Parking business event produced by Edge.
It stores stable historical context (`Parking Area + Lane Master + Layout Revision`) and no longer
uses mutable `Lane Configuration` as the semantic identity of a historical Parking Log.

## Changes

- `Check-in + Allowed` -> Live Monitor `entry`.
- `Check-out + Allowed` -> Live Monitor `clear` (no new entry card).
- `Denied` -> Live Monitor `alert`.
- `Allowed` must have no Reason.
- `Denied` must have a Reason; Cloud rejects missing reasons instead of filling `unknown`.
- Duplicate `log_uid` is compared before current published-route/revision validation, so an identical
  retry stays `duplicate` after a later Parking Layout revision is published.
- New Parking Logs reference stable `lane_id`; `layout_lane_id` is legacy-only, not populated by sync,
  and uses `ondelete=set null` to avoid history blocking Lane Configuration lifecycle.
- Parking Logs UI contains only Event Time, Parking Area, Lane, Vehicle, User, Event, Decision, Reason.
