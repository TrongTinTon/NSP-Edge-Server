# Detection Log raw-receipt contract — 19.0.10.36.0

## Fixed defect

`/v1/parking/detections/push` previously returned HTTP 200 even when every physical detection was silently dropped before `nsp.parking.detection.event` creation. Two common paths were:

- no active RFID Runtime Assignment for the TID;
- Reader Serial / Port could not resolve to an operational contextual Lane.

This violated the Controller/Edge boundary because a successful transport ACK did not prove Edge had durably recorded what the Controller observed.

## New contract

For every syntactically valid detection in an authenticated batch, Edge persists technical evidence before business resolution.

- resolved identity + topology: create normal Lane candidate Detection Log (`pending` / later `processed`);
- unresolved RFID assignment: create terminal Detection Log with `rfid_assignment_not_found`;
- unknown Reader: `device_not_found`;
- Reader/Port absent from runtime timeline: `no_reader_port_timeline`;
- Controller not referenced by candidate Lane: `controller_not_in_scope`;
- Reader/Port spans multiple Parking Layouts: `ambiguous_reader_port_layout`.

Only malformed transport payloads are rejected without persistence.

## Schema boundary

Detection Log now stores raw source identity independently from resolved business context:

- `controller_id`
- `serial_number`
- `port_no`
- `tid`
- `detected_at`
- `rssi_dbm`

`layout_lane_id`, `lane_id`, and `reader_id` are nullable because they are Edge resolution outputs, not Controller transport requirements.

Unresolved records are unique by `event_uid`; resolved Lane candidates remain unique by `(event_uid, layout_lane_id)`.

## API acknowledgement

Successful response now reports:

- `received`
- `persisted`
- `candidate_records_created`
- `error_records_created`
- `duplicates`

HTTP 200 therefore means the valid transport observations are represented durably on Edge, not merely that the route executed.
