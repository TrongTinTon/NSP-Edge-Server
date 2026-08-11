# -*- coding: utf-8 -*-
import logging
from datetime import timedelta
from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError
from odoo.addons.nsp_core.utils import new_management_code

_logger = logging.getLogger(__name__)


class Device(models.Model):
    _name = "nsp.device"
    _description = "NSP RFID Reader"
    _rec_name = "name"
    _order = "serial_number, device_code, id"

    # Device declaration
    whitelist_id = fields.Many2one(
        "nsp.device.whitelist", string="Device Whitelist", readonly=True, copy=False,
        ondelete="set null", index=True,
    )

    name = fields.Char(string="Reader Name", required=True, default="RFID Reader", index=True)
    serial_number = fields.Char(
        string="Serial",
        required=False,
        copy=False,
        index=True,
        help="Physical Reader serial number. It must be globally unique in NSP.",
    )
    device_code = fields.Char(
        string="Device Code", required=True, readonly=True, copy=False, index=True,
        default=lambda self: new_management_code("DEV"),
    )
    active = fields.Boolean(default=True, index=True)

    # Runtime status reported by the Controller
    status = fields.Selection([
        ("online", "Online"),
        ("offline", "Offline"),
        ("degraded", "Degraded"),
    ], string="Status", required=True, default="offline", index=True)
    last_seen = fields.Datetime(string="Last Seen", readonly=True, copy=False, index=True)
    firmware_version = fields.Char(string="Firmware Version", readonly=True, copy=False)
    runtime_power_dbm = fields.Integer(
        string="Reader Power (dBm)",
        readonly=True,
        copy=False,
        help="Actual power reported by the Controller for the running Reader instance.",
    )
    runtime_read_interval_ms = fields.Integer(
        string="Read Interval ms",
        readonly=True,
        copy=False,
        help="Actual read interval reported by the Controller for the running Reader instance.",
    )
    runtime_last_detection_at = fields.Datetime(
        string="Last RFID Detection",
        readonly=True,
        copy=False,
        index=True,
        help="Latest physical RFID detection reported through Edge status.",
    )
    runtime_last_detection_port_no = fields.Integer(
        string="Last Detection Port",
        readonly=True,
        copy=False,
    )

    # Operation profile sent to Controller. These values are promoted from an
    # approved Measurement Session and are intentionally not edited on Reader UI.
    power_dbm = fields.Integer(
        string="Power (dBm)",
        required=True,
        default=30,
        help="Transmit power applied uniformly to all antenna ports of this Reader.",
    )
    read_interval_ms = fields.Integer(string="Read Interval ms", default=200)
    tid_addr = fields.Integer(string="TID Start Address (Words)", default=2, help="Start offset in 16-bit WORD units.")
    tid_len = fields.Integer(string="TID Length (Words)", default=4, help="Read length in 16-bit WORD units; 1 WORD equals 2 bytes.")

    _sql_constraints = [
        ("serial_number_unique", "unique(serial_number)", "Reader Serial must be unique."),
        ("device_code_unique", "unique(device_code)", "Device Code must be unique."),
        ("reader_power_range", "CHECK(power_dbm >= 0 AND power_dbm <= 40)", "Power must be between 0 and 40 dBm."),
        ("read_interval_positive", "CHECK(read_interval_ms > 0)", "Read Interval must be greater than zero."),
        ("tid_addr_non_negative", "CHECK(tid_addr >= 0)", "TID Start Address (Words) cannot be negative."),
        ("tid_len_positive", "CHECK(tid_len > 0)", "TID Length (Words) must be greater than zero."),
    ]

    @api.model
    def _normalize_serial(self, value):
        return str(value or "").strip().upper()

    @api.model
    def _normalize_code(self, value):
        return str(value or "").strip().upper()

    @api.model
    def _serial_conflict(self, serial, exclude_ids=None):
        normalized = self._normalize_serial(serial)
        if not normalized:
            return self.browse()
        domain = [("serial_number", "=", normalized)]
        if exclude_ids:
            domain.append(("id", "not in", list(exclude_ids)))
        return self.with_context(active_test=False).search(domain, limit=1)

    @api.model
    def _raise_serial_conflict(self, serial, conflict=None):
        normalized = self._normalize_serial(serial)
        if conflict:
            raise ValidationError(_(
                "Reader Serial '%(serial)s' already exists on Reader '%(reader)s'. "
                "Reader Serial must be globally unique."
            ) % {
                "serial": normalized,
                "reader": conflict.display_name,
            })
        raise ValidationError(_(
            "Reader Serial '%s' is entered more than once. Reader Serial must be globally unique."
        ) % normalized)

    @api.onchange("serial_number")
    def _onchange_serial_number_unique(self):
        for reader in self:
            serial = self._normalize_serial(reader.serial_number)
            if not serial:
                continue
            reader.serial_number = serial

            origin_ids = reader._origin.ids if reader._origin else []
            conflict = self._serial_conflict(serial, exclude_ids=origin_ids)
            if conflict:
                self._raise_serial_conflict(serial, conflict)


    @api.model
    def name_search(self, name="", domain=None, operator="ilike", limit=100):
        search_domain = list(domain or [])
        if name:
            search_domain = [
                "|", "|",
                ("name", operator, name),
                ("serial_number", operator, name),
                ("device_code", operator, name),
            ] + search_domain
        records = self.search(search_domain, limit=limit)
        return [(record.id, record.display_name) for record in records]

    @api.model
    def name_create(self, name):
        serial = self._normalize_serial(name)
        if not serial:
            raise UserError(_("Reader Serial is required for Quick Create."))
        record = self.create({
            "name": serial,
            "serial_number": serial,
        })
        return record.id, record.display_name

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        serials = set()
        for source in vals_list:
            vals = dict(source)
            serial = self._normalize_serial(vals.get("serial_number"))
            if serial:
                if serial in serials:
                    self._raise_serial_conflict(serial)
                serials.add(serial)
            vals["serial_number"] = serial or False
            vals["name"] = str(vals.get("name") or serial or "RFID Reader").strip()
            vals["device_code"] = self._normalize_code(
                vals.get("device_code") or new_management_code("DEV")
            )
            prepared.append(vals)

        existing = self.with_context(active_test=False).search([
            ("serial_number", "in", sorted(serials)),
        ], limit=1) if serials else self.browse()
        if existing:
            self._raise_serial_conflict(existing.serial_number, existing)

        return super().create(prepared)

    def write(self, vals):
        values = dict(vals)
        if "serial_number" in values:
            serial = self._normalize_serial(values.get("serial_number"))
            if serial:
                if len(self) > 1:
                    self._raise_serial_conflict(serial)
                conflict = self._serial_conflict(serial, exclude_ids=self.ids)
                if conflict:
                    self._raise_serial_conflict(serial, conflict)
            values["serial_number"] = serial or False
        if "name" in values:
            values["name"] = str(values.get("name") or "").strip() or "RFID Reader"
        if "device_code" in values:
            values["device_code"] = self._normalize_code(values.get("device_code"))
        return super().write(values)

    @api.constrains("serial_number", "device_code")
    def _check_declaration(self):
        for reader in self:
            if not self._normalize_code(reader.device_code):
                raise ValidationError(_("Device Code is required."))

    def _build_config_payload(self):
        """Return technical Reader configuration for the Controller."""
        self.ensure_one()
        return {
            "serial_number": self.serial_number or "",
            "reader_parameters": {
                "power_dbm": int(self.power_dbm or 0),
                "read_interval_ms": int(self.read_interval_ms or 0),
                "tid_start_address": int(self.tid_addr or 0),
                "tid_length": int(self.tid_len or 0),
            },
        }

    def _build_edge_config_payload(self):
        """Return Cloud-to-Edge Reader declaration and technical settings."""
        self.ensure_one()
        payload = self._build_config_payload()
        payload.update({
            "technical_code": self.device_code or "",
            "reader_name": self.name or self.serial_number or "RFID Reader",
        })
        return payload

    @api.model
    def cron_mark_offline_devices(self):
        parameter = self.env["ir.config_parameter"].sudo().get_param(
            "nsp_master_gatekeeper.device_report_timeout_sec",
            "300",
        )
        try:
            timeout_sec = int(parameter or "300")
        except (TypeError, ValueError):
            _logger.warning(
                "Invalid device report timeout %r; using 300 seconds.",
                parameter,
            )
            timeout_sec = 300

        cutoff = fields.Datetime.now() - timedelta(seconds=max(30, timeout_sec))
        # This system cron owns Reader liveness for the complete runtime scope.
        Device = self.sudo().with_context(
            active_test=False,
            tracking_disable=True,
            mail_notrack=True,
        )
        stale_devices = Device.search([
            ("status", "!=", "offline"),
            "|",
            ("last_seen", "=", False),
            ("last_seen", "<", cutoff),
        ])
        stale_devices.write({"status": "offline"})

        return True
