# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class Device(models.Model):
    _name = "nsp.device"
    _description = "NSP RFID Reader"
    _rec_name = "serial_number"
    _order = "controller_id, serial_number, id"

    # Device declaration
    serial_number = fields.Char(string="Serial", required=True, copy=False, index=True)
    device_code = fields.Char(string="Device Code", required=True, index=True)
    model_number = fields.Char(string="Model Number")
    device_vendor = fields.Char(string="Vendor")
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
    device_ip = fields.Char(string="IP Address", readonly=True, copy=False)
    device_port = fields.Integer(string="Port", readonly=True, copy=False)

    # Reader parameters controlled by the server
    power_dbm = fields.Integer(string="Power dBm", default=30)
    read_interval_ms = fields.Integer(string="Read Interval ms", default=200)
    tid_addr = fields.Integer(string="TID Start Address", default=2)
    tid_len = fields.Integer(string="TID Length", default=4)

    antennas = fields.Integer(string="Antennas", compute="_compute_antenna_count")
    antennas_ids = fields.One2many(
        "nsp.device.antenna",
        "device_id",
        string="Antennas",
    )

    _sql_constraints = [
        ("serial_number_unique", "unique(serial_number)", "Reader Serial must be unique."),
        ("device_code_controller_unique", "unique(controller_id, device_code)", "Device Code must be unique per Controller."),
        ("read_interval_positive", "CHECK(read_interval_ms > 0)", "Read Interval must be greater than zero."),
        ("tid_addr_non_negative", "CHECK(tid_addr >= 0)", "TID Start Address cannot be negative."),
        ("tid_len_positive", "CHECK(tid_len > 0)", "TID Length must be greater than zero."),
    ]

    @api.model
    def _normalize_serial(self, value):
        return str(value or "").strip().upper()

    @api.model
    def _normalize_code(self, value):
        return str(value or "").strip().upper()

    def _whitelist_record(self):
        self.ensure_one()
        serial = self._normalize_serial(self.serial_number)
        if not serial:
            return self.env["nsp.device.whitelist"].browse()
        return self.env["nsp.device.whitelist"].sudo().search([("serial_number", "=", serial)], limit=1)

    def _is_whitelisted(self):
        self.ensure_one()
        return bool(self._whitelist_record())

    def _notify_not_whitelisted(self, details=None):
        if "nsp.notification" not in self.env.registry.models:
            return True
        for reader in self:
            if reader._is_whitelisted():
                continue
            self.env["nsp.notification"].sudo().notify_device_not_whitelisted(
                reader.serial_number,
                reader.controller_id.controller_id,
                details=details or {
                    "model_number": reader.model_number,
                    "vendor": reader.device_vendor,
                    "device_type": "rfid_reader",
                },
            )
        return True

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for source in vals_list:
            vals = dict(source)
            serial = self._normalize_serial(vals.get("serial_number"))
            vals["serial_number"] = serial
            vals["device_code"] = self._normalize_code(vals.get("device_code") or serial)
            vals["model_number"] = str(vals.get("model_number") or "").strip() or False
            vals["device_vendor"] = str(vals.get("device_vendor") or "").strip() or False
            prepared.append(vals)
        records = super().create(prepared)
        records._notify_not_whitelisted()
        return records

    def write(self, vals):
        values = dict(vals)
        if "serial_number" in values:
            values["serial_number"] = self._normalize_serial(values.get("serial_number"))
        if "device_code" in values:
            values["device_code"] = self._normalize_code(values.get("device_code"))
        if "model_number" in values:
            values["model_number"] = str(values.get("model_number") or "").strip() or False
        if "device_vendor" in values:
            values["device_vendor"] = str(values.get("device_vendor") or "").strip() or False
        result = super().write(values)
        if set(values) & {"serial_number", "controller_id", "model_number", "device_vendor"}:
            self._notify_not_whitelisted()
        return result

    @api.constrains("serial_number", "device_code")
    def _check_declaration(self):
        for reader in self:
            if not self._normalize_serial(reader.serial_number):
                raise ValidationError(_("Reader Serial is required."))
            if not self._normalize_code(reader.device_code):
                raise ValidationError(_("Device Code is required."))

    @api.depends("antennas_ids")
    def _compute_antenna_count(self):
        for record in self:
            record.antennas = len(record.antennas_ids)

    def _build_config_payload(self):
        """Return only technical configuration owned by the Controller."""
        self.ensure_one()
        antennas = [
            {
                "antenna_no": int(antenna.antenna_id or 0),
                "physical_antenna": antenna.physical_antenna or "",
            }
            for antenna in self.antennas_ids.sorted(key=lambda rec: (rec.antenna_id or 0, rec.id))
        ]
        return {
            "serial_number": self.serial_number or "",
            "device_code": self.device_code or self.serial_number or "",
            "model_number": self.model_number or "",
            "vendor": self.device_vendor or "",
            "connection": {
                "protocol": "tcp",
                "ip_address": self.device_ip or "",
                "port": int(self.device_port or 0),
            },
            "reader_parameters": {
                "power_dbm": int(self.power_dbm or 0),
                "read_interval_ms": int(self.read_interval_ms or 0),
                "tid_start_address": int(self.tid_addr or 0),
                "tid_length": int(self.tid_len or 0),
            },
            "antennas": antennas,
        }

    @api.model
    def cron_mark_offline_devices(self):
        try:
            timeout_sec = int(self.env["ir.config_parameter"].sudo().get_param(
                "nsp_gatekeeper.device_report_timeout_sec", "300"
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
