# NSP Mobile API v1

## 1. Identity contract

- Mobile signs in with the active internal Odoo User (`res.users`) login and password.
- Each internal Odoo User resolves to exactly one active `nsp.user` business profile.
- The access token subject remains `res.users`; business authorization and data ownership use the linked `nsp.user`.
- A successful login binds the token to one `nsp.mobile.session` and one `nsp.mobile.device`.
- Business APIs never accept `user_id`, `odoo_user_id` or `user_code` to choose the current identity.
- Standard Odoo activation, Groups, ACLs and Record Rules remain authoritative.
- Accounts requiring an Odoo MFA step are rejected until a Mobile MFA flow is implemented; MFA is never bypassed.

## 2. Route convention

Public paths use:

```text
/v1/mobile/{resource}
```

Rules:

- nouns identify resources;
- plural nouns identify collections;
- the current authenticated profile and device use singular routes;
- HTTP methods distinguish read, update, create and unregister operations where the Core API route model permits it;
- command suffixes are retained only for state transitions such as `accept`, `cancel`, `end`, `read` and `heartbeat`;
- legacy action-style routes are not exposed.

## 3. Authentication routes

| Method | Route | Purpose |
|---|---|---|
| POST | `/v1/mobile/auth/login` | Authenticate the Odoo User, register/update the login device, open a Mobile session and issue tokens. |
| POST | `/v1/mobile/auth/refresh` | Rotate the refresh token and issue a new access token for the same Odoo User, session and device. |
| POST | `/v1/mobile/auth/logout` | Revoke the current Mobile session and all tokens bound to that session. |
| PATCH | `/v1/mobile/auth/password` | Change the standard Odoo User password and require login again. |

### Login request

```json
{
  "login": "odoo.user@example.com",
  "password": "********",
  "device": {
    "device_uid": "device-generated-stable-id",
    "platform": "android",
    "device_name": "Pixel",
    "app_version": "1.0.0",
    "push_provider": "fcm",
    "push_token": "provider-device-token",
    "push_enabled": true
  }
}
```

FCM/APNS server credentials are configured only on Cloud in `nsp_notification`. Mobile sends only the provider name and device token.

## 4. Business routes

| Method | Route | Purpose |
|---|---|---|
| GET | `/v1/mobile/profile` | Read the current NSP profile, Odoo login identity, current device and session. |
| PATCH | `/v1/mobile/profile` | Update the current NSP profile fields: `name`, `email`, `phone`. |
| GET | `/v1/mobile/device` | Read the current token-bound device. |
| PATCH | `/v1/mobile/device` | Update current device metadata and push registration. The bound `device_uid` cannot be changed. |
| DELETE | `/v1/mobile/device` | Unregister the current device and revoke its active Mobile session. |
| POST | `/v1/mobile/device/heartbeat` | Update device/session last-seen and synchronization timestamps. |
| GET | `/v1/mobile/vehicles` | List active vehicles owned by the current NSP User. |
| GET | `/v1/mobile/vehicles/detail?vehicle_id=...` | Read one owned vehicle, latest allowed parking transaction and active borrow state. |
| GET | `/v1/mobile/parking/transactions?vehicle_id=...` | Read paginated Parking Transactions for vehicles owned by the current NSP User. |
| GET | `/v1/mobile/friends/search?q=...` | Search active NSP Users for friend discovery. |
| GET | `/v1/mobile/friends` | List accepted friendships. |
| GET | `/v1/mobile/friend-requests` | List pending sent and received friend requests. |
| POST | `/v1/mobile/friend-requests` | Create a friend request using `friend_id`. |
| POST | `/v1/mobile/friend-requests/accept` | Accept a received pending request using `friendship_id`. |
| POST | `/v1/mobile/friend-requests/cancel` | Cancel/decline/remove a request or friendship using `friendship_id`. |
| GET | `/v1/mobile/vehicle-borrows` | List lending records where the current NSP User is owner or borrower. |
| POST | `/v1/mobile/vehicle-borrows` | Create a lending period for an owned vehicle and an accepted friend. |
| POST | `/v1/mobile/vehicle-borrows/end` | End an active lending period using `borrow_id`. |
| POST | `/v1/mobile/vehicle-borrows/cancel` | Cancel an owned lending record using `borrow_id`. |
| GET | `/v1/mobile/notifications` | Read paginated notification inbox data. |
| GET | `/v1/mobile/notifications/unread-count` | Read the current unread count. |
| POST | `/v1/mobile/notifications/read` | Mark one owned notification as read. |
| POST | `/v1/mobile/notifications/read-all` | Mark all current-user notifications as read. |
| GET | `/v1/mobile/notifications/events?after_id=...` | Poll realtime notification deliveries using a monotonic notification cursor. |

## 5. Route replacement map

| Removed route | Replacement |
|---|---|
| `GET /v1/mobile/me` | `GET /v1/mobile/profile` |
| `PATCH /v1/mobile/me/update` | `PATCH /v1/mobile/profile` |
| `POST /v1/mobile/devices/register` | `PATCH /v1/mobile/device` |
| `POST /v1/mobile/devices/heartbeat` | `POST /v1/mobile/device/heartbeat` |
| `POST /v1/mobile/devices/unregister` | `DELETE /v1/mobile/device` |
| `GET /v1/mobile/vehicle` | `GET /v1/mobile/vehicles/detail` |
| `GET /v1/mobile/parking-history` | `GET /v1/mobile/parking/transactions` |
| `GET /v1/mobile/friends/requests` | `GET /v1/mobile/friend-requests` |
| `POST /v1/mobile/friends/request` | `POST /v1/mobile/friend-requests` |
| `POST /v1/mobile/friends/accept` | `POST /v1/mobile/friend-requests/accept` |
| `POST /v1/mobile/friends/cancel` | `POST /v1/mobile/friend-requests/cancel` |
| `GET /v1/mobile/borrows` | `GET /v1/mobile/vehicle-borrows` |
| `POST /v1/mobile/borrows/create` | `POST /v1/mobile/vehicle-borrows` |
| `POST /v1/mobile/borrows/end` | `POST /v1/mobile/vehicle-borrows/end` |
| `POST /v1/mobile/borrows/cancel` | `POST /v1/mobile/vehicle-borrows/cancel` |
| `POST /v1/mobile/auth/change-password` | `PATCH /v1/mobile/auth/password` |
| `GET /v1/mobile/realtime/events` | `GET /v1/mobile/notifications/events` |

## 6. Notification ownership

- `nsp.notification` is the notification inbox and source of truth.
- `nsp.notification.delivery` records transport state per channel/device.
- FCM and APNS credentials, queueing, retry and provider calls belong to Cloud `nsp_notification`.
- Mobile stores no FCM Service Account or APNS private key. It only registers its device token through login or `PATCH /v1/mobile/device`.
