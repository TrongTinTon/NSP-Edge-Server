from odoo import api, fields, models


class NspUserVehicleExtension(models.Model):
    _inherit = "nsp.user"

    vehicle_ids = fields.One2many("nsp.vehicle", "owner_id", string="Vehicles")
    vehicle_count = fields.Integer(compute="_compute_vehicle_count", string="Vehicles")
    can_manage_vehicles = fields.Boolean(
        compute="_compute_can_manage_vehicles",
        string="Can Manage Vehicles",
    )

    @api.depends("active", "odoo_user_id")
    def _compute_can_manage_vehicles(self):
        admin = bool(
            self.env.is_superuser()
            or self.env.user.has_group("base.group_system")
            or self.env.user.has_group("nsp_core.group_nsp_it_parking")
            or self.env.user.has_group("nsp_core.group_nsp_hr_parking")
        )
        current_user_id = self.env.user.id
        for user in self:
            linked_user_id = user.sudo().odoo_user_id.id if user.id else 0
            user.can_manage_vehicles = bool(admin or linked_user_id == current_user_id)

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
