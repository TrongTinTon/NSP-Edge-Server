# NSP Mobile API v1

## Identity and authorization

- Mobile login uses the active internal Odoo User (`res.users`) login and password.
- The Odoo User must be linked to one active `nsp.user` through `odoo_user_id`.
- There is no separate Mobile Access flag, Mobile Login or Mobile Password.
- Odoo User activation, Groups, ACLs and Record Rules determine access to Mobile business operations.
- Odoo multi-factor authentication is never bypassed; accounts requiring an MFA step are rejected until the Mobile MFA flow is implemented.
- Login/refresh/logout routes are:
  - `POST /v1/mobile/auth/login`
  - `POST /v1/mobile/auth/refresh`
  - `POST /v1/mobile/auth/logout`
- A successful login issues a rotating Core API Mobile Token bound to:
  - `res.users` as the authenticated subject;
  - `nsp.mobile.session`;
  - `nsp.mobile.device`.
- Business requests never accept `user_id` or `user_code` to choose the current user. The linked `nsp.user` is resolved from the authenticated Odoo User.

## Authentication request

```json
{
  "login": "odoo.user@example.com",
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

## Password change

`POST /v1/mobile/auth/change-password` changes the standard Odoo User password. Existing Mobile sessions are revoked and the client must sign in again.

## Notification delivery

`nsp.notification` remains the source of truth. `nsp.notification.delivery` is transport state.
