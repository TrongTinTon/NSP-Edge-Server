# Parking Detection Gateway Route Fix — 19.0.10.35.0

## Symptom

Controller receives HTTP 400:

`Gateway route "NSP Controller Parking Detection Push" has no Server Action configured.`

## Root cause

`t4_coreapi` stores generated public Gateway Routes (`core.api.endpoint`) separately
from module-defined Core API actions (`ir.actions.core_api`). Upgrading the action XML
or `@endpoint` method does not automatically repair an already-generated route whose
`action_id` became empty/stale.

The request therefore stops in the Gateway before
`model.api_parking_detection_push()` runs. No Detection Event can be persisted.

## Fix

The 19.0.10.35.0 post-migration:

1. synchronizes `ir.actions.core_api` metadata from the Endpoint Manager decorators;
2. matches existing generated routes by stable `endpoint_code` or `route_suffix`;
3. restores `core.api.endpoint.action_id` and `endpoint_manager_id`;
4. explicitly verifies/repairs `parking/detections/push`.

The migration repairs existing routes only. It does not grant a new Application access
or create routes for Applications that did not already have them.

## Verify

The Parking route should have a non-null `action_id` after module upgrade.
Use the T4 Core API Gateway Routes UI or inspect `core.api.endpoint` for
`route_suffix = parking/detections/push`.
