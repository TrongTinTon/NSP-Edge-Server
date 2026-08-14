# NSP Vehicle 19.0.19.4.1 - User Vehicle View Compatibility

## Symptom
Upgrading `nsp_vehicle` failed while validating `views/user_vehicle_views.xml` when the installed `nsp_user` still used the older User form without `can_edit_profile`.

## Root cause
The Vehicle inherited view referenced the newer `nsp_user.can_edit_profile` UI helper field. This created an unnecessary cross-version UI dependency.

## Fix
`nsp_vehicle` now owns `nsp.user.can_manage_vehicles` and uses it only for the Vehicles tab visibility. The compute supports both older and newer `nsp_user` versions by relying on the long-lived `odoo_user_id` relation and Parking admin groups.

The Vehicle Borrow lifecycle fix from 19.0.19.4.0 is unchanged. Interactive Cloud creates still begin as Active with no `returned_at`; only Cloud-to-Edge sync context may import historical Returned/Cancelled states.
