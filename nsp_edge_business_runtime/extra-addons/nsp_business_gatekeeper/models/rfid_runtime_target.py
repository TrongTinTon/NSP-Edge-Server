from odoo import api, fields, models


class NspUserRfidRuntime(models.Model):
    _inherit = "nsp.user"

    runtime_rfid_assignment_ids = fields.One2many(
        "nsp.rfid.runtime.assignment",
        "user_id",
        readonly=True,
    )
    runtime_rfid_tid = fields.Char(compute="_compute_runtime_rfid_tid")

    @api.depends("runtime_rfid_assignment_ids.tid")
    def _compute_runtime_rfid_tid(self):
        for user in self:
            assignment = user.runtime_rfid_assignment_ids[:1]
            user.runtime_rfid_tid = assignment.tid if assignment else False


class NspVehicleRfidRuntime(models.Model):
    _inherit = "nsp.vehicle"

    runtime_rfid_assignment_ids = fields.One2many(
        "nsp.rfid.runtime.assignment",
        "vehicle_id",
        readonly=True,
    )
    runtime_rfid_tid = fields.Char(compute="_compute_runtime_rfid_tid")

    @api.depends("runtime_rfid_assignment_ids.tid")
    def _compute_runtime_rfid_tid(self):
        for vehicle in self:
            assignment = vehicle.runtime_rfid_assignment_ids[:1]
            vehicle.runtime_rfid_tid = assignment.tid if assignment else False
