# NSP User

`nsp_user` is the Cloud/Edge master identity module for NSP people. It does not depend on Odoo HR.

## Responsibilities

- `nsp.user`: stable NSP user identity and business contact information.
- `nsp.user.card`: assignment history between a User and User RFID Cards.
- `nsp.user.friendship`: lightweight friend relationship used by Vehicle Borrow.

## Design rules

- `user_code` is a system/Cloud sync identifier. It is generated automatically and immutable after creation.
- Business UI uses the User name; Technical Code is only shown to IT administrators.
- Users are archived instead of deleted.
- User Card assignments are revoked/reactivated instead of deleted, preserving assignment history.
- A Master RFID Card can have only one active assignment.
- New friendships always start as `pending`; only `action_accept()` changes them to `accepted`.
- Mobile authentication is implemented by the optional Cloud-only `nsp_mobile` module, not by `nsp_user` itself.
