# NSP User

`nsp.user` is the master business identity for NSP business functions.

## Identity ownership

- `nsp.user` owns the business identity used by Mobile and NSP business modules.
- `res.users` is an optional Odoo Web access account.
- One internal Odoo account can be linked to at most one NSP User.
- Portal and public accounts cannot be linked.
- `user_code` is generated once and remains the stable Cloud/Edge synchronization key.

## Related module ownership

- `nsp_vehicle` owns Vehicle data and ownership relations.
- `nsp_rfid` owns the canonical RFID Tag Whitelist.
- `nsp_rfid_assignment` owns assignment, revoke history, audit messages, and runtime projection generation.

## Fresh installation

This module contains the final schema only and does not include migrations for earlier versions.
