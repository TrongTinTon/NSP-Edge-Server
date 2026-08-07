# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError


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
    serial_number = fields.Char(related="reader_id.serial_number", readonly=True)
    reader_status = fields.Selection(related="reader_id.status", readonly=True)
    reader_tid_addr = fields.Integer(
        related="reader_id.tid_addr", string="TID Start Address (Words)", readonly=False,
    )
    reader_tid_len = fields.Integer(
        related="reader_id.tid_len", string="TID Length (Words)", readonly=False,
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
    ]
    @api.depends("reader_port_ids")
    def _compute_port_count(self):
        for line in self:
            line.port_count = len(line.reader_port_ids)

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

    @api.model_create_multi
    # def create(self, vals_list):
    #     prepared = []
    #     context_session_id = self._session_id_from_context()
    #     for source in vals_list:
    #         values = dict(source)
    #         if not values.get("session_id") and context_session_id:
    #             values["session_id"] = context_session_id
    #         # A Reader Assembly opened from an unsaved Lane Calibration is
    #         # first kept as an x2many child without a database parent id. Odoo
    #         # assigns session_id when the parent form is saved. Do not block
    #         # that standard form workflow.
    #         reader = self.env["nsp.device"].browse(values.get("reader_id")).exists()
    #         if reader:
    #             values.setdefault(
    #                 "reader_power_dbm",
    #                 int(reader.runtime_power_dbm or reader.power_dbm or 30),
    #             )
    #             values.setdefault(
    #                 "read_interval_ms",
    #                 int(reader.runtime_read_interval_ms or reader.read_interval_ms or 200),
    #             )
    #         prepared.append(values)
    #     records = super().create(prepared)
    #     records._validate_line_scope()
    #     return records

    # def write(self, vals):
    #     if not self.env.context.get("measurement_sync"):
    #         protected = self.filtered(
    #             lambda line: line.session_id
    #             and line.session_id.status not in ("draft", "completed")
    #         )
    #         if protected:
    #             raise ValidationError(
    #                 _("Device assembly can be edited only while Draft or after completion before Measure Again.")
    #             )
    #     result = super().write(vals)
    #     self._validate_line_scope()
    #     return result

    def unlink(self):
        if not self.env.context.get("measurement_sync"):
            protected = self.filtered(
                lambda line: line.session_id
                and line.session_id.status not in ("draft", "completed")
            )
            if protected:
                raise ValidationError(
                    _("Calibration device lines can be removed only while Draft or after completion before Measure Again.")
                )
        return super().unlink()

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

    @api.constrains(
        "edge_server_id", "controller_id", "reader_id", "reader_port_ids",
        "reader_power_dbm", "read_interval_ms", "session_id",
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

    @api.model_create_multi
    def create(self, vals_list):
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
