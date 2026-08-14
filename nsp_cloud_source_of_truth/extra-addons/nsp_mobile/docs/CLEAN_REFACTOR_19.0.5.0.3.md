# NSP Mobile 19.0.5.0.3 — Clean refactor

## Vehicle master contract

Mobile now uses the current NSP master models directly:

- `nsp.vehicle.type`
- `nsp.reference.brand`
- `nsp.reference.model`
- `nsp.vehicle.color`
- `nsp.vehicle`
- `nsp.parking.log`

Legacy `nsp.vehicle.brand`, `nsp.vehicle.model`, vehicle approval state, engine/chassis/description fields and direct vehicle RFID fields are not part of the current contract and are no longer referenced.

`GET /v1/mobile/vehicles/config` returns `vehicle_types`, `brands`, `models`, and `colors`. `POST /v1/mobile/vehicles/register` accepts master IDs (`vehicle_type_id`, `brand_id`, `model_id`, `color_id`).

## Notification Parking Event badge

Notification responses keep the canonical `parking_event_type` and add UI-ready badge metadata:

- Check-in: `{\"key\": \"check_in\", \"label\": \"Check-in\", \"tone\": \"success\"}`
- Check-out: `{\"key\": \"check_out\", \"label\": \"Check-out\", \"tone\": \"info\"}`

The badge is derived only from `nsp.notification.parking_event_type`; it is never inferred from title/message text.
