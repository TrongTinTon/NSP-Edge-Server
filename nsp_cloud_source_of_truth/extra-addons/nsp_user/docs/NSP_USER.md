# NSP User

`nsp.user` is the master business identity used by Parking, Friends, Vehicle Borrow, Notifications and Mobile business functions.

## Odoo Web access

- `res.users` is an optional Web access account.
- An `nsp.user` can exist without an Odoo login.
- Create or link `res.users` only when the business identity needs Web access, Groups or ACLs.
- One Odoo User can be linked to at most one NSP User.
- An existing link cannot be reassigned directly to another Odoo User, but it can be cleared.
- Portal and public accounts cannot be linked.
- Passwords, Groups, ACLs and Record Rules remain owned by standard Odoo user administration.
- `user_code` is the stable identity used for Cloud and Edge synchronization.

## Module ownership

- `nsp_vehicle` adds Vehicle ownership and borrowing relations.
- `nsp_rfid` on Cloud owns RFID Tag master data and whitelist.
- `nsp_rfid_assignment` on Cloud owns assignment, revocation, audit and runtime projection generation.
- Edge Gatekeeper owns only the runtime TID assignment projection required for Parking processing.
