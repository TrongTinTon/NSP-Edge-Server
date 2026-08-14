# Friendships Gateway Route — 19.0.13.48.0

## Symptom

Edge `Friendships Snapshot` fails with:

`No gateway route configured for: /v1/edge/friendships/snapshot`

## Root cause

`t4_coreapi` separates:

- `ir.actions.core_api`: module-defined server action/endpoint metadata;
- `core.api.endpoint`: public Gateway Route granted to a specific Application and API Version.

The Friendship Core API action existed, but existing Edge Service Applications were created before the new route and therefore had no per-Application Gateway Route for `edge/friendships/snapshot`.

## Fix

The post-migration provisions/repairs the Friendship Gateway Route only for Application/API-Version pairs that already own `edge/users/snapshot` or `edge/vehicle-borrows/snapshot`.

This keeps the existing Cloud -> Edge Master Data authorization boundary and does not grant the route to unrelated Applications.
