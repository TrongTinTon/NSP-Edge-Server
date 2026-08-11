# -*- coding: utf-8 -*-
import logging

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)


class NspMeasurementSessionReaderScope(models.Model):
    _inherit = "nsp.measurement.session"

    def _allowed_reader_port_pairs(self):
        self.ensure_one()
        return {
            ((line.reader_id.serial_number or "").strip().upper(), int(mapping.port_no or 0))
            for line in self.reader_line_ids
            for mapping in line.reader_port_ids
        }

    def _measurement_line_for_serial(self, serial_number):
        self.ensure_one()
        serial = str(serial_number or "").strip().upper()
        return self.reader_line_ids.filtered(
            lambda line: (line.reader_id.serial_number or "").strip().upper() == serial
        )[:1]

    def _reader_power_for_serial(self, serial_number):
        self.ensure_one()
        line = self._measurement_line_for_serial(serial_number)
        return int(line.reader_power_dbm or 0) if line else 0

    def _reader_interval_for_serial(self, serial_number):
        self.ensure_one()
        line = self._measurement_line_for_serial(serial_number)
        return int(line.read_interval_ms or 0) if line else 0

    def _controller_codes(self):
        self.ensure_one()
        return sorted({
            str(line.controller_id.controller_id or "").strip().upper()
            for line in self.reader_line_ids
            if line.controller_id
        })

    def _edge_server_codes(self):
        self.ensure_one()
        values = set()
        for edge in self.reader_line_ids.mapped("edge_server_id"):
            code = str(edge.edge_server_code or "").strip().upper() if edge else ""
            if code:
                values.add(code)
        return sorted(values)

    def _validate_reader_scope(self):
        for session in self:
            reader_ids = session.reader_line_ids.mapped("reader_id").ids
            if len(reader_ids) != len(set(reader_ids)):
                raise ValidationError(
                    _("A Reader can be selected only once in a Lane Calibration.")
                )
            edge_ids = set(session.reader_line_ids.mapped("edge_server_id").ids)
            if len(edge_ids) > 1:
                raise ValidationError(
                    _("All Reader assemblies in one calibration must use the same Server.")
                )
        return True


class NspMeasurementReaderLine(models.Model):
    """Contextual hardware assembly for one Lane Calibration.

    This line stores the Server, Controller, RFID Reader and Reader Ports used by the calibration session.
    """

    _name = "nsp.measurement.reader.line"
    _description = "NSP Measurement Reader Assembly"
    _order = "session_id, edge_server_id, controller_id, reader_id, id"

    session_id = fields.Many2one(
        "nsp.measurement.session",
        required=False,
        ondelete="cascade",
        index=True,
        help=(
            "Assigned automatically when the Reader Assembly is attached to "
            "a Lane Calibration. It may be temporarily empty while editing "
            "a new, unsaved calibration form."
        ),
    )
    edge_server_id = fields.Many2one(
        "nsp.edge.server", string="Server", required=True,
        ondelete="restrict", index=True,
    )
    controller_id = fields.Many2one(
        "nsp.controller", string="Controller", required=True,
        ondelete="restrict", index=True,
    )
    reader_id = fields.Many2one(
        "nsp.device", string="RFID Reader", required=True,
        ondelete="restrict", index=True,
    )
    edge_server_name = fields.Char(
        related="edge_server_id.name", string="Server Name", readonly=True,
    )
    edge_server_status = fields.Selection(
        related="edge_server_id.status", string="Server Status", readonly=True,
    )
    controller_name = fields.Char(
        related="controller_id.controller_name", string="Controller Name", readonly=True,
    )
    controller_status = fields.Selection(
        related="controller_id.status", string="Controller Status", readonly=True,
    )
    reader_name = fields.Char(related="reader_id.name", string="Reader Name", readonly=True)
    serial_number = fields.Char(related="reader_id.serial_number", readonly=True)
    reader_status = fields.Selection(related="reader_id.status", readonly=True)
    # Contextual calibration snapshot. These values are seeded from the Reader
    # master when the Reader is selected, then belong to this Calibration revision.
    # Editing Lane Calibration must never mutate nsp.device master TID settings.
    reader_tid_addr = fields.Integer(
        string="TID Start Address (Words)", default=0, required=True,
    )
    reader_tid_len = fields.Integer(
        string="TID Length (Words)", default=4, required=True,
    )
    reader_power_dbm = fields.Integer(
        string="Reader Power (dBm)", default=30, required=True,
    )
    read_interval_ms = fields.Integer(
        string="Read Interval ms", default=200, required=True,
        help="Temporary inventory interval applied during this calibration.",
    )
    reader_port_ids = fields.One2many(
        "nsp.measurement.reader.port", "reader_line_id",
        string="Reader Ports", copy=True,
    )
    port_count = fields.Integer(compute="_compute_port_count")
    port_numbers = fields.Char(
        string="Ports",
        compute="_compute_port_count",
        help="Configured physical Reader ports.",
    )

    available_edge_server_ids = fields.Many2many(
        "nsp.edge.server", compute="_compute_available_devices", readonly=True,
    )
    available_controller_ids = fields.Many2many(
        "nsp.controller", compute="_compute_available_devices", readonly=True,
    )
    available_reader_ids = fields.Many2many(
        "nsp.device", compute="_compute_available_devices", readonly=True,
    )

    @api.model
    def default_get(self, fields_list):
        values = super().default_get(fields_list)
        if "session_id" in fields_list and not values.get("session_id"):
            session_id = self.env.context.get("default_session_id")
            if not session_id and self.env.context.get("active_model") == "nsp.measurement.session":
                session_id = self.env.context.get("active_id")
            if session_id:
                values["session_id"] = int(session_id)
        # A new Reader Assembly should be immediately saveable from the
        # Infrastructure Scope popup. Seed Port 1 unless the caller supplied
        # an explicit port collection.
        if "reader_port_ids" in fields_list and not values.get("reader_port_ids"):
            values["reader_port_ids"] = [(0, 0, {"port_no": 1})]
        return values

    def action_open_scope_create(self):
        session_id = self._session_id_from_context()
        session = self.env["nsp.measurement.session"].browse(session_id).exists()
        if not session:
            raise UserError(_("Lane Calibration was not found."))
        session.check_access("write")
        if session.status != "draft":
            raise ValidationError(_("Infrastructure Scope can be edited only while Lane Calibration is Draft."))
        form_view = self.env.ref(
            "nsp_master_gatekeeper.view_nsp_measurement_reader_line_form"
        )
        return {
            "type": "ir.actions.act_window",
            "name": _("New Reader Assembly"),
            "res_model": self._name,
            "view_mode": "form",
            "views": [(form_view.id, "form")],
            "target": "new",
            "context": {
                **dict(self.env.context),
                "active_model": "nsp.measurement.session",
                "active_id": session.id,
                "active_ids": session.ids,
                "default_session_id": session.id,
                "default_reader_port_ids": [(0, 0, {"port_no": 1})],
            },
        }

    @api.model
    def _session_id_from_context(self):
        session_id = self.env.context.get("default_session_id")
        if not session_id and self.env.context.get("active_model") == "nsp.measurement.session":
            session_id = self.env.context.get("active_id")
        return int(session_id) if session_id else False

    _sql_constraints = [
        (
            "measurement_reader_unique", "unique(session_id, reader_id)",
            "An RFID Reader can be selected only once in a Lane Calibration.",
        ),
        (
            "measurement_reader_power_range",
            "CHECK(reader_power_dbm >= 0 AND reader_power_dbm <= 40)",
            "Reader Power must be between 0 and 40 dBm.",
        ),
        (
            "measurement_reader_interval_range",
            "CHECK(read_interval_ms > 0 AND read_interval_ms <= 60000)",
            "Read Interval must be between 1 and 60000 ms.",
        ),
        (
            "measurement_reader_tid_addr_nonnegative",
            "CHECK(reader_tid_addr >= 0)",
            "TID Start Address must be zero or greater.",
        ),
        (
            "measurement_reader_tid_len_positive",
            "CHECK(reader_tid_len > 0)",
            "TID Length must be greater than zero.",
        ),
    ]
    @api.depends("reader_port_ids", "reader_port_ids.port_no")
    def _compute_port_count(self):
        for line in self:
            ports = sorted({int(port.port_no or 0) for port in line.reader_port_ids if int(port.port_no or 0) > 0})
            line.port_count = len(ports)
            line.port_numbers = ", ".join("P%s" % port_no for port_no in ports)

    @api.model
    def _active_whitelisted(self, model_name, type_code):
        return self.env[model_name].search([
            ("active", "=", True),
            ("whitelist_id", "!=", False),
            ("whitelist_id.active", "=", True),
            ("whitelist_id.device_type_code", "=", type_code),
        ])

    @api.depends("edge_server_id", "controller_id", "reader_id")
    def _compute_available_devices(self):
        Edge = self.env["nsp.edge.server"]
        Controller = self.env["nsp.controller"]
        Reader = self.env["nsp.device"]
        edges = self._active_whitelisted("nsp.edge.server", "SERVER")
        controllers = self._active_whitelisted("nsp.controller", "CONTROLLER")
        readers = self._active_whitelisted("nsp.device", "RFID_READER")
        for line in self:
            line.available_edge_server_ids = edges if edges else Edge.browse()
            line.available_controller_ids = controllers if controllers else Controller.browse()
            line.available_reader_ids = readers if readers else Reader.browse()

    @api.model
    def _validate_whitelist_identity(self, record, type_code, label):
        if (
            not record
            or not record.active
            or not record.whitelist_id
            or not record.whitelist_id.active
            or record.whitelist_id.device_type_code != type_code
        ):
            raise ValidationError(
                _("%(label)s must be an active %(type)s from Device Whitelist.")
                % {"label": label, "type": type_code.replace("_", " ").title()}
            )

    def _validate_line_scope(self):
        for line in self:
            self._validate_whitelist_identity(line.edge_server_id, "SERVER", _("Server"))
            self._validate_whitelist_identity(line.controller_id, "CONTROLLER", _("Controller"))
            self._validate_whitelist_identity(line.reader_id, "RFID_READER", _("RFID Reader"))
            if line.reader_power_dbm < 0 or line.reader_power_dbm > 40:
                raise ValidationError(_("Reader Power must be between 0 and 40 dBm."))
            if line.read_interval_ms <= 0 or line.read_interval_ms > 60000:
                raise ValidationError(_("Read Interval must be between 1 and 60000 ms."))
            if line.reader_tid_addr < 0:
                raise ValidationError(_("TID Start Address (Words) cannot be negative."))
            if line.reader_tid_len <= 0:
                raise ValidationError(_("TID Length (Words) must be greater than zero."))
            if not line.reader_port_ids:
                raise ValidationError(_("Select at least one Reader Port for every RFID Reader."))
            for port in line.reader_port_ids:
                port._validate_port()
        return True

    @api.model
    def _ensure_draft_session(self, session):
        if (
            session
            and not self.env.context.get("measurement_sync")
            and session.status != "draft"
        ):
            raise ValidationError(
                _("Device Configuration can be changed only while Lane Calibration is Draft. Create a new Draft revision before changing devices.")
            )
        return True

    def _ensure_lines_editable(self, incoming_session_id=False):
        if self.env.context.get("measurement_sync"):
            return True
        incoming_session = self.env["nsp.measurement.session"].browse(
            int(incoming_session_id or 0)
        ).exists() if incoming_session_id else self.env["nsp.measurement.session"].browse()
        if incoming_session:
            self._ensure_draft_session(incoming_session)
        for line in self:
            self._ensure_draft_session(line.session_id)
        return True

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        context_session_id = self._session_id_from_context()
        for source in vals_list:
            values = dict(source)
            if not values.get("session_id") and context_session_id:
                values["session_id"] = context_session_id
            session = self.env["nsp.measurement.session"].browse(
                int(values.get("session_id") or 0)
            ).exists() if values.get("session_id") else self.env["nsp.measurement.session"].browse()
            self._ensure_draft_session(session)

            reader = self.env["nsp.device"].browse(values.get("reader_id")).exists()
            if reader:
                values.setdefault(
                    "reader_power_dbm",
                    int(reader.runtime_power_dbm or reader.power_dbm or 30),
                )
                values.setdefault(
                    "read_interval_ms",
                    int(reader.runtime_read_interval_ms or reader.read_interval_ms or 200),
                )
                values.setdefault("reader_tid_addr", int(reader.tid_addr or 0))
                values.setdefault("reader_tid_len", int(reader.tid_len or 4))
            values.setdefault("reader_port_ids", [(0, 0, {"port_no": 1})])
            prepared.append(values)
        records = super().create(prepared)
        records._validate_line_scope()
        records.mapped("session_id")._validate_reader_scope()
        return records

    def write(self, vals):
        self._ensure_lines_editable(vals.get("session_id"))
        result = super().write(vals)
        self._validate_line_scope()
        self.mapped("session_id")._validate_reader_scope()
        return result

    def unlink(self):
        sessions = self.mapped("session_id")
        self._ensure_lines_editable()
        result = super().unlink()
        sessions._validate_reader_scope()
        return result

    def action_save_device_configuration(self, values=None, port_numbers=None, identity=None, trace_id=None):
        """Persist one complete Lane Calibration Reader mapping from Device Tree.

        Manual Save is the authoritative persistence boundary for the custom tree.  A
        Reader mapping is not only its RF/TID parameters: Server, Controller and Reader
        identities are part of the same contextual Calibration assembly.  Persist all of
        them atomically so an Edit Reader operation can never remain only in the OWL
        relational cache while the old database mapping survives.
        """
        self.ensure_one()
        self.check_access("write")
        self._ensure_draft_session(self.session_id)

        _logger.info(
            "[NSP MANUAL SAVE][START] trace_id=%s calibration_id=%s calibration_code=%s line_id=%s "
            "current_server_id=%s current_controller_id=%s current_reader_id=%s "
            "incoming_identity=%s incoming_values=%s incoming_ports=%s user_id=%s",
            trace_id or "-",
            self.session_id.id,
            self.session_id.code or self.session_id.display_name,
            self.id,
            self.edge_server_id.id,
            self.controller_id.id,
            self.reader_id.id,
            identity or {},
            values or {},
            port_numbers,
            self.env.user.id,
        )

        source = dict(values or {})
        allowed = {
            "reader_power_dbm",
            "read_interval_ms",
            "reader_tid_addr",
            "reader_tid_len",
        }
        unknown = sorted(set(source) - allowed)
        if unknown:
            raise ValidationError(
                _("Unsupported Reader configuration fields: %s") % ", ".join(unknown)
            )

        identity_source = dict(identity or {})
        allowed_identity = {"edge_server_id", "controller_id", "reader_id"}
        unknown_identity = sorted(set(identity_source) - allowed_identity)
        if unknown_identity:
            raise ValidationError(
                _("Unsupported Reader identity fields: %s") % ", ".join(unknown_identity)
            )

        normalized = {
            "reader_power_dbm": int(source.get("reader_power_dbm", self.reader_power_dbm or 0)),
            "read_interval_ms": int(source.get("read_interval_ms", self.read_interval_ms or 0)),
            "reader_tid_addr": int(source.get("reader_tid_addr", self.reader_tid_addr or 0)),
            "reader_tid_len": int(source.get("reader_tid_len", self.reader_tid_len or 0)),
        }
        for field_name in ("edge_server_id", "controller_id", "reader_id"):
            if field_name in identity_source:
                record_id = int(identity_source.get(field_name) or 0)
                if not record_id:
                    raise ValidationError(_("Server, Controller and Reader are required."))
                normalized[field_name] = record_id

        requested_ports = port_numbers
        if requested_ports is None:
            requested_ports = self.reader_port_ids.mapped("port_no")
        try:
            requested_ports = [int(value) for value in requested_ports]
        except (TypeError, ValueError):
            raise ValidationError(_("Reader Ports must be integer values from 1 to 16."))
        if not requested_ports:
            raise ValidationError(_("Select at least one Reader Port for every RFID Reader."))
        if len(requested_ports) != len(set(requested_ports)):
            raise ValidationError(_("Reader Port must be unique per RFID Reader."))
        if any(port_no < 1 or port_no > 16 for port_no in requested_ports):
            raise ValidationError(_("Reader Port must be an integer from 1 to 16."))
        requested_ports = sorted(requested_ports)

        current_by_no = {int(port.port_no): port for port in self.reader_port_ids}
        commands = []
        for port_no, port in current_by_no.items():
            if port_no not in requested_ports:
                commands.append((2, port.id, 0))
        for port_no in requested_ports:
            if port_no not in current_by_no:
                commands.append((0, 0, {"port_no": port_no}))
        if commands:
            normalized["reader_port_ids"] = commands

        _logger.info(
            "[NSP MANUAL SAVE][WRITE] trace_id=%s line_id=%s normalized=%s requested_ports=%s port_commands=%s",
            trace_id or "-", self.id, normalized, requested_ports, commands,
        )
        try:
            self.write(normalized)
        except Exception:
            _logger.exception(
                "[NSP MANUAL SAVE][ERROR] trace_id=%s write failed calibration_id=%s line_id=%s normalized=%s ports=%s",
                trace_id or "-", self.session_id.id, self.id, normalized, requested_ports,
            )
            raise

        result = {
            "id": self.id,
            "edge_server_id": self.edge_server_id.id,
            "controller_id": self.controller_id.id,
            "reader_id": self.reader_id.id,
            "reader_power_dbm": int(self.reader_power_dbm or 0),
            "read_interval_ms": int(self.read_interval_ms or 0),
            "reader_tid_addr": int(self.reader_tid_addr or 0),
            "reader_tid_len": int(self.reader_tid_len or 0),
            "port_numbers": sorted(int(port.port_no) for port in self.reader_port_ids),
        }
        _logger.info(
            "[NSP MANUAL SAVE][SUCCESS] trace_id=%s calibration_id=%s line_id=%s persisted=%s",
            trace_id or "-", self.session_id.id, self.id, result,
        )
        # Return canonical DB-backed configuration for explicit frontend verification.
        return result

    @api.model
    def action_create_device_configuration(self, session_id, values=None, port_numbers=None, identity=None, trace_id=None):
        """Create a complete Reader mapping directly from Device Tree.

        This is used when the OWL One2many row is still virtual.  Persisting through the
        parent form is intentionally avoided for an existing Lane Calibration because a
        custom field component must not depend on an unrelated invisible One2many field's
        save lifecycle.
        """
        session = self.env["nsp.measurement.session"].browse(int(session_id or 0)).exists()
        _logger.info(
            "[NSP MANUAL SAVE][CREATE START] trace_id=%s calibration_id=%s incoming_identity=%s incoming_values=%s incoming_ports=%s user_id=%s",
            trace_id or "-", int(session_id or 0), identity or {}, values or {}, port_numbers, self.env.user.id,
        )
        if not session:
            raise ValidationError(_("Lane Calibration was not found."))
        session.check_access("write")
        self._ensure_draft_session(session)

        identity_source = dict(identity or {})
        required_identity = ("edge_server_id", "controller_id", "reader_id")
        normalized_identity = {}
        for field_name in required_identity:
            record_id = int(identity_source.get(field_name) or 0)
            if not record_id:
                raise ValidationError(_("Server, Controller and Reader are required."))
            normalized_identity[field_name] = record_id

        source = dict(values or {})
        allowed = {
            "reader_power_dbm",
            "read_interval_ms",
            "reader_tid_addr",
            "reader_tid_len",
        }
        unknown = sorted(set(source) - allowed)
        if unknown:
            raise ValidationError(
                _("Unsupported Reader configuration fields: %s") % ", ".join(unknown)
            )

        requested_ports = [int(value) for value in (port_numbers or [])]
        if not requested_ports:
            raise ValidationError(_("Select at least one Reader Port for every RFID Reader."))
        if len(requested_ports) != len(set(requested_ports)):
            raise ValidationError(_("Reader Port must be unique per RFID Reader."))
        if any(port_no < 1 or port_no > 16 for port_no in requested_ports):
            raise ValidationError(_("Reader Port must be an integer from 1 to 16."))

        create_values = {
            "session_id": session.id,
            **normalized_identity,
            "reader_power_dbm": int(source.get("reader_power_dbm", 30)),
            "read_interval_ms": int(source.get("read_interval_ms", 200)),
            "reader_tid_addr": int(source.get("reader_tid_addr", 0)),
            "reader_tid_len": int(source.get("reader_tid_len", 4)),
            "reader_port_ids": [(0, 0, {"port_no": port_no}) for port_no in sorted(requested_ports)],
        }
        _logger.info(
            "[NSP MANUAL SAVE][CREATE] trace_id=%s calibration_id=%s create_values=%s",
            trace_id or "-", session.id, create_values,
        )
        try:
            line = self.create(create_values)
        except Exception:
            _logger.exception(
                "[NSP MANUAL SAVE][CREATE ERROR] trace_id=%s calibration_id=%s create_values=%s",
                trace_id or "-", session.id, create_values,
            )
            raise
        result = {
            "id": line.id,
            "edge_server_id": line.edge_server_id.id,
            "controller_id": line.controller_id.id,
            "reader_id": line.reader_id.id,
            "reader_power_dbm": int(line.reader_power_dbm or 0),
            "read_interval_ms": int(line.read_interval_ms or 0),
            "reader_tid_addr": int(line.reader_tid_addr or 0),
            "reader_tid_len": int(line.reader_tid_len or 0),
            "port_numbers": sorted(int(port.port_no) for port in line.reader_port_ids),
        }
        _logger.info(
            "[NSP MANUAL SAVE][CREATE SUCCESS] trace_id=%s calibration_id=%s line_id=%s persisted=%s",
            trace_id or "-", session.id, line.id, result,
        )
        return result

    @api.onchange("reader_id")
    def _onchange_reader_id(self):
        for line in self:
            if not line.reader_id:
                continue
            line.reader_power_dbm = int(
                line.reader_id.runtime_power_dbm or line.reader_id.power_dbm or 30
            )
            line.read_interval_ms = int(
                line.reader_id.runtime_read_interval_ms
                or line.reader_id.read_interval_ms
                or 200
            )
            line.reader_tid_addr = int(line.reader_id.tid_addr or 0)
            line.reader_tid_len = int(line.reader_id.tid_len or 4)

    @api.constrains(
        "edge_server_id", "controller_id", "reader_id", "reader_port_ids",
        "reader_power_dbm", "read_interval_ms", "reader_tid_addr",
        "reader_tid_len", "session_id",
    )
    def _check_line_scope(self):
        self._validate_line_scope()

class NspMeasurementReaderPort(models.Model):
    _name = "nsp.measurement.reader.port"
    _description = "NSP Measurement Reader Port"
    _order = "reader_line_id, port_no, id"
    _rec_name = "display_name"

    reader_line_id = fields.Many2one(
        "nsp.measurement.reader.line", required=True,
        ondelete="cascade", index=True,
    )
    session_id = fields.Many2one(
        related="reader_line_id.session_id", store=True, readonly=True, index=True,
    )
    port_no = fields.Integer(
        string="Port", required=True, index=True,
        help="Physical RFID Reader port number. Allowed range: 1 to 16.",
    )
    display_name = fields.Char(compute="_compute_display_name")

    _sql_constraints = [
        (
            "measurement_reader_port_unique", "unique(reader_line_id, port_no)",
            "Reader Port must be unique per RFID Reader.",
        ),
        (
            "measurement_reader_port_range", "CHECK(port_no >= 1 AND port_no <= 16)",
            "Reader Port must be an integer from 1 to 16.",
        ),
    ]

    @api.model
    def default_get(self, fields_list):
        values = super().default_get(fields_list)
        if "port_no" in fields_list and not values.get("port_no"):
            reader_line_id = self.env.context.get("default_reader_line_id")
            if reader_line_id:
                reader_line = self.env["nsp.measurement.reader.line"].browse(
                    int(reader_line_id)
                ).exists()
                if reader_line:
                    next_port = max(reader_line.reader_port_ids.mapped("port_no") or [0]) + 1
                    values["port_no"] = next_port if next_port <= 16 else False
        return values

    @api.model
    def _ensure_ports_editable(self, reader_lines=None):
        if self.env.context.get("measurement_sync"):
            return True
        lines = reader_lines if reader_lines is not None else self.mapped("reader_line_id")
        for line in lines:
            line._ensure_draft_session(line.session_id)
        return True

    @api.model_create_multi
    def create(self, vals_list):
        requested_line_ids = {
            int(values.get("reader_line_id") or 0)
            for values in vals_list
            if int(values.get("reader_line_id") or 0)
        }
        requested_lines = self.env["nsp.measurement.reader.line"].browse(
            sorted(requested_line_ids)
        ).exists()
        self._ensure_ports_editable(requested_lines)
        reader_line_ids = {
            int(values.get("reader_line_id") or 0)
            for values in vals_list
            if int(values.get("reader_line_id") or 0) and not values.get("port_no")
        }
        existing_by_reader = {reader_line_id: [] for reader_line_id in reader_line_ids}
        if reader_line_ids:
            existing = self.search([("reader_line_id", "in", sorted(reader_line_ids))])
            for row in existing:
                existing_by_reader.setdefault(row.reader_line_id.id, []).append(int(row.port_no or 0))
        next_by_reader = {
            reader_line_id: max(port_numbers or [0]) + 1
            for reader_line_id, port_numbers in existing_by_reader.items()
        }
        prepared = []
        for source in vals_list:
            values = dict(source)
            reader_line_id = int(values.get("reader_line_id") or 0)
            if reader_line_id and not values.get("port_no"):
                values["port_no"] = next_by_reader[reader_line_id]
                next_by_reader[reader_line_id] += 1
            prepared.append(values)
        records = super().create(prepared)
        records._validate_port()
        return records

    def write(self, vals):
        self._ensure_ports_editable()
        result = super().write(vals)
        self._validate_port()
        return result

    def unlink(self):
        self._ensure_ports_editable()
        return super().unlink()

    @api.depends("port_no")
    def _compute_display_name(self):
        for record in self:
            record.display_name = _("Port %s") % (record.port_no or "-")

    def _validate_port(self):
        for record in self:
            port_no = int(record.port_no or 0)
            if port_no < 1 or port_no > 16:
                raise ValidationError(_("Reader Port must be an integer from 1 to 16."))
        return True

    @api.constrains("port_no", "reader_line_id")
    def _check_port(self):
        self._validate_port()
