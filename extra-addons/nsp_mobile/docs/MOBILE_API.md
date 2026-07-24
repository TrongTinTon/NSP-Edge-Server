# NSP Mobile API v1

## Security model

- Mobile authentication is user-bound and does not use the Service Application Client Secret.
- Login/refresh/logout are Core API authentication routes:
  - `POST /v1/mobile/auth/login`
  - `POST /v1/mobile/auth/refresh`
  - `POST /v1/mobile/auth/logout`
- A successful login issues a rotating Core API Mobile Token bound to:
  - `nsp.user`
  - `nsp.mobile.session`
  - `nsp.mobile.device`
- Business endpoints are normal `core.api.endpoint` routes owned by the system `NSP Mobile` Core API Application.
- Business requests never accept `user_id` or `user_code` to select the current user. The authenticated user always comes from the Mobile Token.

## Authentication request

```json
{
  "login": "user-technical-login",
  "password": "********",
  "device": {
    "device_uid": "device-generated-stable-id",
    "platform": "android",
    "device_name": "Pixel",
    "app_version": "1.0.0",
    "push_provider": "none",
    "push_enabled": false
  }
}
```

## Business routes

- `GET /v1/mobile/me`
- `PATCH /v1/mobile/me/update`
- `POST /v1/mobile/auth/change-password`
- `POST /v1/mobile/devices/register`
- `POST /v1/mobile/devices/heartbeat`
- `POST /v1/mobile/devices/unregister`
- `GET /v1/mobile/vehicles`
- `GET /v1/mobile/vehicle?vehicle_id=...`
- `GET /v1/mobile/parking-history?vehicle_id=...`
- `GET /v1/mobile/friends/search?q=...`
- `GET /v1/mobile/friends`
- `GET /v1/mobile/friends/requests`
- `POST /v1/mobile/friends/request`
- `POST /v1/mobile/friends/accept`
- `POST /v1/mobile/friends/cancel`
- `GET /v1/mobile/borrows`
- `POST /v1/mobile/borrows/create`
- `POST /v1/mobile/borrows/end`
- `POST /v1/mobile/borrows/cancel`
- `GET /v1/mobile/notifications`
- `GET /v1/mobile/notifications/unread-count`
- `POST /v1/mobile/notifications/read`
- `POST /v1/mobile/notifications/read-all`
- `GET /v1/mobile/realtime/events?after_id=...`

## Notification delivery

`nsp.notification` remains the source of truth. `nsp.notification.delivery` is transport state.

- Realtime provider is implemented as device-bound event delivery acknowledged by `/mobile/realtime/events`.
- Push providers are provider-agnostic (`none`, `fcm`, `apns`, `custom`) at the device layer.
- FCM/APNs network adapters are intentionally not hard-coded into this base module. A provider module can extend `nsp.notification.delivery.service` without changing Parking or Mobile business logic.
