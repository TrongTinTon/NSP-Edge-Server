# Detection Identity Badge — 19.0.10.53.0

Detection Logs keep one operator-facing **Identity** column. The value remains:

- `User: <display name>`
- `Vehicle: <license plate>`

The list/form renders this field with Odoo's native `badge` widget:

- User identity: `decoration-info`
- Vehicle identity: `decoration-success`

`user_id` and `vehicle_id` remain typed backend relations and are loaded invisibly only so the view can choose the correct badge decoration. No custom JS/CSS or polymorphic identity storage is introduced.
