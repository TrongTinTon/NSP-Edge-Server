# NSP User

`nsp.user` is the NSP Master Business Identity. It owns profile data, friendships and optional Odoo Web Account linkage.

RFID and vehicle functions are added by `nsp_vehicle`:

- Every User may have one active Employee RFID Tag assignment.
- Every Vehicle belongs to one User and may have one active RFID Tag assignment.
- RFID assignment history is immutable; revoke records who performed the action and when.
- Friends remain the selection boundary for Vehicle Borrow. Being a Friend alone does not authorize vehicle check-out.
- Vehicle Borrow must be active at the parking event time.
