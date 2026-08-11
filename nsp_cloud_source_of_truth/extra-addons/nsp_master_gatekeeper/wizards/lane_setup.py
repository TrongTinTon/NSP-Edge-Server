# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

from ..services.lane_setup_service import LaneSetupService


class NspLaneSetupWizard(models.TransientModel):
    _name = "nsp.lane.setup.wizard"
    _description = "Lane Setup"

    source_scope = fields.Selection(
        [
            ("calibration", "Lane Calibration"),
            ("parking_layout", "Parking Layout"),
        ],
        string="Setup Source",
        required=True,
        default="calibration",
        readonly=True,
    )
    session_id = fields.Many2one(
        "nsp.measurement.session", readonly=True, ondelete="cascade"
    )
    edge_server_id = fields.Many2one(
        "nsp.edge.server", string="Server", required=True, readonly=True
    )
    controller_id = fields.Many2one(
        "nsp.controller", string="Controller", required=True, readonly=True
    )
    lane_id = fields.Many2one(
        "nsp.parking.lane",
        string="Lane",
        ondelete="cascade",
        help="Select an existing Lane or type a name to quick-create one.",
    )
    direction = fields.Selection(
        [("lane_in", "Lane In"), ("lane_out", "Lane Out")],
        string="Direction",
        required=True,
        default="lane_in",
    )
    active_section = fields.Selection(
        [
            ("device", "Device"),
            ("lane_in", "Lane In"),
            ("lane_out", "Lane Out"),
        ],
        string="Lane Setup Section",
        required=True,
        default="device",
    )
    device_line_ids = fields.One2many(
        "nsp.lane.setup.device.line", "wizard_id", string="Device Configuration"
    )
    direction_line_ids = fields.One2many(
        "nsp.lane.setup.direction.line", "wizard_id", string="Antenna Sequence"
    )
    available_reader_ids = fields.Many2many(
        "nsp.device",
        compute="_compute_available_reader_ids",
        string="Available Readers",
    )
    direction_preview_anchor = fields.Boolean(
        string="Direction Preview",
        default=True,
        readonly=True,
        help="Presentation-only anchor for the live Lane Direction preview widget.",
    )

    @api.depends("source_scope", "session_id", "session_id.reader_line_ids.reader_id", "session_id.reader_line_ids.reader_id.active")
    def _compute_available_reader_ids(self):
        """Apply Reader scope according to the Lane Setup entry point.

        * Lane Calibration -> only Readers configured on that Calibration.
        * Parking Layout   -> every active Reader is selectable.
        """
        Device = self.env["nsp.device"]
        unrestricted = self.filtered(lambda wizard: wizard.source_scope != "calibration")
        all_active = (
            Device.search([("active", "=", True)])
            if unrestricted
            else Device.browse()
        )
        for wizard in self:
            if wizard.source_scope == "calibration" and wizard.session_id:
                wizard.available_reader_ids = wizard.session_id.reader_line_ids.mapped(
                    "reader_id"
                ).filtered("active")
            else:
                wizard.available_reader_ids = all_active

    def _allowed_reader_port_pairs(self):
        """Return the calibrated Reader/Port allowlist, or ``None`` when unrestricted."""
        self.ensure_one()
        if self.source_scope != "calibration":
            return None
        if not self.session_id:
            return set()
        return {
            (line.reader_id.id, int(port.port_no or 0))
            for line in self.session_id.reader_line_ids
            for port in line.reader_port_ids
            if line.reader_id and int(port.port_no or 0) > 0
        }

    def _switch_section(self, section):
        self.ensure_one()
        if section not in ("device", "lane_in", "lane_out"):
            raise ValidationError(_("Unsupported Lane Setup section."))
        values = {"active_section": section}
        if section in ("lane_in", "lane_out"):
            values["direction"] = section
        self.write(values)
        return self._reopen_action()

    def action_show_device(self):
        return self._switch_section("device")

    def action_show_lane_in(self):
        return self._switch_section("lane_in")

    def action_show_lane_out(self):
        return self._switch_section("lane_out")

    def _reopen_action(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Lane Setup"),
            "res_model": "nsp.lane.setup.wizard",
            "res_id": self.id,
            "view_mode": "form",
            "views": [(
                self.env.ref(
                    "nsp_master_gatekeeper.view_nsp_lane_direction_setup_wizard_form"
                ).id,
                "form",
            )],
            "target": "new",
            "context": dict(self.env.context),
        }

    def action_save(self):
        """Persist the current Lane Setup from the single Save action."""
        self.ensure_one()
        return LaneSetupService(self.env).save(self, apply_setup=True)

    def action_save_setup(self):
        """Compatibility alias for older views/API. Removal target: NSP 20.0."""
        return self.action_save()

    def action_apply_setup(self):
        """Compatibility alias for NSP 19.x callers."""
        return self.action_save()

    def action_save_draft(self):
        """Deprecated compatibility alias. Lane Setup now has one Save action."""
        return self.action_save()


class NspLaneSetupDeviceLine(models.TransientModel):
    _name = "nsp.lane.setup.device.line"
    _description = "Lane Setup Device Configuration"
    _order = "reader_id, id"

    wizard_id = fields.Many2one(
        "nsp.lane.setup.wizard", required=True, ondelete="cascade", index=True
    )
    available_reader_ids = fields.Many2many(
        "nsp.device",
        related="wizard_id.available_reader_ids",
        readonly=True,
    )
    reader_id = fields.Many2one(
        "nsp.device", string="Reader", required=True
    )
    port_summary = fields.Char(string="Ports", compute="_compute_port_summary")
    power_dbm = fields.Integer(string="Power (dBm)", required=True, default=30)
    read_interval_ms = fields.Integer(
        string="Read Interval (ms)", required=True, default=200
    )
    tid_start_address = fields.Integer(
        string="TID Start", required=True, default=0
    )
    tid_length = fields.Integer(string="TID Length", required=True, default=4)

    @api.depends(
        "wizard_id.direction_line_ids.reader_id",
        "wizard_id.direction_line_ids.port_no",
        "reader_id",
    )
    def _compute_port_summary(self):
        for line in self:
            ports = sorted({
                int(point.port_no or 0)
                for point in line.wizard_id.direction_line_ids
                if point.reader_id == line.reader_id and int(point.port_no or 0) > 0
            })
            line.port_summary = ", ".join("P%s" % port for port in ports)


class NspLaneSetupDirectionLine(models.TransientModel):
    _name = "nsp.lane.setup.direction.line"
    _description = "Lane Setup Direction Point"
    _order = "sequence, id"

    wizard_id = fields.Many2one(
        "nsp.lane.setup.wizard", required=True, ondelete="cascade", index=True
    )
    sequence = fields.Integer(string="#", required=True, default=10)
    reader_id = fields.Many2one(
        "nsp.device", string="Reader", required=True
    )
    available_reader_ids = fields.Many2many(
        "nsp.device",
        related="wizard_id.available_reader_ids",
        readonly=True,
    )
    port_no = fields.Integer(string="Antenna", required=True)
    antenna = fields.Char(string="Antenna", compute="_compute_antenna")
    reader_identity = fields.Char(
        string="Reader Identity", compute="_compute_antenna"
    )
    duration_ms = fields.Integer(
        string="Max Duration from Previous (ms)", required=True, default=0,
        help="Maximum allowed time from the previous Antenna. The first point uses 0 ms.",
    )

    @api.depends("reader_id", "reader_id.serial_number", "reader_id.device_code", "port_no")
    def _compute_antenna(self):
        for line in self:
            reader_name = (
                line.reader_id.name
                or line.reader_id.device_code
                or line.reader_id.serial_number
                or _("Reader")
            )
            line.antenna = "%s-P%s" % (reader_name, int(line.port_no or 0))
            identity = line.reader_id.serial_number or line.reader_id.device_code or ""
            line.reader_identity = (
                "%s · Antenna %s" % (identity, int(line.port_no or 0))
                if identity
                else _("Antenna %s") % int(line.port_no or 0)
            )

    @api.onchange("reader_id", "port_no")
    def _onchange_reader_port(self):
        if self.port_no and (int(self.port_no) < 1 or int(self.port_no) > 16):
            return {
                "warning": {
                    "title": _("Invalid Antenna"),
                    "message": _("Antenna/Port must be between 1 and 16."),
                }
            }
        if self.reader_id and self.port_no and self.wizard_id.source_scope == "calibration":
            allowed_pairs = self.wizard_id._allowed_reader_port_pairs()
            key = (self.reader_id.id, int(self.port_no or 0))
            if key not in allowed_pairs:
                allowed_ports = sorted(
                    port_no
                    for reader_id, port_no in allowed_pairs
                    if reader_id == self.reader_id.id
                )
                return {
                    "warning": {
                        "title": _("Antenna outside Lane Calibration"),
                        "message": _(
                            "This Lane Setup was opened from Lane Calibration. "
                            "Use one of the calibrated Antennas for this Reader: %(ports)s."
                        ) % {
                            "ports": ", ".join("P%s" % port for port in allowed_ports) or _("none"),
                        },
                    }
                }
        return False
