# NSP Notification Native Push Design

## Module scope

`nsp_notification` stores business notifications and sends native mobile push through Cloud-side providers.
It does not modify `t4_coreapi`.

## Menu

NSP Notification
- Notifications
- Push Devices
- Push Deliveries
- Push Rules
- Push Providers
- Settings

## Runtime flow

1. Parking/Gatekeeper event creates `nsp.notification`.
2. Active `nsp.push.rule` decides recipients.
3. The module creates one `nsp.push.delivery` per target mobile device.
4. Cron `NSP Push Send Queue` sends queued deliveries through the selected provider.
5. Mobile app can register token, ack/read notifications and list inbox only through a Core API Application.

## Data minimization

No raw parking payload, antenna events or provider raw response payloads are stored.
Delivery records store only provider type, status, timestamps, retry counters, provider message id and short error text.
Provider secrets are referenced by `ir.config_parameter` key names instead of being stored directly on provider records.

## Native provider notes

- Android native push uses FCM or HMS depending on provider setup.
- iOS native push uses APNs.
- APNs sending requires Python packages with JWT ES256 and HTTP/2 support.
- If a connector dependency is missing, delivery fails gracefully with a short error code/message.


## Core API Applications

External notification clients must be represented by `core.api.application`:

```text
NSP Mobile App
→ server code: nsp-mobile
→ allowed routes: mobile/push/*, mobile/notifications/*

NSP Parking Monitor
→ server code: parking-monitor
→ allowed routes: parking-monitor/*
```

Authentication flow:

```text
POST /auth/token?db=<dbname>
→ returns access_token, refresh_token, server_code, gateway_base

GET/POST /<server_code>/v1/<route>?db=<dbname>
Authorization: Bearer <access_token>
```

After module upgrade, if the application was auto-created, regenerate the client secret from Core API → Applications before deploying a real mobile app or monitor device.
