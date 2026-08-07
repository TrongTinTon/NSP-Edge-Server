# -*- coding: utf-8 -*-
from odoo import api, fields, models

from ..services.lane_direction_setup_service import LaneDirectionSetupService


class NspLaneDirectionSetupWizard(models.TransientModel):
    _name = "nsp.lane.direction.setup.wizard"
    _description = "Lane Direction Setup"

    session_id = fields.Many2one(
        "nsp.measurement.session", required=True, readonly=True, ondelete="cascade"
    )
    parking_area_id = fields.Many2one(
        related="lane_id.parking_area_id",
        string="Parking Layout",
        readonly=True,
    )
    edge_server_id = fields.Many2one(
        "nsp.edge.server", string="Server", required=True, readonly=True
    )
    controller_id = fields.Many2one(
        "nsp.controller", string="Controller", required=True, readonly=True
    )
    lane_id = fields.Many2one(
        "nsp.parking.lane", string="Lane", ondelete="cascade",
        help=(
            "Select an existing Lane or type a name to quick-create one. "
            "Lane is required only when saving the direction setup."
        ),
    )
    direction = fields.Selection(
        [("lane_in", "Lane In"), ("lane_out", "Lane Out")],
        string="Lane Direction",
        required=True,
        default="lane_in",
    )
    line_ids = fields.One2many(
        "nsp.lane.direction.setup.wizard.line", "wizard_id", string="Observed Timeline"
    )
    point_count = fields.Integer(compute="_compute_point_count")

    @api.depends("line_ids")
    def _compute_point_count(self):
        for wizard in self:
            wizard.point_count = len(wizard.line_ids)

    def action_save_direction(self):
        self.ensure_one()
        return LaneDirectionSetupService(self.env).apply(self)


class NspLaneDirectionSetupWizardLine(models.TransientModel):
    _name = "nsp.lane.direction.setup.wizard.line"
    _description = "Observed Lane Direction Point"
    _order = "sequence, id"

    wizard_id = fields.Many2one(
        "nsp.lane.direction.setup.wizard", required=True, ondelete="cascade", index=True
    )
    sequence = fields.Integer(string="#", required=True, readonly=True)
    event_id = fields.Many2one("nsp.measurement.event", readonly=True, ondelete="set null")
    reader_id = fields.Many2one("nsp.device", string="Reader", required=True, readonly=True)
    serial_number = fields.Char(string="Serial", readonly=True)
    port_no = fields.Integer(string="Port", required=True, readonly=True)
    observed_at = fields.Datetime(string="Timestamp", readonly=True)
    observed_at_ms = fields.Integer(string="ms", readonly=True)
    duration_from_previous = fields.Float(
        string="Duration from Previous (s)", readonly=True, digits=(8, 3)
    )
    reader_power_dbm = fields.Integer(string="Power (dBm)", readonly=True)
    read_interval_ms = fields.Integer(string="Read Interval (ms)", readonly=True)
    tid_start_address = fields.Integer(string="TID Start", readonly=True)
    tid_length = fields.Integer(string="TID Length", readonly=True)
