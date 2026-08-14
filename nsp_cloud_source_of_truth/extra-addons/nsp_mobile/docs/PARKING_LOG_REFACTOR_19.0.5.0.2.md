# Mobile Parking Log Refactor — 19.0.5.0.2

- Mobile parking history is exposed as `GET /v1/mobile/parking/logs`.
- Parking history reads the authoritative Cloud model `nsp.parking.log`.
- API handler, endpoint code, serializer and response fields use Parking Log terminology.
- A pre-migration renames installed legacy external IDs so existing Core API and security records are updated in place.
- Retired T4 Core API authentication rate-limit hooks are removed; IP allowlist enforcement remains.
- Mobile login authenticates the linked internal Odoo account and issues a device/session-bound token for the `nsp.user` business identity.
