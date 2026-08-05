# -*- coding: utf-8 -*-
import json

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError
from odoo.addons.nsp_core.utils import new_management_code


class Device(models.Model):
    _name = "nsp.device"
    _description = "NSP RFID Reader"
    _rec_name = "name"
    _order = "controller_id, serial_number, id"

    # Device declaration
    whitelist_id = fields.Many2one(
        "nsp.device.whitelist", string="Device Whitelist", readonly=True, copy=False,
        ondelete="set null", index=True,
    )

    name = fields.Char(string="Reader Name", required=True, default="RFID Reader", index=True)
    serial_number = fields.Char(
        string="Serial",
        required=True,
        copy=False,
        index=True,
        help="Physical Reader serial number. It must be globally unique across all Edge Servers and Controllers.",
    )
    device_code = fields.Char(
        string="Device Code", required=True, readonly=True, copy=False, index=True,
        default=lambda self: new_management_code("DEV"),
    )
    controller_id = fields.Many2one(
        "nsp.controller",
        string="Controller",
        required=True,
        ondelete="restrict",
        index=True,
        help="Controller that directly manages this Reader.",
    )

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
    runtime_ports_json = fields.Text(
        string="Runtime Ports",
        readonly=True,
        copy=False,
        help="Last Reader Port list reported by the Controller.",
    )
    active = fields.Boolean(default=True, index=True)
    cloud_removed = fields.Boolean(default=False, readonly=True, index=True, copy=False)

    # Physical connection inventory. The Odoo field widget groups options as Wired / Wireless.
    connection_type = fields.Selection([
        ("usb", "USB"),
        ("rs232", "RS-232"),
        ("rs485", "RS-485"),
        ("ethernet", "Ethernet (RJ45)"),
        ("wiegand", "Wiegand"),
        ("bluetooth", "Bluetooth"),
        ("wifi", "Wi-Fi"),
        ("cellular", "4G/5G"),
    ], string="Physical Connection", index=True)

    # Operation profile sent to Controller. These values are promoted from an
    # approved Measurement Session and are intentionally not edited on Reader UI.
    power_dbm = fields.Integer(
        string="Power (dBm)",
        required=True,
        default=30,
        help="Transmit power applied uniformly to all configured Reader ports.",
    )
    read_interval_ms = fields.Integer(string="Read Interval ms", default=200)
    tid_addr = fields.Integer(string="TID Start Address (Words)", default=2, help="Start offset in 16-bit WORD units.")
    tid_len = fields.Integer(string="TID Length (Words)", default=4, help="Read length in 16-bit WORD units; 1 WORD equals 2 bytes.")


    _sql_constraints = [
        ("serial_number_unique", "unique(serial_number)", "Reader Serial must be unique."),
        ("device_code_controller_unique", "unique(controller_id, device_code)", "Device Code must be unique per Controller."),
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
                "Reader Serial '%(serial)s' already exists on Reader '%(reader)s' "
                "under Controller '%(controller)s'. Reader Serial must be globally unique."
            ) % {
                "serial": normalized,
                "reader": conflict.display_name,
                "controller": conflict.controller_id.display_name,
            })
        raise ValidationError(_(
            "Reader Serial '%s' is entered more than once. Reader Serial must be globally unique."
        ) % normalized)

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        serials = set()
        for source in vals_list:
            vals = dict(source)
            serial = self._normalize_serial(vals.get("serial_number"))
            if not serial:
                raise ValidationError(_("Reader Serial is required."))
            if serial in serials:
                self._raise_serial_conflict(serial)
            serials.add(serial)
            vals["serial_number"] = serial
            vals["name"] = str(vals.get("name") or serial or "RFID Reader").strip()
            vals["device_code"] = self._normalize_code(
                vals.get("device_code") or new_management_code("DEV")
            )
            prepared.append(vals)

        existing = self.with_context(active_test=False).search([
            ("serial_number", "in", sorted(serials)),
        ], limit=1)
        if existing:
            self._raise_serial_conflict(existing.serial_number, existing)
        return super().create(prepared)

    def write(self, vals):
        values = dict(vals)
        if "serial_number" in values:
            serial = self._normalize_serial(values.get("serial_number"))
            if not serial:
                raise ValidationError(_("Reader Serial is required."))
            if len(self) > 1:
                self._raise_serial_conflict(serial)
            conflict = self._serial_conflict(serial, exclude_ids=self.ids)
            if conflict:
                self._raise_serial_conflict(serial, conflict)
            values["serial_number"] = serial
        if "name" in values:
            values["name"] = str(values.get("name") or "").strip() or "RFID Reader"
        if "device_code" in values:
            values["device_code"] = self._normalize_code(values.get("device_code"))
        return super().write(values)

    @api.constrains("serial_number", "device_code")
    def _check_declaration(self):
        for reader in self:
            if not self._normalize_serial(reader.serial_number):
                raise ValidationError(_("Reader Serial is required."))
            if not self._normalize_code(reader.device_code):
                raise ValidationError(_("Device Code is required."))


    def _runtime_port_numbers(self):
        self.ensure_one()
        port_numbers = set()
        Timeline = self.env["nsp.parking.lane.timeline"].sudo()
        if "reader_id" in Timeline._fields:
            rows = Timeline.search([
                ("reader_id", "=", self.id),
                ("lane_id.active", "=", True),
                ("lane_id.parking_area_id.state", "in", ["operational", "maintenance"]),
            ])
            port_numbers.update(int(value) for value in rows.mapped("port_no") if int(value or 0) > 0)
        ReaderLine = self.env["nsp.measurement.reader.line"].sudo()
        if "reader_port_ids" in ReaderLine._fields:
            lines = ReaderLine.search([
                ("reader_id", "=", self.id),
                ("session_id.status", "in", ["ready", "running"]),
            ])
            port_numbers.update(
                int(value)
                for value in lines.mapped("reader_port_ids.port_no")
                if int(value or 0) > 0
            )
        return sorted(port_numbers)

    def _reported_port_numbers(self):
        self.ensure_one()
        if self.runtime_ports_json not in (False, None, ""):
            try:
                values = json.loads(self.runtime_ports_json)
            except (TypeError, ValueError):
                values = None
            if isinstance(values, list):
                normalized = set()
                for value in values:
                    if isinstance(value, bool):
                        continue
                    try:
                        port_no = int(value)
                    except (TypeError, ValueError):
                        continue
                    if 1 <= port_no <= 16:
                        normalized.add(port_no)
                return sorted(normalized)
        return self._runtime_port_numbers()

    def _build_config_payload(self):
        self.ensure_one()
        return {
            "serial_number": self.serial_number or "",
            "reader_parameters": {
                "power_dbm": int(self.power_dbm or 0),
                "read_interval_ms": int(self.read_interval_ms or 0),
                "tid_start_address": int(self.tid_addr or 0),
                "tid_length": int(self.tid_len or 0),
            },
            "ports": [
                {"port_no": port_no}
                for port_no in self._runtime_port_numbers()
            ],
        }

    def _build_edge_config_payload(self):
        self.ensure_one()
        payload = self._build_config_payload()
        payload.update({
            "technical_code": self.device_code or "",
            "reader_name": self.name or self.serial_number or "RFID Reader",
            "physical_connection": self.connection_type or False,
        })
        return payload

    @api.model
    def cron_mark_offline_devices(self):
        try:
            timeout_sec = int(self.env["ir.config_parameter"].sudo().get_param(
                "nsp_business_gatekeeper.device_report_timeout_sec", "300"
            ) or "300")
        except Exception:
            timeout_sec = 300
        timeout_sec = max(30, timeout_sec)
        self.env.cr.execute("""
            UPDATE nsp_device
               SET status = 'offline'
             WHERE COALESCE(status, 'offline') != 'offline'
               AND (last_seen IS NULL OR last_seen < (NOW() AT TIME ZONE 'UTC') - (%s || ' seconds')::interval)
        """, (str(timeout_sec),))
        self.env.cr.execute("""
            UPDATE nsp_device d
               SET status = 'offline'
              FROM nsp_controller c
             WHERE d.controller_id = c.id
               AND COALESCE(d.status, 'offline') != 'offline'
               AND COALESCE(c.status, 'offline') = 'offline'
        """)
        return True
