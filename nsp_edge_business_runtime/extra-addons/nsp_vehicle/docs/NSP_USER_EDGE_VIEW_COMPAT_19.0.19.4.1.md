# NSP Vehicle 19.0.19.4.1 — Edge nsp_user compatibility

- Removes the inherited User view dependency on `nsp_user.can_edit_profile`.
- Adds local `nsp.user.can_manage_vehicles` for Vehicle page visibility.
- Supports `nsp_user 19.0.15.0.0` by resolving the current identity through `odoo_user_id` when `_current_nsp_identity()` is unavailable.
- Keeps the Vehicle Borrow create-state fix from 19.0.19.4.0.
