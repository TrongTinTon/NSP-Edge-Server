# NSP Mobile Refactor — 2026-08-01

## Scope

This package changes only the Odoo/Cloud Mobile API and the mandatory Mobile identity pairing. Controller source and Controller API routes are unchanged.

## Identity

- Mobile authentication: `res.users`.
- Business profile: `nsp.user`.
- Cardinality for internal Odoo Users: mandatory 1:1.
- `nsp.user.odoo_user_id`: required, unique, immutable, `ondelete=restrict`.
- Creating an internal Odoo User automatically creates the corresponding NSP User in the same transaction.
- Installation backfills profiles for existing internal Odoo Users.
- Direct creation of NSP User records is disabled in the UI; identities are created from Odoo Users.

## Mobile route structure

- Base: `/v1/mobile`.
- Resource-oriented route names replace action-style legacy routes.
- Shared resource routes use HTTP methods where supported:
  - `GET,PATCH /v1/mobile/profile`
  - `GET,PATCH,DELETE /v1/mobile/device`
  - `GET,POST /v1/mobile/friend-requests`
  - `GET,POST /v1/mobile/vehicle-borrows`
- Persisted Core API endpoints and server actions are automatically cleaned when their decorator metadata is removed from source.

## Push notifications

- FCM/APNS provider credentials remain Cloud configuration in `nsp_notification`.
- Mobile sends only `push_provider`, `push_token` and `push_enabled` for its current device.
- Controller was not modified.

## Versions

- `nsp_user`: `19.0.14.0.0`
- `nsp_mobile`: `19.0.5.0.0`
