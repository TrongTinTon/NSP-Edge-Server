# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError
from odoo.addons.nsp_core.utils import new_management_code
from ..services.raw_rfid_tag import normalize_raw_tid


def _new_measurement_code():
    return new_management_code("MSR")


class NspMeasurementSession(models.Model):
    """Measurement plan shared by Cloud, Edge and one-or-more Controllers.

    The Session owns one raw calibration tag and a list of Reader lines.
    Reader ownership determines Controller scope; therefore Controller is not stored
    again on the Session. Each Edge receives only Reader lines belonging to it and
    each physical Controller pulls only its own Reader subset.
    """

    _name = "nsp.measurement.session"
    _description = "NSP Lane Calibration"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _rec_name = "measurement_code"
    _order = "create_date desc, id desc"

    measurement_code = fields.Char(
        required=True,
        readonly=True,
        copy=False,
        index=True,
        default=lambda self: _new_measurement_code(),
    )
    target_line_ids = fields.One2many(
        "nsp.measurement.target.line",
        "session_id",
        string="Calibration Tag",
        copy=True,
    )
    device_node_ids = fields.One2many(
        "nsp.measurement.device.node",
        "session_id",
        string="Device Topology",
        copy=True,
    )
    device_tree_anchor = fields.Boolean(
        string="NSP Device Tree", compute="_compute_device_tree_anchor",
    )
    calibration_tid = fields.Char(
        string="Calibration Tag", compute="_compute_calibration_tid", readonly=True,
    )
    revision = fields.Integer(
        string="Revision",
        required=True,
        default=1,
        readonly=True,
        copy=False,
        index=True,
    )
    started_at = fields.Datetime(readonly=True, copy=False)
    ended_at = fields.Datetime(readonly=True, copy=False)
    status = fields.Selection(
        [
            ("draft", "Draft"),
            ("ready", "Ready"),
            ("running", "Running"),
            ("completed", "Completed"),
            ("applied", "Configured"),
            ("failed", "Failed"),
            ("cancelled", "Cancelled"),
        ],
        required=True,
        default="draft",
        index=True,
        tracking=True,
    )
    event_ids = fields.One2many(
        "nsp.measurement.event",
        "session_id",
        string="Measurement Observations",
        readonly=True,
    )

    _sql_constraints = [
        (
            "measurement_code_unique",
            "unique(measurement_code)",
            "Measurement Code must be unique.",
        ),
        (
            "measurement_revision_positive",
            "CHECK(revision > 0)",
            "Measurement Revision must be greater than zero.",
        ),
    ]

    @api.depends("device_node_ids", "device_node_ids.device_type")
    def _compute_device_tree_anchor(self):
        for session in self:
            session.device_tree_anchor = True

    @api.depends("target_line_ids.tid")
    def _compute_calibration_tid(self):
        for session in self:
            session.calibration_tid = session.target_line_ids[:1].tid or ""

    def _sanitize_target_commands(self, commands):
        """Normalize the single raw Calibration Tag without runtime assignment lookup."""
        if not commands:
            return commands
        Target = self.env["nsp.measurement.target.line"]
        clear_all = any(
            isinstance(command, (list, tuple)) and command and command[0] == 5
            for command in commands
        )
        removed_ids = {
            int(command[1])
            for command in commands
            if isinstance(command, (list, tuple))
            and len(command) > 1
            and command[0] in (2, 3)
            and command[1]
        }
        existing = self.mapped("target_line_ids") if self and not clear_all else Target
        existing = existing.filtered(lambda line: line.id not in removed_ids)
        seen_tids = set(existing.mapped("tid"))
        resulting_count = len(existing)
        cleaned = []
        for command in commands:
            if not isinstance(command, (list, tuple)) or not command:
                cleaned.append(command)
                continue
            operation = command[0]
            if operation == 0 and len(command) >= 3:
                values = dict(command[2] or {})
                tid = Target._normalize_tid(values.get("tid"))
                if not tid:
                    continue
                if tid in seen_tids:
                    raise ValidationError(_("The same raw TID can be used only once."))
                resulting_count += 1
                if resulting_count > 1:
                    raise ValidationError(_("Lane Calibration accepts exactly one raw RFID Tag."))
                values["tid"] = tid
                cleaned.append((0, 0, values))
                seen_tids.add(tid)
                continue
            if operation == 1 and len(command) >= 3:
                current = Target.browse(int(command[1] or 0)).exists()
                values = dict(command[2] or {})
                if "tid" in values:
                    seen_tids.discard(current.tid if current else "")
                    tid = Target._normalize_tid(values.get("tid"))
                    if not tid:
                        raise ValidationError(_("Raw TID is required."))
                    if tid in seen_tids:
                        raise ValidationError(_("The same raw TID can be used only once."))
                    values["tid"] = tid
                    seen_tids.add(tid)
                cleaned.append((1, command[1], values))
                continue
            cleaned.append(command)
        return cleaned

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for source in vals_list:
            vals = dict(source)
            if "target_line_ids" in vals:
                vals["target_line_ids"] = self._sanitize_target_commands(vals.get("target_line_ids"))
            vals["measurement_code"] = str(
                vals.get("measurement_code") or _new_measurement_code()
            ).strip().upper()
            vals["revision"] = max(int(vals.get("revision") or 1), 1)
            if not self.env.context.get("measurement_sync"):
                vals["status"] = "draft"
            prepared.append(vals)
        records = super().create(prepared)
        records._validate_measurement_scope()
        return records

    def write(self, vals):
        values = dict(vals)
        if "target_line_ids" in values:
            values["target_line_ids"] = self._sanitize_target_commands(values.get("target_line_ids"))
        configuration_fields = {
            "measurement_code", "target_line_ids", "device_node_ids",
        }
        if configuration_fields.intersection(values) and not self.env.context.get("measurement_sync"):
            protected = self.filtered(lambda session: session.status not in ("draft", "completed"))
            if protected:
                raise ValidationError(_(
                    "Measurement configuration can be edited only while Draft or after completion before Measure Again."
                ))
        if "measurement_code" in values:
            values["measurement_code"] = str(values.get("measurement_code") or "").strip().upper()
        result = super().write(values)
        if {"target_line_ids", "device_node_ids"}.intersection(values):
            self._validate_measurement_scope()
        return result

    @api.constrains("device_node_ids", "target_line_ids")
    def _check_scope_constraint(self):
        self._validate_measurement_scope()

    def _validate_measurement_scope(self):
        for session in self:
            seen = {"server": set(), "controller": set(), "reader": set()}
            for node in session.device_node_ids:
                record = node.device_record
                if not record:
                    continue
                if record.id in seen[node.device_type]:
                    raise ValidationError(_(
                        "%(type)s '%(device)s' can be selected only once in a Lane Calibration."
                    ) % {"type": node.device_type.title(), "device": record.display_name})
                seen[node.device_type].add(record.id)
            if len(session.target_line_ids) > 1:
                raise ValidationError(_("Lane Calibration accepts exactly one raw RFID Tag."))
            if session.target_line_ids.filtered(lambda line: not line.tid):
                raise ValidationError(_("Raw TID is required for Lane Calibration."))
        return True

    def _require_ready_configuration(self):
        self.ensure_one()
        if len(self.target_line_ids) != 1:
            raise ValidationError(_("Lane Calibration requires exactly one raw Calibration Tag."))
        servers = self._server_nodes()
        controllers = self._controller_nodes()
        readers = self._reader_nodes()
        missing = []
        if not servers:
            missing.append(_("Server"))
        if not controllers:
            missing.append(_("Controller"))
        if not readers:
            missing.append(_("Reader"))
        if missing:
            raise ValidationError(_("Missing Lane Calibration configuration: %s") % ", ".join(missing))
        invalid_controllers = controllers.filtered(
            lambda node: not node.parent_id or node.parent_id.device_type != "server"
        )
        invalid_readers = readers.filtered(
            lambda node: not node.parent_id or node.parent_id.device_type != "controller"
        )
        if invalid_controllers or invalid_readers:
            raise ValidationError(_("Lane Calibration Device Tree is incomplete."))
        missing_ports = readers.filtered(lambda node: not node.reader_port_ids)
        if missing_ports:
            raise ValidationError(_("Select at least one Reader Port for each RFID Reader."))
        self._validate_measurement_scope()
        return True

    def _allowed_target_tids(self):
        """Return the single raw TID allowed for the current calibration revision."""
        self.ensure_one()
        return {line.tid for line in self.target_line_ids if line.tid}

    def _device_nodes(self, device_type=False):
        self.ensure_one()
        nodes = self.device_node_ids
        return nodes.filtered(lambda node: node.device_type == device_type) if device_type else nodes

    def _server_nodes(self):
        return self._device_nodes("server")

    def _controller_nodes(self):
        return self._device_nodes("controller")

    def _reader_nodes(self):
        return self._device_nodes("reader")

    def _allowed_reader_port_pairs(self):
        self.ensure_one()
        return {
            ((node.reader_id.serial_number or "").strip().upper(), int(port.port_no or 0))
            for node in self._reader_nodes()
            for port in node.reader_port_ids
        }

    def _measurement_node_for_serial(self, serial_number):
        self.ensure_one()
        serial = str(serial_number or "").strip().upper()
        return self._reader_nodes().filtered(
            lambda node: (node.reader_id.serial_number or "").strip().upper() == serial
        )[:1]

    def _reader_power_for_serial(self, serial_number):
        node = self._measurement_node_for_serial(serial_number)
        return int(node.power_dbm or 0) if node else 0

    def _reader_interval_for_serial(self, serial_number):
        node = self._measurement_node_for_serial(serial_number)
        return int(node.read_interval_ms or 0) if node else 0

    @api.model
    def cron_cleanup_expired_measurements(self):
        value = self.env["ir.config_parameter"].sudo().get_param(
            "nsp_business_gatekeeper.measurement_retention_days", "7"
        )
        try:
            retention_days = max(int(value), 1)
        except (TypeError, ValueError):
            retention_days = 7
        cutoff = fields.Datetime.now() - timedelta(days=retention_days)
        events = self.env["nsp.measurement.event"].sudo().search(
            [
                ("session_id.status", "in", ["completed", "applied", "failed", "cancelled"]),
                ("read_at", "<", cutoff),
            ],
            limit=5000,
        )
        count = len(events)
        if events and "nsp.sync.record" in self.env.registry.models:
            self.env["nsp.sync.record"].sudo().search([
                ("record_model", "=", "nsp.measurement.event"),
                ("record_key", "in", events.mapped("event_uid")),
            ]).unlink()
        events.unlink()

        # Let PostgreSQL test emptiness instead of prefetching event_ids for every
        # stale Session in Python. Keep the batch bounded like the Event cleanup.
        stale_empty_sessions = self.sudo().search([
            ("status", "in", ["completed", "applied", "failed", "cancelled"]),
            ("ended_at", "!=", False),
            ("ended_at", "<", cutoff),
            ("event_ids", "=", False),
        ], limit=1000)
        if stale_empty_sessions:
            stale_empty_sessions.with_context(measurement_sync=True).unlink()
        return count


class NspMeasurementTargetLine(models.Model):
    """One arbitrary raw RFID tag used only as the Lane Calibration probe."""

    _name = "nsp.measurement.target.line"
    _description = "NSP Lane Calibration Raw Tag"
    _order = "session_id, id"

    session_id = fields.Many2one(
        "nsp.measurement.session",
        ondelete="cascade",
        index=True,
    )
    tid = fields.Char(
        string="Raw TID",
        required=True,
        index=True,
        help=(
            "Arbitrary raw RFID TID used for calibration. It is not resolved through "
            "RFID runtime assignment and is not mapped to a Vehicle or User."
        ),
    )
    _sql_constraints = [
        (
            "measurement_target_tid_unique",
            "unique(session_id, tid)",
            "This raw TID is already selected in the Lane Calibration.",
        ),
    ]

    @api.model
    def _normalize_tid(self, value):
        try:
            return normalize_raw_tid(value)
        except ValueError as exc:
            raise ValidationError(_("Raw TID must contain hexadecimal characters only.")) from exc

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for source in vals_list:
            values = dict(source)
            values["tid"] = self._normalize_tid(values.get("tid"))
            if not values["tid"]:
                raise ValidationError(_("Raw TID is required."))
            prepared.append(values)
        return super().create(prepared)

    def write(self, vals):
        values = dict(vals)
        if "tid" in values:
            values["tid"] = self._normalize_tid(values.get("tid"))
            if not values["tid"]:
                raise ValidationError(_("Raw TID is required."))
        result = super().write(values)
        self.mapped("session_id")._validate_measurement_scope()
        return result

    @api.constrains("tid", "session_id")
    def _check_raw_tag(self):
        if self.filtered(lambda line: not line.tid):
            raise ValidationError(_("Raw TID is required."))
        self.mapped("session_id")._validate_measurement_scope()


class NspMeasurementDeviceNode(models.Model):
    """Edge runtime projection of one released Lane Calibration topology node."""

    _name = "nsp.measurement.device.node"
    _description = "NSP Lane Calibration Device Node"
    _order = "session_id, sequence, id"
    _rec_name = "device_name"
    _parent_name = "parent_id"
    _parent_store = True

    session_id = fields.Many2one(
        "nsp.measurement.session", required=True, ondelete="cascade", index=True,
    )
    source_node_id = fields.Char(
        string="Cloud Node ID", required=True, copy=False, index=True,
        help="Stable node identifier from the released Cloud topology snapshot.",
    )
    device_type = fields.Selection([
        ("server", "Server"),
        ("controller", "Controller"),
        ("reader", "Reader"),
    ], required=True, index=True)
    server_id = fields.Many2one("nsp.edge.server", ondelete="restrict", index=True)
    controller_id = fields.Many2one("nsp.controller", ondelete="restrict", index=True)
    reader_id = fields.Many2one("nsp.device", ondelete="restrict", index=True)
    parent_id = fields.Many2one(
        "nsp.measurement.device.node", ondelete="cascade", index=True,
    )
    parent_path = fields.Char(index=True)
    child_ids = fields.One2many("nsp.measurement.device.node", "parent_id")
    sequence = fields.Integer(default=10, index=True)

    power_dbm = fields.Integer(string="Reader Power (dBm)", default=30)
    read_interval_ms = fields.Integer(string="Read Interval ms", default=200)
    tid_addr = fields.Integer(string="TID Start Address (Words)", default=0)
    tid_len = fields.Integer(string="TID Length (Words)", default=4)
    reader_port_ids = fields.One2many(
        "nsp.measurement.reader.port", "reader_node_id", string="Reader Ports", copy=True,
    )

    device_name = fields.Char(compute="_compute_device_meta")
    device_status = fields.Char(compute="_compute_device_meta")
    serial_number = fields.Char(compute="_compute_device_meta")
    port_numbers = fields.Char(compute="_compute_port_numbers")

    _sql_constraints = [
        ("measurement_source_node_unique", "unique(session_id, source_node_id)", "Cloud Node ID must be unique per Lane Calibration."),
        ("measurement_node_server_unique", "unique(session_id, server_id)", "A Server can appear only once in a Lane Calibration."),
        ("measurement_node_controller_unique", "unique(session_id, controller_id)", "A Controller can appear only once in a Lane Calibration."),
        ("measurement_node_reader_unique", "unique(session_id, reader_id)", "A Reader can appear only once in a Lane Calibration."),
        ("measurement_node_power_range", "CHECK(power_dbm >= 0 AND power_dbm <= 40)", "Reader Power must be between 0 and 40 dBm."),
        ("measurement_node_interval_range", "CHECK(read_interval_ms > 0 AND read_interval_ms <= 60000)", "Read Interval must be between 1 and 60000 ms."),
        ("measurement_node_tid_addr_nonnegative", "CHECK(tid_addr >= 0)", "TID Start Address cannot be negative."),
        ("measurement_node_tid_len_positive", "CHECK(tid_len > 0)", "TID Length must be greater than zero."),
    ]

    @property
    def device_record(self):
        self.ensure_one()
        if self.device_type == "server":
            return self.server_id
        if self.device_type == "controller":
            return self.controller_id
        if self.device_type == "reader":
            return self.reader_id
        return self.env["nsp.device"].browse()

    @api.depends(
        "device_type", "server_id.name", "server_id.status",
        "controller_id.controller_name", "controller_id.status",
        "reader_id.name", "reader_id.serial_number", "reader_id.status",
    )
    def _compute_device_meta(self):
        for node in self:
            record = node.device_record
            if node.device_type == "server" and record:
                node.device_name = record.name or "Server"
                node.device_status = record.status or ""
                node.serial_number = ""
            elif node.device_type == "controller" and record:
                node.device_name = record.controller_name or "Controller"
                node.device_status = record.status or ""
                node.serial_number = ""
            elif node.device_type == "reader" and record:
                node.device_name = record.name or record.serial_number or "RFID Reader"
                node.device_status = record.status or ""
                node.serial_number = record.serial_number or ""
            else:
                node.device_name = ""
                node.device_status = ""
                node.serial_number = ""

    @api.depends("reader_port_ids", "reader_port_ids.port_no")
    def _compute_port_numbers(self):
        for node in self:
            ports = sorted({int(port.port_no or 0) for port in node.reader_port_ids if int(port.port_no or 0) > 0})
            node.port_numbers = ", ".join("P%s" % port_no for port_no in ports)

    def _validate_node(self):
        for node in self:
            selected = {
                "server": bool(node.server_id),
                "controller": bool(node.controller_id),
                "reader": bool(node.reader_id),
            }
            if not selected.get(node.device_type) or sum(bool(value) for value in selected.values()) != 1:
                raise ValidationError(_("A Device Tree node must reference exactly one matching device."))
            if node.parent_id and node.parent_id.session_id != node.session_id:
                raise ValidationError(_("Parent Node must belong to the same Lane Calibration."))
            if node.device_type == "server" and node.parent_id:
                raise ValidationError(_("Server node must be a Tree root."))
            if node.device_type == "controller" and (not node.parent_id or node.parent_id.device_type != "server"):
                raise ValidationError(_("Controller node must belong to a Server node."))
            if node.device_type == "reader" and (not node.parent_id or node.parent_id.device_type != "controller"):
                raise ValidationError(_("Reader node must belong to a Controller node."))
            if node.device_type == "reader":
                for port in node.reader_port_ids:
                    port._validate_port()
        if not self._check_recursion():
            raise ValidationError(_("Device Tree cannot contain a circular parent relationship."))
        return True

    @api.constrains(
        "session_id", "source_node_id", "device_type", "server_id", "controller_id",
        "reader_id", "parent_id", "power_dbm", "read_interval_ms", "tid_addr", "tid_len",
    )
    def _check_node(self):
        self._validate_node()


class NspMeasurementReaderPort(models.Model):
    _name = "nsp.measurement.reader.port"
    _description = "NSP Measurement Reader Port"
    _order = "reader_node_id, sequence, port_no, id"
    _rec_name = "display_name"

    reader_node_id = fields.Many2one(
        "nsp.measurement.device.node", required=False, ondelete="cascade", index=True,
    )
    session_id = fields.Many2one(
        related="reader_node_id.session_id", store=True, readonly=True, index=True,
    )
    port_no = fields.Integer(string="Port", required=True, index=True)
    sequence = fields.Integer(default=10, index=True)
    display_name = fields.Char(compute="_compute_display_name")

    _sql_constraints = [
        ("measurement_reader_port_unique", "unique(reader_node_id, port_no)", "Reader Port must be unique per RFID Reader."),
        ("measurement_reader_port_range", "CHECK(port_no >= 1 AND port_no <= 16)", "Reader Port must be an integer from 1 to 16."),
    ]

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

    @api.constrains("port_no", "reader_node_id")
    def _check_port(self):
        for record in self:
            if not record.reader_node_id:
                raise ValidationError(_("Reader Port must belong to a Reader node."))
        self._validate_port()


class NspMeasurementEvent(models.Model):
    _name = "nsp.measurement.event"
    _description = "NSP Measurement Observation"
    _rec_name = "event_uid"
    _order = "read_at desc, id desc"

    event_uid = fields.Char(required=True, copy=False, index=True)
    session_id = fields.Many2one(
        "nsp.measurement.session",
        required=True,
        ondelete="cascade",
        index=True,
    )
    revision = fields.Integer(required=True, default=1, index=True)
    serial_number = fields.Char(required=True, index=True)
    port_no = fields.Integer(required=True, index=True)
    tid = fields.Char(required=True, index=True)
    read_at = fields.Datetime(required=True, index=True)
    read_at_ms = fields.Integer(string="Millisecond", required=True, default=0)
    rssi_dbm = fields.Float()
    power_dbm = fields.Integer(string="Reader Power (dBm)")
    read_interval_ms = fields.Integer(string="Read Interval ms", required=True, default=200)
    timeline_timestamp = fields.Char(
        string="Timestamp", compute="_compute_timeline_display", readonly=True,
    )
    timeline_reader = fields.Char(
        string="Reader", compute="_compute_timeline_display", readonly=True,
    )
    timeline_duration_ms = fields.Float(
        string="Duration (ms)", compute="_compute_timeline_display", digits=(16, 3), readonly=True,
    )

    @api.depends(
        "session_id", "revision", "read_at", "read_at_ms", "serial_number",
        "session_id.device_node_ids.reader_id.name",
        "session_id.device_node_ids.reader_id.serial_number",
    )
    def _compute_timeline_display(self):
        for event in self:
            event.timeline_timestamp = ""
            event.timeline_reader = event.serial_number or ""
            event.timeline_duration_ms = 0.0

        persisted = self.filtered(lambda event: event.id and event.session_id)
        if not persisted:
            return
        session_ids = persisted.mapped("session_id").ids
        requested_pairs = {
            (event.session_id.id, int(event.revision or 1)) for event in persisted
        }
        reader_name_by_key = {}
        for session in persisted.mapped("session_id"):
            for node in session._reader_nodes():
                serial = str(node.reader_id.serial_number or "").strip().upper()
                if serial:
                    reader_name_by_key[(session.id, serial)] = (
                        node.reader_id.name or node.reader_id.serial_number or serial
                    )
        all_events = self.search(
            [("session_id", "in", session_ids)],
            order="session_id, revision, read_at asc, read_at_ms asc, id asc",
        )
        previous_seconds = {}
        duration_by_id = {}
        for event in all_events:
            pair = (event.session_id.id, int(event.revision or 1))
            if pair not in requested_pairs:
                continue
            observed_at = fields.Datetime.to_datetime(event.read_at)
            seconds = (
                observed_at.timestamp() + (int(event.read_at_ms or 0) / 1000.0)
                if observed_at else 0.0
            )
            previous = previous_seconds.get(pair)
            duration_by_id[event.id] = (
                max((seconds - previous) * 1000.0, 0.0)
                if previous is not None else 0.0
            )
            previous_seconds[pair] = seconds
        for event in persisted:
            base = fields.Datetime.to_string(event.read_at) if event.read_at else ""
            event.timeline_timestamp = (
                "%s.%03d" % (base, int(event.read_at_ms or 0)) if base else ""
            )
            serial = str(event.serial_number or "").strip().upper()
            event.timeline_reader = reader_name_by_key.get(
                (event.session_id.id, serial), event.serial_number or ""
            )
            event.timeline_duration_ms = duration_by_id.get(event.id, 0.0)

    _sql_constraints = [
        ("measurement_event_uid_unique", "unique(event_uid)", "Measurement Event UID must be unique."),
        ("measurement_event_port_positive", "CHECK(port_no > 0)", "Reader Port must be greater than zero."),
        ("measurement_event_revision_positive", "CHECK(revision > 0)", "Measurement Revision must be greater than zero."),
        ("measurement_event_ms_range", "CHECK(read_at_ms >= 0 AND read_at_ms <= 999)", "Measurement millisecond must be between 0 and 999."),
        ("measurement_event_read_interval_range", "CHECK(read_interval_ms > 0 AND read_interval_ms <= 60000)", "Read Interval must be between 1 and 60000 ms."),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for source in vals_list:
            vals = dict(source)
            vals["event_uid"] = str(vals.get("event_uid") or "").strip()
            vals["serial_number"] = str(vals.get("serial_number") or "").strip().upper()
            try:
                vals["tid"] = normalize_raw_tid(vals.get("tid"))
            except ValueError as exc:
                raise ValidationError(_("Measurement TID must contain hexadecimal characters only.")) from exc
            vals["revision"] = max(int(vals.get("revision") or 1), 1)
            vals["read_at_ms"] = max(0, min(int(vals.get("read_at_ms") or 0), 999))
            vals["read_interval_ms"] = max(1, min(int(vals.get("read_interval_ms") or 200), 60000))
            prepared.append(vals)
        return super().create(prepared)

    def init(self):
        self.env.cr.execute(
            """
            CREATE INDEX IF NOT EXISTS nsp_measurement_event_session_revision_read_idx
                ON nsp_measurement_event (session_id, revision, read_at, read_at_ms, id)
            """
        )
        self.env.cr.execute(
            """
            CREATE INDEX IF NOT EXISTS nsp_measurement_event_session_revision_tid_idx
                ON nsp_measurement_event (session_id, revision, tid, read_at, id)
            """
        )

    @api.constrains("session_id", "serial_number", "port_no", "tid")
    def _check_event_scope(self):
        for event in self:
            session = event.session_id
            key = (event.serial_number, int(event.port_no or 0))
            if key not in session._allowed_reader_port_pairs():
                raise ValidationError(_("Measurement observation Reader Port is not part of the Lane Calibration."))
            if event.tid not in session._allowed_target_tids():
                raise ValidationError(_("Only the active Calibration Tag may be stored in this Lane Calibration."))
