from odoo import api, fields, models


class NspUserVehicleExtension(models.Model):
    _inherit = "nsp.user"

    vehicle_ids = fields.One2many("nsp.vehicle", "owner_id", string="Vehicles")
    vehicle_count = fields.Integer(compute="_compute_vehicle_count", string="Vehicles")

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
