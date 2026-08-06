from odoo import api, fields, models

from .rfid_target_helpers import (
    compute_active_assignment,
    compute_tid_input,
    inverse_tid_input,
    revoke_assignments_before_archive,
    revoke_target_assignments,
)


class NspVehicleRfidAssignment(models.Model):
    _inherit = "nsp.vehicle"

    rfid_assignment_ids = fields.One2many(
        "nsp.rfid.tag.assignment",
        "vehicle_id",
        readonly=True,
    )
    active_rfid_assignment_id = fields.Many2one(
        "nsp.rfid.tag.assignment",
        compute="_compute_rfid_assignment",
    )
    rfid_tag_id = fields.Many2one(
        "nsp.rfid.tag",
        compute="_compute_rfid_assignment",
    )
    rfid_tid = fields.Char(
        string="RFID TID",
        compute="_compute_rfid_assignment",
    )
    rfid_tid_input = fields.Char(
        string="Scan RFID Tag",
        compute="_compute_rfid_tid_input",
        inverse="_inverse_rfid_tid_input",
        store=False,
    )

    @api.depends("rfid_assignment_ids.state", "rfid_assignment_ids.tag_id")
    def _compute_rfid_assignment(self):
        compute_active_assignment(self, "vehicle_id")

    @api.depends("rfid_tid")
    def _compute_rfid_tid_input(self):
        compute_tid_input(self)

    def _inverse_rfid_tid_input(self):
        inverse_tid_input(self)

    def action_revoke_rfid_tag(self):
        return revoke_target_assignments(self, "vehicle_id")

    def write(self, vals):
        revoke_assignments_before_archive(self, vals, "vehicle_id")
        return super().write(vals)
