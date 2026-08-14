from odoo import api, fields, models, _
from odoo.exceptions import AccessError


class NspUserVehicleExtension(models.Model):
    _inherit = "nsp.user"

    vehicle_ids = fields.One2many("nsp.vehicle", "owner_id", string="Vehicles")
    vehicle_count = fields.Integer(compute="_compute_vehicle_count", string="Vehicles")
    can_manage_vehicles = fields.Boolean(
        compute="_compute_can_manage_vehicles",
        string="Can Manage Vehicles",
    )

    @api.model
    def _vehicle_current_identity(self, required=False):
        """Resolve the current NSP identity across old/new nsp_user versions.

        nsp_user >= 19.0.16 exposes _current_nsp_identity().  Edge deployments
        may still run 19.0.15, where the authoritative link is odoo_user_id.
        """
        User = self.env["nsp.user"]
        resolver = getattr(User, "_current_nsp_identity", None)
        if callable(resolver):
            return resolver(required=required)

        identity = User.sudo().search([
            ("odoo_user_id", "=", self.env.user.id),
        ], limit=1)
        if required and not identity:
            raise AccessError(_(
                "Your Odoo account is not linked to an NSP User identity. Please contact IT."
            ))
        return identity

    @api.depends("vehicle_ids")
    def _compute_vehicle_count(self):
        counts = self.env["nsp.vehicle"].sudo()._read_group(
            [("owner_id", "in", self.ids)],
            ["owner_id"],
            ["__count"],
        ) if self.ids else []
        count_by_owner = {owner.id: count for owner, count in counts if owner}
        for user in self:
            user.vehicle_count = count_by_owner.get(user.id, 0)

    def _compute_can_manage_vehicles(self):
        is_admin = bool(
            self.env.is_superuser()
            or self.env.user.has_group("base.group_system")
            or self.env.user.has_group("nsp_core.group_nsp_it_parking")
            or self.env.user.has_group("nsp_core.group_nsp_hr_parking")
        )
        identity = self._vehicle_current_identity(required=False)
        for user in self:
            user.can_manage_vehicles = bool(is_admin or (identity and user.id == identity.id))
