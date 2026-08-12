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
        "nsp.edge.server", string="Server", required=True
    )
    controller_id = fields.Many2one(
        "nsp.controller", string="Controller", required=True
    )
    parking_area_id = fields.Many2one(
        "nsp.parking.area",
        string="Parking Layout",
        domain=[("state", "=", "draft")],
        help="Draft Parking Layout that will reference the selected independent Lane.",
    )
    parking_area_branch_id = fields.Many2one(
        "nsp.branch", related="parking_area_id.branch_id", readonly=True,
    )
    lane_id = fields.Many2one(
        "nsp.parking.lane",
        string="Lane",
        ondelete="set null",
        help="Independent Lane master. Parking Layout configuration is stored separately.",
    )
    layout_lane_id = fields.Many2one(
        "nsp.parking.layout.lane",
        string="Lane Configuration",
        ondelete="set null",
        readonly=True,
    )
    active_section = fields.Selection(
        [
            ("device", "Device"),
            ("sequence", "Antenna Sequence"),
        ],
        string="Lane Setup Section",
        required=True,
        default="device",
    )
    device_line_ids = fields.One2many(
        "nsp.lane.setup.device.line", "wizard_id", string="Device Configuration"
    )
    device_tree_anchor = fields.Boolean(
        string="Device Configuration",
        default=True,
        readonly=True,
        help="Presentation anchor for the reusable NSP Device Configuration Tree.",
    )
    sequence_line_ids = fields.One2many(
        "nsp.lane.setup.sequence.line", "wizard_id", string="Antenna Sequence"
    )
    available_reader_ids = fields.Many2many(
        "nsp.device",
        compute="_compute_available_reader_ids",
        string="Available Readers",
    )
    sequence_preview_anchor = fields.Boolean(
        string="Antenna Sequence Preview",
        default=True,
        readonly=True,
        help="Presentation-only anchor for the live Antenna Sequence preview widget.",
    )

    @api.onchange("parking_area_id")
    def _onchange_parking_area_id(self):
        for wizard in self:
            if (
                wizard.lane_id
                and wizard.parking_area_id
                and wizard.lane_id.branch_id != wizard.parking_area_id.branch_id
            ):
                wizard.lane_id = False
                wizard.layout_lane_id = False
            elif wizard.layout_lane_id and wizard.layout_lane_id.parking_area_id != wizard.parking_area_id:
                wizard.layout_lane_id = False

    @api.onchange("lane_id")
    def _onchange_lane_id(self):
        for wizard in self:
            if (
                wizard.lane_id and wizard.parking_area_id
                and wizard.lane_id.branch_id != wizard.parking_area_id.branch_id
            ):
                wizard.lane_id = False
                wizard.layout_lane_id = False
            elif wizard.layout_lane_id and wizard.layout_lane_id.lane_id != wizard.lane_id:
                wizard.layout_lane_id = False

    def _calibration_branch_nodes(self):
        """Return the contextual Server, Controller and Reader nodes for this Lane.

        Lane Calibration may contain many independent branches.  Lane Setup owns
        exactly one physical Lane, so its Device Configuration must be projected
        from the Server -> Controller branch selected by the Detection Timeline.
        """
        self.ensure_one()
        if (
            self.source_scope != "calibration"
            or not self.session_id
            or not self.edge_server_id
            or not self.controller_id
        ):
            Node = self.env["nsp.measurement.device.node"]
            return Node.browse(), Node.browse(), Node.browse()

        server_node = self.session_id._server_nodes().filtered(
            lambda node: node.server_id == self.edge_server_id
        )[:1]
        controller_node = self.session_id._controller_nodes().filtered(
            lambda node: node.controller_id == self.controller_id
            and node.parent_id == server_node
        )[:1]
        reader_nodes = self.session_id._reader_nodes().filtered(
            lambda node: node.parent_id == controller_node
        ) if controller_node else self.env["nsp.measurement.device.node"].browse()
        return server_node, controller_node, reader_nodes

    @api.depends(
        "source_scope",
        "session_id",
        "edge_server_id",
        "controller_id",
        "session_id.device_node_ids.parent_id",
        "session_id.device_node_ids.reader_id",
        "session_id.device_node_ids.reader_id.active",
    )
    def _compute_available_reader_ids(self):
        """Expose Readers from the selected Calibration branch or active inventory."""
        active_readers = self.env["nsp.device"].search([("active", "=", True)])
        for wizard in self:
            if wizard.source_scope == "calibration" and wizard.session_id:
                _server, _controller, reader_nodes = wizard._calibration_branch_nodes()
                wizard.available_reader_ids = reader_nodes.mapped("reader_id").filtered(
                    "active"
                )
                continue
            wizard.available_reader_ids = active_readers

    def _allowed_reader_port_pairs(self):
        """Return Reader/Port scope for the selected Calibration branch."""
        self.ensure_one()
        if self.source_scope != "calibration":
            return None
        _server, _controller, reader_nodes = self._calibration_branch_nodes()
        return {
            (node.reader_id.id, int(port.port_no or 0))
            for node in reader_nodes
            for port in node.reader_port_ids
            if node.reader_id and int(port.port_no or 0) > 0
        }

    def _switch_section(self, section):
        self.ensure_one()
        if section not in ("device", "sequence"):
            raise ValidationError(_("Unsupported Lane Setup section."))
        self.active_section = section
        return self._reopen_action()

    def action_show_device(self):
        return self._switch_section("device")

    def action_show_sequence(self):
        return self._switch_section("sequence")

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

    def _ensure_calibration_device_lines(self):
        """Ensure Lane Setup has one transient Reader config per Calibration branch Reader.

        The reusable Tree UI is presentation only.  Saving Lane Setup must never
        depend on whether the hidden x2many was hydrated in the browser.  For a
        Calibration-sourced wizard the Device Configuration membership is locked
        to the selected Server -> Controller branch, so any missing transient line
        can be recreated safely from the persisted Calibration Reader node without
        overwriting Reader settings the operator already edited in this wizard.
        """
        self.ensure_one()
        if self.source_scope != "calibration" or not self.session_id:
            return self.device_line_ids

        _server, _controller, reader_nodes = self._calibration_branch_nodes()
        existing_by_reader = {
            line.reader_id.id: line
            for line in self.device_line_ids
            if line.reader_id
        }
        missing_values = []
        for node in reader_nodes.sorted(lambda item: (item.sequence or 0, item.id)):
            if not node.reader_id or node.reader_id.id in existing_by_reader:
                continue
            missing_values.append({
                "wizard_id": self.id,
                "reader_id": node.reader_id.id,
                "power_dbm": int(node.power_dbm or 0),
                "read_interval_ms": int(node.read_interval_ms or 200),
                "tid_start_address": int(node.tid_addr or 0),
                "tid_length": int(node.tid_len or 4),
            })
        if missing_values:
            self.env["nsp.lane.setup.device.line"].create(missing_values)
            self.invalidate_recordset(["device_line_ids"])
        return self.device_line_ids

    def action_save(self):
        self.ensure_one()
        self._ensure_calibration_device_lines()
        return LaneSetupService(self.env).save(self)

    def action_save_setup(self):
        """Compatibility alias for older NSP 19.x callers."""
        return self.action_save()

    def action_apply_setup(self):
        """Compatibility alias for older NSP 19.x callers."""
        return self.action_save()

    def action_save_draft(self):
        """Compatibility alias; Lane Setup now has one Save action."""
        return self.action_save()


class NspLaneSetupDeviceLine(models.TransientModel):
    _name = "nsp.lane.setup.device.line"
    _description = "Lane Setup Device Configuration"
    _order = "reader_id, id"

    wizard_id = fields.Many2one(
        "nsp.lane.setup.wizard", required=True, ondelete="cascade", index=True
    )
    reader_id = fields.Many2one("nsp.device", string="Reader", required=True)
    reader_name = fields.Char(
        related="reader_id.name", string="Reader Name", readonly=True
    )
    reader_serial_number = fields.Char(
        related="reader_id.serial_number", string="Serial", readonly=True
    )
    reader_status = fields.Selection(
        related="reader_id.status", string="Status", readonly=True
    )
    port_summary = fields.Char(string="Antennas", compute="_compute_port_summary")
    power_dbm = fields.Integer(string="Power (dBm)", required=True, default=30)
    read_interval_ms = fields.Integer(
        string="Read Interval (ms)", required=True, default=200
    )
    tid_start_address = fields.Integer(string="TID Start", required=True, default=0)
    tid_length = fields.Integer(string="TID Length", required=True, default=4)

    @api.depends(
        "wizard_id.sequence_line_ids.reader_id",
        "wizard_id.sequence_line_ids.port_no",
        "reader_id",
    )
    def _compute_port_summary(self):
        for line in self:
            ports = sorted({
                int(point.port_no or 0)
                for point in line.wizard_id.sequence_line_ids
                if point.reader_id == line.reader_id and int(point.port_no or 0) > 0
            })
            line.port_summary = ", ".join("P%s" % port for port in ports)


class NspLaneSetupSequenceLine(models.TransientModel):
    _name = "nsp.lane.setup.sequence.line"
    _description = "Lane Setup Antenna Sequence Point"
    _order = "sequence, id"

    wizard_id = fields.Many2one(
        "nsp.lane.setup.wizard", required=True, ondelete="cascade", index=True
    )
    sequence = fields.Integer(string="#", required=True, default=10)
    reader_id = fields.Many2one("nsp.device", string="Reader", required=True)
    port_no = fields.Integer(string="Antenna", required=True)
    antenna = fields.Char(string="Antenna", compute="_compute_antenna")
    reader_identity = fields.Char(
        string="Reader Identity", compute="_compute_antenna"
    )
    duration_ms = fields.Integer(
        string="Max Duration from Previous (ms)",
        required=True,
        default=0,
        help="Maximum allowed time from the previous Antenna. The first point uses 0 ms and is system-managed.",
    )
    is_first_point = fields.Boolean(string="First Point", compute="_compute_is_first_point")

    @api.depends("wizard_id.sequence_line_ids.sequence")
    def _compute_is_first_point(self):
        for line in self:
            ordered_lines = line.wizard_id.sequence_line_ids.sorted(
                lambda row: (row.sequence or 0, row.id)
            ) if line.wizard_id else self.browse()
            line.is_first_point = bool(ordered_lines and ordered_lines[0] == line)

    @api.depends(
        "reader_id", "reader_id.serial_number", "reader_id.device_code", "port_no"
    )
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
        if self.port_no and not 1 <= int(self.port_no) <= 16:
            return {
                "warning": {
                    "title": _("Invalid Antenna"),
                    "message": _("Antenna/Port must be between 1 and 16."),
                }
            }
        if (
            self.reader_id
            and self.port_no
            and self.wizard_id.source_scope == "calibration"
        ):
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
                            "ports": ", ".join(
                                "P%s" % port for port in allowed_ports
                            ) or _("none"),
                        },
                    }
                }
        return False
