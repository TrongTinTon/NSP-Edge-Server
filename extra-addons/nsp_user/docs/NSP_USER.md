# NSP User

`nsp.user` is the NSP business profile used by Parking, RFID assignment, Friends, Vehicle Borrow and Notifications.

## Odoo User linkage

- Mobile authentication uses the internal Odoo User (`res.users`) login and password.
- Every internal Odoo User has exactly one `nsp.user` business profile.
- Every `nsp.user` must reference exactly one internal `res.users` account.
- The link is mandatory, immutable from the NSP User form and protected by a unique database constraint.
- Creating an internal Odoo User automatically creates its NSP User profile in the same transaction.
- Existing internal Odoo Users are backfilled when `nsp_user` is installed.
- Portal/public accounts are not Mobile identities and are excluded from this pairing rule.
- Passwords, activation, Groups, ACLs and Record Rules remain managed through standard Odoo user administration.
- `user_code` remains a hidden immutable technical identifier for Cloud/Edge synchronization.

## RFID and vehicles

RFID and vehicle functions are added by `nsp_vehicle`:

- Every User may have one active Employee RFID Tag assignment.
- Every Vehicle belongs to one User and may have one active RFID Tag assignment.
- Employee RFID assignment and revoke actions are written to the User chatter; Odoo records the operator and timestamp.
- Friends remain the selection boundary for Vehicle Borrow. Being a Friend alone does not authorize vehicle check-out.
- Vehicle Borrow must be active at the parking event time.
