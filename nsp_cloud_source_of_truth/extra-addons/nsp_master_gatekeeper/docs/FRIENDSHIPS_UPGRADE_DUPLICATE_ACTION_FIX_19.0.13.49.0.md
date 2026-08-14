# Friendships Core API Upgrade Duplicate Action Fix — 19.0.13.49.0

## Symptom

Upgrading `nsp_master_gatekeeper` fails with T4 Core API constraint message:

`unique name with manager`

## Root cause

T4 Core API enforces `UNIQUE(endpoint_manager_id, name)` on `ir.actions.core_api`.
A `Friendships Snapshot` action may already have been generated from the `@endpoint`
declaration but have no `nsp_master_gatekeeper.api_master_friendships` XML ID. Module
XML then attempts to create the same action again during upgrade.

## Fix

- pre-migration finds the already-existing action by manager + exact name, endpoint code,
  or route path;
- it binds/rebinds the stable module XML ID to that action before XML data is loaded;
- XML therefore updates the existing action rather than creating a duplicate;
- gateway-route provisioning no longer calls `_generate_core_api_action()`;
- post-migration creates/repairs only `core.api.endpoint` rows for Application/API-Version
  pairs already authorized for Users or Vehicle Borrows snapshots.

No Cloud/Edge ownership or synchronization direction changes are introduced.
