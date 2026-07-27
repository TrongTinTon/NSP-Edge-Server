# NSP User

`nsp.user` is the **Master Business User** for NSP. It is independent from Odoo HR and must not be replaced by `res.users`.

## Identity ownership

- `nsp.user`: business identity used by RFID, Vehicle, Friends, Vehicle Borrow, Parking, Mobile and Notification.
- `res.users`: optional Odoo backend account used only for Web login, Groups, ACLs, Record Rules and backend administration.
- `nsp.user.web_user_id`: optional one-to-one link to an internal `res.users` account. A Web Account is created/linked only when the person needs Odoo backend access.

There is no password, active-state or role synchronization between the two identities. Archiving an NSP User does not automatically disable the linked Odoo account, and disabling an Odoo account does not archive the NSP business identity.

## Business models

- `nsp.user`: stable NSP business identity and contact information.
- `nsp.user.card`: assignment history between a User and User RFID Cards.
- `nsp.user.friendship`: friend relationship used by Vehicle Borrow.
- `nsp_mobile` extends `nsp.user` with Mobile login/password and binds Mobile Tokens to `nsp.user`.

## Architecture rules

- Cloud owns the master `nsp.user` record.
- Edge receives only the user/card data required for business runtime.
- Controller never stores or authenticates NSP business users.
- Mobile APIs authenticate and authorize `nsp.user`; they do not require `res.users`.
- Odoo Web permissions always remain on `res.users` and `res.groups`.
- `user_code` is immutable and is the stable identifier for Cloud/Edge synchronization.
- Users are archived rather than deleted.
- User Card assignments are revoked/reactivated rather than rewritten, preserving assignment history.
- A Master RFID Card can have only one active assignment.
- Friendships start as `pending`; only explicit acceptance changes them to `accepted`.

## Profile image

- `nsp.user` uses Odoo `image.mixin` for the user avatar/profile image.
- Profile images remain Cloud/Master presentation data and are not synchronized to Edge/Controller runtime.
