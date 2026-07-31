# NSP User

`nsp.user` is the NSP business identity used by Parking, RFID assignment, Friends, Vehicle Borrow and Notifications.

## Odoo User linkage

- `odoo_user_id` links one `nsp.user` to one active internal `res.users` account.
- The same Odoo User is used for Web and NSP Mobile authentication.
- The link is optional; an NSP User without an Odoo User cannot sign in to NSP Mobile.
- Passwords, activation, Groups, ACLs and Record Rules are managed only through standard Odoo user administration.
- `user_code` remains a hidden immutable technical identifier for Cloud/Edge synchronization.

## RFID and vehicles

RFID and vehicle functions are added by `nsp_vehicle`:

- Every User may have one active Employee RFID Tag assignment.
- Every Vehicle belongs to one User and may have one active RFID Tag assignment.
- Employee RFID assignment and revoke actions are written to the User chatter; Odoo records the operator and timestamp.
- Friends remain the selection boundary for Vehicle Borrow. Being a Friend alone does not authorize vehicle check-out.
- Vehicle Borrow must be active at the parking event time.
