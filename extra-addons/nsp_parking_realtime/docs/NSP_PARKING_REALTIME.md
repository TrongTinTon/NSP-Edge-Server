# NSP Parking Realtime

Dedicated module for realtime parking operation UI.

Architecture:

```text
nsp_gatekeeper
→ nsp.parking.transaction is the business source of truth

nsp_notification
→ parking_monitor channel delivers display notifications from those transactions

nsp_parking_realtime
→ menu/client action for monitor screens
```

For an Odoo backend operator screen, the client action may consume the internal logged-in controller.

For a real parking monitor/kiosk/browser outside Odoo backend, the screen must use Core API Application:

```text
POST /auth/token?db=<dbname>
GET  /parking-monitor/v1/parking-monitor/events?db=<dbname>
Authorization: Bearer <access_token>
```

Application:

```text
Name: NSP Parking Monitor
Server Code: parking-monitor
```
