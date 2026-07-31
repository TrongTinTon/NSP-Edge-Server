# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError
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
        required=False,
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
        required=False,
        ondelete="restrict",
        index=True,
        help="Controller that directly manages this Reader.",
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
        help="Transmit power applied uniformly to all antenna ports of this Reader.",
    )
    read_interval_ms = fields.Integer(string="Read Interval ms", default=200)
    tid_addr = fields.Integer(string="TID Start Address (Words)", default=2, help="Start offset in 16-bit WORD units.")
    tid_len = fields.Integer(string="TID Length (Words)", default=4, help="Read length in 16-bit WORD units; 1 WORD equals 2 bytes.")

    antennas = fields.Integer(string="Antennas", compute="_compute_antenna_count")
    antennas_ids = fields.One2many(
        "nsp.device.antenna",
        "device_id",
        string="Antennas",
    )
    antenna_numbers = fields.Char(
        string="Antenna Nos.",
        compute="_compute_antenna_numbers",
        inverse="_inverse_antenna_numbers",
        help="Comma-separated antenna numbers or ranges, for example: 1,2,3,4 or 1-4.",
    )

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

            controller = reader.controller_id
            if controller:
                for sibling in controller.device_ids:
                    if sibling == reader:
                        continue
                    if self._normalize_serial(sibling.serial_number) == serial:
                        self._raise_serial_conflict(serial)

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

        controller = self.env["nsp.controller"]
        controller_id = self.env.context.get("default_controller_id")
        if controller_id:
            controller = controller.browse(int(controller_id)).exists()
        else:
            controller_domain = [("active", "=", True)]
            edge_id = self.env.context.get("default_edge_server_id")
            if edge_id:
                controller_domain.append(("edge_server_id", "=", int(edge_id)))
            candidates = controller.search(controller_domain, limit=2)
            if len(candidates) == 1:
                controller = candidates

        if len(controller) != 1:
            raise UserError(_(
                "Select a Controller first, or use Create and Edit... to create the Reader with its Controller and Serial."
            ))

        record = self.create({
            "name": serial,
            "serial_number": serial,
            "controller_id": controller.id,
        })
        return record.id, record.display_name

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        antenna_inputs = []
        serials = set()
        for source in vals_list:
            vals = dict(source)
            antenna_inputs.append(vals.pop("antenna_numbers", None))
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

        records = super().create(prepared)
        for record, antenna_input in zip(records, antenna_inputs):
            if antenna_input is not None:
                record._apply_antenna_numbers(antenna_input)
        return records

    def write(self, vals):
        values = dict(vals)
        antenna_input = values.pop("antenna_numbers", None)
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
        result = super().write(values)
        if antenna_input is not None:
            for record in self:
                record._apply_antenna_numbers(antenna_input)
        return result

    @api.constrains("serial_number", "device_code")
    def _check_declaration(self):
        for reader in self:
            if not self._normalize_code(reader.device_code):
                raise ValidationError(_("Device Code is required."))

    @api.depends("antennas_ids")
    def _compute_antenna_count(self):
        for record in self:
            record.antennas = len(record.antennas_ids)

    @api.depends("antennas_ids.antenna_no")
    def _compute_antenna_numbers(self):
        for record in self:
            numbers = sorted(set(record.antennas_ids.mapped("antenna_no")))
            record.antenna_numbers = ",".join(str(number) for number in numbers)

    @api.model
    def _parse_antenna_numbers(self, value):
        """Parse compact Antenna input without storing a duplicate representation.

        Accepted examples: ``1,2,3,4``, ``1 2 3 4``, ``1-4`` and
        ``1,3-5``. The physical Antenna rows remain the source of truth.
        """
        raw = str(value or "").strip()
        if not raw:
            return []

        normalized = raw.replace(";", ",").replace(" ", ",")
        numbers = set()
        for token in (part.strip() for part in normalized.split(",")):
            if not token:
                continue
            if "-" in token:
                pieces = [part.strip() for part in token.split("-", 1)]
                if len(pieces) != 2 or not all(piece.isdigit() for piece in pieces):
                    raise ValidationError(_(
                        "Invalid Antenna range '%s'. Use values such as 1,2,3,4 or 1-4."
                    ) % token)
                start, end = (int(piece) for piece in pieces)
                if start <= 0 or end <= 0 or end < start:
                    raise ValidationError(_(
                        "Invalid Antenna range '%s'. Antenna numbers must be positive and ascending."
                    ) % token)
                numbers.update(range(start, end + 1))
            else:
                if not token.isdigit() or int(token) <= 0:
                    raise ValidationError(_(
                        "Invalid Antenna number '%s'. Use positive whole numbers."
                    ) % token)
                numbers.add(int(token))

        if len(numbers) > 64:
            raise ValidationError(_("A Reader cannot declare more than 64 Antennas."))
        return sorted(numbers)

    def _inverse_antenna_numbers(self):
        for record in self:
            record._apply_antenna_numbers(record.antenna_numbers)

    def _apply_antenna_numbers(self, value):
        """Reconcile physical Antenna rows from compact inline Reader input."""
        Antenna = self.env["nsp.device.antenna"]
        for record in self:
            record.ensure_one()
            desired = set(record._parse_antenna_numbers(value))
            existing_by_number = {
                int(antenna.antenna_no): antenna
                for antenna in record.antennas_ids
            }

            stale = record.antennas_ids.filtered(
                lambda antenna: int(antenna.antenna_no) not in desired
            )
            if stale:
                stale.unlink()

            missing = sorted(desired - set(existing_by_number))
            if missing:
                Antenna.create([
                    {"device_id": record.id, "antenna_no": number}
                    for number in missing
                ])
        return True

    def _antenna_config_payload(self, include_identity=False):
        self.ensure_one()
        antennas = self.antennas_ids
        if "active" in antennas._fields:
            antennas = antennas.filtered("active")
        if "cloud_removed" in antennas._fields:
            antennas = antennas.filtered(lambda antenna: not antenna.cloud_removed)
        if "whitelist_id" in antennas._fields:
            antennas = antennas.filtered(
                lambda antenna: antenna.whitelist_id and antenna.whitelist_id.active
            )
        result = []
        for antenna in antennas.sorted(key=lambda item: (item.antenna_no, item.id)):
            item = {"antenna_no": int(antenna.antenna_no)}
            if include_identity:
                item.update({
                    "technical_code": antenna.technical_code or "",
                    "serial_number": antenna.serial_number or False,
                    "name": antenna.whitelist_id.name if antenna.whitelist_id else antenna.display_name,
                })
            result.append(item)
        return result

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
            "antennas": self._antenna_config_payload(include_identity=False),
        }

    def _build_edge_config_payload(self):
        """Return Cloud-to-Edge Reader declaration and technical settings."""
        self.ensure_one()
        payload = self._build_config_payload()
        payload.update({
            "technical_code": self.device_code or "",
            "reader_name": self.name or self.serial_number or "RFID Reader",
            "physical_connection": self.connection_type or False,
            "antennas": self._antenna_config_payload(include_identity=True),
        })
        return payload

    @api.model
    def cron_mark_offline_devices(self):
        try:
            timeout_sec = int(self.env["ir.config_parameter"].sudo().get_param(
                "nsp_master_gatekeeper.device_report_timeout_sec", "300"
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
