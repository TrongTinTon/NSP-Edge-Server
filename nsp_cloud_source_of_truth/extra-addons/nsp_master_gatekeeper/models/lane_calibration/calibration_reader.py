# -*- coding: utf-8 -*-
"""Lane Calibration device topology.

Server, Controller and Reader remain independent master identities.  A Lane
Calibration owns contextual ``nsp.measurement.device.node`` rows.  The only
relationship between devices in a calibration is ``parent_id`` on those rows.
Drafts may contain incomplete branches (for example Server without Controller or
Controller without Reader), but every existing Controller must already belong to a Server
and every existing Reader must already belong to a Controller. Release validates branch completeness.
"""

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


DEVICE_TYPES = (
    ("server", "Server"),
    ("controller", "Controller"),
    ("reader", "Reader"),
)


class NspMeasurementSessionDeviceTopology(models.Model):
    _inherit = "nsp.measurement.session"

    def _device_nodes(self, device_type=False):
        self.ensure_one()
        nodes = self.device_node_ids
        return nodes.filtered(lambda node: node.device_type == device_type) if device_type else nodes

    def _server_nodes(self):
        self.ensure_one()
        return self._device_nodes("server")

    def _controller_nodes(self):
        self.ensure_one()
        return self._device_nodes("controller")

    def _reader_nodes(self):
        self.ensure_one()
        return self._device_nodes("reader")

    def _measurement_node_for_serial(self, serial_number):
        self.ensure_one()
        serial = str(serial_number or "").strip().upper()
        return self._reader_nodes().filtered(
            lambda node: (node.reader_id.serial_number or "").strip().upper() == serial
        )[:1]

    def _allowed_reader_port_pairs(self):
        self.ensure_one()
        return {
            ((node.reader_id.serial_number or "").strip().upper(), int(port.port_no or 0))
            for node in self._reader_nodes()
            for port in node.reader_port_ids
        }

    def _reader_power_for_serial(self, serial_number):
        node = self._measurement_node_for_serial(serial_number)
        return int(node.power_dbm or 0) if node else 0

    def _reader_interval_for_serial(self, serial_number):
        node = self._measurement_node_for_serial(serial_number)
        return int(node.read_interval_ms or 0) if node else 0

    def _controller_codes(self):
        self.ensure_one()
        return sorted({
            str(node.controller_id.controller_id or "").strip().upper()
            for node in self._controller_nodes()
            if node.controller_id
        })

    def _edge_server_codes(self):
        self.ensure_one()
        return sorted({
            str(node.server_id.edge_server_code or "").strip().upper()
            for node in self._server_nodes()
            if node.server_id
        })

    def _validate_device_node_scope(self):
        """Validate identity uniqueness; node-level hierarchy is enforced immediately."""
        for session in self:
            seen = {"server": set(), "controller": set(), "reader": set()}
            for node in session.device_node_ids:
                master = node.device_record
                if not master:
                    continue
                if master.id in seen[node.device_type]:
                    raise ValidationError(
                        _("%(type)s '%(device)s' can be added only once to a Lane Calibration.")
                        % {"type": node.device_type.title(), "device": master.display_name}
                    )
                seen[node.device_type].add(master.id)
        return True


class NspMeasurementDeviceNode(models.Model):
    """One contextual device node in a Lane Calibration Tree.

    ``parent_id`` is contextual topology only.  It never mutates or duplicates
    ownership on the Server/Controller/Reader master records.
    """

    _name = "nsp.measurement.device.node"
    _description = "NSP Lane Calibration Device Node"
    _order = "session_id, sequence, id"
    _rec_name = "device_name"
    _parent_name = "parent_id"
    _parent_store = True

    session_id = fields.Many2one(
        "nsp.measurement.session",
        required=True,
        ondelete="cascade",
        index=True,
    )
    device_type = fields.Selection(DEVICE_TYPES, required=True, index=True)

    server_id = fields.Many2one(
        "nsp.edge.server",
        string="Server",
        ondelete="restrict",
        index=True,
    )
    controller_id = fields.Many2one(
        "nsp.controller",
        string="Controller",
        ondelete="restrict",
        index=True,
    )
    reader_id = fields.Many2one(
        "nsp.device",
        string="RFID Reader",
        ondelete="restrict",
        index=True,
    )

    parent_id = fields.Many2one(
        "nsp.measurement.device.node",
        string="Parent Node",
        ondelete="cascade",
        index=True,
    )
    parent_path = fields.Char(index=True)
    child_ids = fields.One2many(
        "nsp.measurement.device.node",
        "parent_id",
        string="Child Nodes",
    )
    sequence = fields.Integer(default=10, index=True)

    # Reader configuration belongs to this Lane Calibration node, not Reader master.
    power_dbm = fields.Integer(string="Reader Power (dBm)", default=30)
    read_interval_ms = fields.Integer(string="Read Interval ms", default=200)
    tid_addr = fields.Integer(string="TID Start Address (Words)", default=0)
    tid_len = fields.Integer(string="TID Length (Words)", default=4)
    reader_port_ids = fields.One2many(
        "nsp.measurement.reader.port",
        "reader_node_id",
        string="Reader Ports",
        copy=True,
    )

    # Display-only fields make the hidden x2many payload sufficient for the OWL Tree.
    device_name = fields.Char(compute="_compute_device_meta")
    device_status = fields.Char(compute="_compute_device_meta")
    serial_number = fields.Char(compute="_compute_device_meta")
    port_numbers = fields.Char(compute="_compute_port_numbers")
    port_count = fields.Integer(compute="_compute_port_numbers")

    _sql_constraints = [
        (
            "measurement_node_server_unique",
            "unique(session_id, server_id)",
            "A Server can be added only once to a Lane Calibration.",
        ),
        (
            "measurement_node_controller_unique",
            "unique(session_id, controller_id)",
            "A Controller can be added only once to a Lane Calibration.",
        ),
        (
            "measurement_node_reader_unique",
            "unique(session_id, reader_id)",
            "A Reader can be added only once to a Lane Calibration.",
        ),
        (
            "measurement_node_power_range",
            "CHECK(power_dbm >= 0 AND power_dbm <= 40)",
            "Reader Power must be between 0 and 40 dBm.",
        ),
        (
            "measurement_node_interval_range",
            "CHECK(read_interval_ms > 0 AND read_interval_ms <= 60000)",
            "Read Interval must be between 1 and 60000 ms.",
        ),
        (
            "measurement_node_tid_addr_nonnegative",
            "CHECK(tid_addr >= 0)",
            "TID Start Address must be zero or greater.",
        ),
        (
            "measurement_node_tid_len_positive",
            "CHECK(tid_len > 0)",
            "TID Length must be greater than zero.",
        ),
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
        "device_type",
        "server_id.name",
        "server_id.status",
        "controller_id.controller_name",
        "controller_id.status",
        "reader_id.name",
        "reader_id.serial_number",
        "reader_id.status",
    )
    def _compute_device_meta(self):
        for node in self:
            if node.device_type == "server" and node.server_id:
                node.device_name = node.server_id.name or "Server"
                node.device_status = node.server_id.status or ""
                node.serial_number = ""
            elif node.device_type == "controller" and node.controller_id:
                node.device_name = node.controller_id.controller_name or "Controller"
                node.device_status = node.controller_id.status or ""
                node.serial_number = ""
            elif node.device_type == "reader" and node.reader_id:
                node.device_name = node.reader_id.name or node.reader_id.serial_number or "RFID Reader"
                node.device_status = node.reader_id.status or ""
                node.serial_number = node.reader_id.serial_number or ""
            else:
                node.device_name = ""
                node.device_status = ""
                node.serial_number = ""

    @api.depends("reader_port_ids", "reader_port_ids.port_no")
    def _compute_port_numbers(self):
        for node in self:
            ports = sorted({
                int(port.port_no or 0)
                for port in node.reader_port_ids
                if int(port.port_no or 0) > 0
            })
            node.port_count = len(ports)
            node.port_numbers = ", ".join("P%s" % port_no for port_no in ports)

    @api.model
    def _ensure_draft_session(self, session):
        if (
            session
            and not self.env.context.get("measurement_sync")
            and session.status != "draft"
        ):
            raise ValidationError(
                _("Device Configuration can be changed only while Lane Calibration is Draft.")
            )
        return True

    def _ensure_editable(self, incoming_session_id=False):
        if self.env.context.get("measurement_sync"):
            return True
        if incoming_session_id:
            session = self.env["nsp.measurement.session"].browse(
                int(incoming_session_id)
            ).exists()
            self._ensure_draft_session(session)
        for node in self:
            self._ensure_draft_session(node.session_id)
        return True

    @api.model
    def _validate_master(self, device_type, master):
        expected = {
            "server": "SERVER",
            "controller": "CONTROLLER",
            "reader": "RFID_READER",
        }.get(device_type)
        if not expected or not master:
            raise ValidationError(_("Select a valid device for this Device Tree node."))
        if (
            not master.active
            or not master.whitelist_id
            or not master.whitelist_id.active
            or master.whitelist_id.device_type_code != expected
        ):
            raise ValidationError(
                _("%(type)s must be an active device from Device Whitelist.")
                % {"type": device_type.title()}
            )

    def _validate_node_integrity(self):
        """Validate identity and mandatory parent hierarchy; branch completeness is Release-only."""
        for node in self:
            selected = {
                "server": bool(node.server_id),
                "controller": bool(node.controller_id),
                "reader": bool(node.reader_id),
            }
            if not selected.get(node.device_type):
                raise ValidationError(_("Select the %(type)s for this node.") % {
                    "type": (node.device_type or "device").title(),
                })
            if sum(bool(value) for value in selected.values()) != 1:
                raise ValidationError(
                    _("A Device Tree node must reference exactly one Server, Controller, or Reader.")
                )
            self._validate_master(node.device_type, node.device_record)
            if node.parent_id:
                if node.parent_id == node:
                    raise ValidationError(_("A Device Tree node cannot be its own parent."))
                if node.parent_id.session_id != node.session_id:
                    raise ValidationError(_("Parent Node must belong to the same Lane Calibration."))
            if node.device_type == "server" and node.parent_id:
                raise ValidationError(_("Server node must be a Device Tree root."))
            if node.device_type == "controller" and (
                not node.parent_id or node.parent_id.device_type != "server"
            ):
                raise ValidationError(_("Controller node must belong to a Server node."))
            if node.device_type == "reader" and (
                not node.parent_id or node.parent_id.device_type != "controller"
            ):
                raise ValidationError(_("Reader node must belong to a Controller node."))
            if node.device_type == "reader":
                for port in node.reader_port_ids:
                    port._validate_port()
        if not self._check_recursion():
            raise ValidationError(_("Device Tree cannot contain a circular parent relationship."))
        return True

    @api.model
    def _reader_configuration_defaults(self, reader):
        return {
            "power_dbm": int(reader.runtime_power_dbm or reader.power_dbm or 30),
            "read_interval_ms": int(
                reader.runtime_read_interval_ms or reader.read_interval_ms or 200
            ),
            "tid_addr": int(reader.tid_addr or 0),
            "tid_len": int(reader.tid_len or 4),
        }

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for source in vals_list:
            values = dict(source)
            session = self.env["nsp.measurement.session"].browse(
                int(values.get("session_id") or 0)
            ).exists()
            self._ensure_draft_session(session)

            device_type = values.get("device_type")
            if device_type == "reader" and values.get("reader_id"):
                reader = self.env["nsp.device"].browse(int(values["reader_id"])).exists()
                if reader:
                    for field_name, default_value in self._reader_configuration_defaults(reader).items():
                        values.setdefault(field_name, default_value)
            prepared.append(values)
        records = super().create(prepared)
        records._validate_node_integrity()
        records.mapped("session_id")._validate_device_node_scope()
        return records

    def write(self, vals):
        self._ensure_editable(vals.get("session_id"))
        values = dict(vals)
        if values.get("reader_id"):
            reader = self.env["nsp.device"].browse(int(values["reader_id"])).exists()
            if reader:
                for field_name, default_value in self._reader_configuration_defaults(reader).items():
                    values.setdefault(field_name, default_value)
        result = super().write(values)
        self._validate_node_integrity()
        self.mapped("session_id")._validate_device_node_scope()
        return result

    def unlink(self):
        sessions = self.mapped("session_id")
        self._ensure_editable()
        result = super().unlink()
        sessions._validate_device_node_scope()
        return result

    @api.onchange("reader_id")
    def _onchange_reader_id(self):
        for node in self.filtered(lambda item: item.device_type == "reader" and item.reader_id):
            for field_name, value in self._reader_configuration_defaults(node.reader_id).items():
                node[field_name] = value

    @api.constrains(
        "session_id",
        "device_type",
        "server_id",
        "controller_id",
        "reader_id",
        "parent_id",
        "power_dbm",
        "read_interval_ms",
        "tid_addr",
        "tid_len",
        "reader_port_ids",
    )
    def _check_node_integrity(self):
        self._validate_node_integrity()


class NspMeasurementReaderPort(models.Model):
    _name = "nsp.measurement.reader.port"
    _description = "NSP Lane Calibration Reader Port"
    _order = "reader_node_id, port_no, id"
    _rec_name = "display_name"

    # Not DB-required to permit an in-place upgrade from the legacy reader_line_id
    # column. New application writes are validated and always require reader_node_id.
    reader_node_id = fields.Many2one(
        "nsp.measurement.device.node",
        string="Reader Node",
        ondelete="cascade",
        index=True,
    )
    session_id = fields.Many2one(
        related="reader_node_id.session_id",
        store=True,
        readonly=True,
        index=True,
    )
    port_no = fields.Integer(
        string="Port",
        required=True,
        index=True,
        help="Physical RFID Reader port number. Allowed range: 1 to 16.",
    )
    sequence = fields.Integer(default=10)
    display_name = fields.Char(compute="_compute_display_name")

    _sql_constraints = [
        (
            "measurement_reader_node_port_unique",
            "unique(reader_node_id, port_no)",
            "Reader Port must be unique per RFID Reader.",
        ),
        (
            "measurement_reader_port_range",
            "CHECK(port_no >= 1 AND port_no <= 16)",
            "Reader Port must be an integer from 1 to 16.",
        ),
    ]

    def _ensure_editable(self, reader_nodes=None):
        if self.env.context.get("measurement_sync"):
            return True
        nodes = reader_nodes if reader_nodes is not None else self.mapped("reader_node_id")
        for node in nodes:
            node._ensure_draft_session(node.session_id)
        return True

    @api.model_create_multi
    def create(self, vals_list):
        node_ids = {
            int(values.get("reader_node_id") or 0)
            for values in vals_list
            if int(values.get("reader_node_id") or 0)
        }
        nodes = self.env["nsp.measurement.device.node"].browse(sorted(node_ids)).exists()
        self._ensure_editable(nodes)
        prepared = []
        for source in vals_list:
            values = dict(source)
            node = self.env["nsp.measurement.device.node"].browse(
                int(values.get("reader_node_id") or 0)
            ).exists()
            if not node or node.device_type != "reader":
                raise ValidationError(_("Reader Port must belong to a Reader node."))
            if not values.get("port_no"):
                used = node.reader_port_ids.mapped("port_no")
                next_port = max(used or [0]) + 1
                values["port_no"] = next_port
            prepared.append(values)
        records = super().create(prepared)
        records._validate_port()
        return records

    def write(self, vals):
        self._ensure_editable()
        result = super().write(vals)
        self._validate_port()
        return result

    def unlink(self):
        self._ensure_editable()
        return super().unlink()

    @api.depends("port_no")
    def _compute_display_name(self):
        for record in self:
            record.display_name = _("Port %s") % (record.port_no or "-")

    def _validate_port(self):
        for record in self:
            if not record.reader_node_id:
                raise ValidationError(_("Reader Port must belong to a Reader node."))
            if record.reader_node_id.device_type != "reader":
                raise ValidationError(_("Reader Port can be attached only to a Reader node."))
            port_no = int(record.port_no or 0)
            if port_no < 1 or port_no > 16:
                raise ValidationError(_("Reader Port must be an integer from 1 to 16."))
        return True

    @api.constrains("port_no", "reader_node_id")
    def _check_port(self):
        self._validate_port()
