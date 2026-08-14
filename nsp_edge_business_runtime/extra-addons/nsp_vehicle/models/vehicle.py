from odoo import api, fields, models, _
from odoo.exceptions import AccessError, ValidationError
from odoo.addons.nsp_core.utils import new_management_code


class Vehicle(models.Model):
    _name = "nsp.vehicle"
    _description = "Vehicle Management"
    _inherit = ["mail.thread", "mail.activity.mixin", "image.mixin"]
    _rec_name = "license_plate"
    _order = "license_plate, id"

    vehicle_code = fields.Char(
        string="Technical Code",
        required=True,
        readonly=True,
        copy=False,
        index=True,
        default=lambda self: new_management_code("VEH"),
        help="Stable system-generated identifier used for Cloud and Edge synchronization.",
    )
    license_plate = fields.Char(
        string="License Plate",
        required=True,
        tracking=True,
        index=True,
    )
    owner_id = fields.Many2one(
        "nsp.user",
        string="Owner",
        tracking=True,
        ondelete="restrict",
        index=True,
    )
    vehicle_type_id = fields.Many2one(
        "nsp.vehicle.type",
        string="Vehicle Type",
        ondelete="set null",
        tracking=True,
    )
    brand_id = fields.Many2one(
        "nsp.reference.brand",
        string="Brand",
        ondelete="set null",
        tracking=True,
        index=True,
    )
    model_id = fields.Many2one(
        "nsp.reference.model",
        string="Model",
        ondelete="set null",
        tracking=True,
        index=True,
    )
    color_id = fields.Many2one(
        "nsp.vehicle.color",
        string="Color",
        ondelete="set null",
        tracking=True,
    )
    active = fields.Boolean(default=True, tracking=True, index=True)
    borrow_ids = fields.One2many(
        "nsp.vehicle.borrow",
        "vehicle_id",
        string="Authorized Users",
    )

    can_administer_vehicle = fields.Boolean(
        compute="_compute_vehicle_ui_access",
        string="Can Administer Vehicle",
    )
    can_manage_access = fields.Boolean(
        compute="_compute_vehicle_ui_access",
        string="Can Manage Vehicle Access",
    )

    _sql_constraints = [
        ("vehicle_code_uniq", "unique(vehicle_code)", "Vehicle Technical Code must be unique."),
        ("license_plate_uniq", "unique(license_plate)", "This license plate already exists in the system!"),
    ]

    @api.model
    def _is_vehicle_admin(self):
        return bool(
            self.env.is_superuser()
            or self.env.user.has_group("base.group_system")
            or self.env.user.has_group("nsp_core.group_nsp_it_parking")
            or self.env.user.has_group("nsp_core.group_nsp_hr_parking")
        )

    @api.model
    def _current_identity(self, required=True):
        User = self.env["nsp.user"]
        resolver = getattr(User, "_current_nsp_identity", None)
        if callable(resolver):
            return resolver(required=required)
        identity = User.sudo().search([("odoo_user_id", "=", self.env.user.id)], limit=1)
        if required and not identity:
            raise AccessError(_(
                "Your Odoo account is not linked to an NSP User identity. Please contact IT."
            ))
        return identity

    def _compute_vehicle_ui_access(self):
        admin = self._is_vehicle_admin()
        identity = self._current_identity(required=False)
        for vehicle in self:
            is_owner = bool(identity and vehicle.owner_id == identity)
            vehicle.can_administer_vehicle = admin
            vehicle.can_manage_access = bool(admin or is_owner)

    @api.model
    def _normalize_license_plate(self, value):
        return " ".join(str(value or "").strip().upper().split()) or False

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for source in vals_list:
            vals = dict(source)
            vals["vehicle_code"] = str(
                vals.get("vehicle_code") or new_management_code("VEH")
            ).strip().upper()
            vals["license_plate"] = self._normalize_license_plate(vals.get("license_plate"))
            prepared.append(vals)
        return super().create(prepared)

    def write(self, vals):
        values = dict(vals)
        if not self._is_vehicle_admin():
            identity = self._current_identity(required=True)
            if any(vehicle.owner_id != identity for vehicle in self):
                raise AccessError(_("You can modify only vehicles that you own."))
            forbidden = {"owner_id", "vehicle_code", "active"}.intersection(values)
            if forbidden:
                raise AccessError(
                    _("Only HR Parking Officer or IT Parking Admin can change Vehicle ownership or system state.")
                )

        if "vehicle_code" in values:
            normalized = str(values.get("vehicle_code") or "").strip().upper()
            if any(vehicle.vehicle_code and vehicle.vehicle_code != normalized for vehicle in self):
                raise ValidationError(_("Vehicle Technical Code cannot be changed after creation."))
            values["vehicle_code"] = normalized
        if "license_plate" in values:
            values["license_plate"] = self._normalize_license_plate(values.get("license_plate"))
        return super().write(values)

    @api.onchange("brand_id")
    def _onchange_brand_id(self):
        for vehicle in self:
            if vehicle.model_id and vehicle.model_id.brand_id != vehicle.brand_id:
                vehicle.model_id = False

    @api.constrains("brand_id", "model_id")
    def _check_model_brand(self):
        for vehicle in self:
            if vehicle.model_id and vehicle.model_id.brand_id != vehicle.brand_id:
                raise ValidationError(_("Vehicle Model must belong to the selected Brand."))

    def action_archive(self):
        if not self._is_vehicle_admin():
            raise AccessError(_("Only HR Parking Officer or IT Parking Admin can archive vehicles."))
        self.filtered("active").write({"active": False})
        return True

    def action_unarchive(self):
        if not self._is_vehicle_admin():
            raise AccessError(_("Only HR Parking Officer or IT Parking Admin can restore vehicles."))
        self.filtered(lambda vehicle: not vehicle.active).write({"active": True})
        return True
