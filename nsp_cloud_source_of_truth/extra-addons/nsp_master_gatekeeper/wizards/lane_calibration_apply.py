# -*- coding: utf-8 -*-
from odoo import api, fields, models

from ..services.calibration_apply_service import CalibrationApplyService


class NspMeasurementApplyLaneWizard(models.TransientModel):
    _name = "nsp.measurement.apply.lane.wizard"
    _description = "Apply Lane Calibration Timeline"

    session_id = fields.Many2one(
        "nsp.measurement.session", required=True, readonly=True, ondelete="cascade"
    )
    parking_area_id = fields.Many2one(
        "nsp.parking.area", string="Parking Layout", ondelete="cascade"
    )
    edge_server_id = fields.Many2one(
        "nsp.edge.server", string="Server", required=True, readonly=True
    )
    controller_id = fields.Many2one(
        "nsp.controller", string="Controller", required=True, readonly=True
    )
    lane_id = fields.Many2one(
        "nsp.parking.lane", string="Lane", ondelete="cascade"
    )
    line_ids = fields.One2many(
        "nsp.measurement.apply.lane.wizard.line", "wizard_id", string="Selected Timeline"
    )
    selected_count = fields.Integer(compute="_compute_selected_count")
    checkin_overview = fields.Text(string="Check-in", readonly=True)
    checkout_overview = fields.Text(string="Check-out", readonly=True)

    @api.depends("line_ids")
    def _compute_selected_count(self):
        for wizard in self:
            wizard.selected_count = len(wizard.line_ids)

    @api.onchange("parking_area_id")
    def _onchange_parking_area_id(self):
        for wizard in self:
            if wizard.lane_id and wizard.lane_id.parking_area_id != wizard.parking_area_id:
                wizard.lane_id = False

    @api.onchange("lane_id")
    def _onchange_lane_id(self):
        for wizard in self:
            if wizard.lane_id:
                wizard.parking_area_id = wizard.lane_id.parking_area_id

    def action_save_configuration(self):
        self.ensure_one()
        return CalibrationApplyService(self.env).apply(self)


class NspMeasurementApplyLaneWizardLine(models.TransientModel):
    _name = "nsp.measurement.apply.lane.wizard.line"
    _description = "Selected Lane Calibration Detection"
    _order = "selection_order, id"

    wizard_id = fields.Many2one(
        "nsp.measurement.apply.lane.wizard", required=True, ondelete="cascade", index=True
    )
    selection_order = fields.Integer(string="#", required=True, readonly=True)
    event_id = fields.Many2one("nsp.measurement.event", readonly=True, ondelete="set null")
    reader_id = fields.Many2one("nsp.device", string="Reader", required=True, readonly=True)
    serial_number = fields.Char(string="Serial", readonly=True)
    port_no = fields.Integer(string="Port", required=True, readonly=True)
    observed_at = fields.Datetime(string="Detected", readonly=True)
    observed_at_ms = fields.Integer(string="ms", readonly=True)
    duration_from_previous = fields.Float(string="Duration from Previous (s)", readonly=True, digits=(8, 3))
    reader_power_dbm = fields.Integer(string="Power (dBm)", readonly=True)
    read_interval_ms = fields.Integer(string="Read Interval (ms)", readonly=True)
    tid_start_address = fields.Integer(string="TID Start", readonly=True)
    tid_length = fields.Integer(string="TID Length", readonly=True)
    checkin_order = fields.Integer(string="Check-in #", readonly=True)
    checkout_order = fields.Integer(string="Check-out #", readonly=True)
