# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError
from odoo.addons.nsp_core.utils import new_management_code


VALID_DEVICE_TYPE_CODES = {"SERVER", "CONTROLLER", "RFID_READER", "ANTENNA"}

DEVICE_CODE_PREFIX = {
    "SERVER": "EDGE",
    "CONTROLLER": "CTRL",
    "RFID_READER": "DEV",
    "ANTENNA": "ANT",
}


class DeviceWhitelist(models.Model):
    """Authoritative inventory for every physical/runtime Gatekeeper device.

    Device Whitelist is the only UI where Server, Controller, RFID Reader and
    Antenna records are created. Runtime models are maintained automatically so
    Parking Layout and Reader Calibration only select existing devices.
    """

    _name = "nsp.device.whitelist"
    _description = "NSP Device Whitelist"
    _inherit = ["image.mixin"]
    _rec_name = "display_name"
    _order = "device_type_id, name, technical_code, id"

    name = fields.Char(string="Device Name", readonly=True, copy=False, index=True)
    display_name = fields.Char(compute="_compute_display_name", store=True)
    active = fields.Boolean(default=True, index=True)

    device_type_id = fields.Many2one(
        "nsp.device.type",
        string="Device Type",
        required=True,
        ondelete="restrict",
        index=True,
    )
    device_type_code = fields.Char(
        related="device_type_id.code", string="Device Type Code", store=True, readonly=True
    )
    device_type_badge = fields.Selection(
        [
            ("SERVER", "Server"),
            ("CONTROLLER", "Controller"),
            ("RFID_READER", "RFID Reader"),
            ("ANTENNA", "Antenna"),
        ],
        string="Device Type",
        compute="_compute_device_type_badge",
        readonly=True,
    )
    technical_code = fields.Char(
        string="Technical Code",
        readonly=True,
        copy=False,
        index=True,
        help="Stable identity used for Cloud/Edge synchronization.",
    )
    serial_number = fields.Char(
        string="Serial Number",
        index=True,
        copy=False,
        help="Required for RFID Reader. Optional for Server, Controller and Antenna.",
    )
    parent_id = fields.Many2one(
        "nsp.device.whitelist",
        string="Parent Device",
        ondelete="restrict",
        index=True,
    )
    child_ids = fields.One2many(
        "nsp.device.whitelist", "parent_id", string="Connected Devices"
    )
    allowed_parent_ids = fields.Many2many(
        "nsp.device.whitelist", compute="_compute_allowed_parent_ids"
    )
    antenna_no = fields.Integer(
        string="Antenna No.",
        help="Required only for Antenna and unique within its parent RFID Reader.",
    )

    model_id = fields.Many2one(
        "nsp.reference.model", string="Model", ondelete="set null", index=True
    )
    vendor_id = fields.Many2one(
        "nsp.reference.vendor", string="Vendor", ondelete="set null", index=True
    )
    connection_type = fields.Selection(
        [
            ("usb", "USB"),
            ("rs232", "RS-232"),
            ("rs485", "RS-485"),
            ("ethernet", "Ethernet (RJ45)"),
            ("wiegand", "Wiegand"),
            ("bluetooth", "Bluetooth"),
            ("wifi", "Wi-Fi"),
            ("cellular", "4G/5G"),
        ],
        string="Physical Connection",
        index=True,
    )
    tid_addr = fields.Integer(
        string="TID Start Address (Words)", default=0,
        help="Reader-only setting. One WORD equals 2 bytes.",
    )
    tid_len = fields.Integer(
        string="TID Length (Words)", default=6,
        help="Reader-only setting. One WORD equals 2 bytes.",
    )

    # Runtime links are system-maintained and intentionally hidden from normal UI.
    edge_server_id = fields.Many2one(
        "nsp.edge.server", readonly=True, copy=False, ondelete="set null", index=True
    )
    controller_id = fields.Many2one(
        "nsp.controller", readonly=True, copy=False, ondelete="set null", index=True
    )
    reader_id = fields.Many2one(
        "nsp.device", readonly=True, copy=False, ondelete="set null", index=True
    )
    antenna_id = fields.Many2one(
        "nsp.device.antenna", readonly=True, copy=False, ondelete="set null", index=True
    )
    runtime_status = fields.Char(compute="_compute_runtime_status")
    reader_power_dbm = fields.Integer(
        related="reader_id.power_dbm", string="Operational Power (dBm)", readonly=True
    )
    reader_read_interval_ms = fields.Integer(
        related="reader_id.read_interval_ms", string="Operational Read Interval ms", readonly=True
    )

    _sql_constraints = [
        (
            "device_whitelist_technical_code_unique",
            "unique(technical_code)",
            "Technical Code must be unique in Device Whitelist.",
        ),
        (
            "device_whitelist_serial_unique",
            "unique(serial_number)",
            "Serial Number must be unique in Device Whitelist.",
        ),
    ]

    @api.depends("device_type_code")
    def _compute_device_type_badge(self):
        for record in self:
            record.device_type_badge = record.device_type_code if record.device_type_code in VALID_DEVICE_TYPE_CODES else False

    @api.depends("device_type_id.name", "serial_number", "technical_code")
    def _compute_display_name(self):
        for record in self:
            identity = record.serial_number or record.technical_code or ""
            record.display_name = "%s · %s" % (
                record.device_type_id.name or _("Device"),
                identity,
            )

    @api.depends("device_type_code")
    def _compute_allowed_parent_ids(self):
        # Kept as a technical compatibility field. Device relationships are
        # configured in operational views, not in Device Whitelist.
        for record in self:
            record.allowed_parent_ids = self.browse()

    @api.depends(
        "device_type_code",
        "edge_server_id.status",
        "controller_id.status",
        "reader_id.status",
        "antenna_id.active",
    )
    def _compute_runtime_status(self):
        for record in self:
            if record.device_type_code == "SERVER":
                record.runtime_status = record.edge_server_id.status or ""
            elif record.device_type_code == "CONTROLLER":
                record.runtime_status = record.controller_id.status or ""
            elif record.device_type_code == "RFID_READER":
                record.runtime_status = record.reader_id.status or ""
            elif record.device_type_code == "ANTENNA":
                record.runtime_status = "active" if record.antenna_id.active else "inactive"
            else:
                record.runtime_status = ""

    @api.model
    def _normalize_serial(self, value):
        normalized = str(value or "").strip().upper()
        return normalized or False

    @api.model
    def _normalize_code(self, value):
        return str(value or "").strip().upper()

    @api.model
    def _device_type_code_from_vals(self, vals):
        type_id = vals.get("device_type_id")
        device_type = self.env["nsp.device.type"].browse(type_id).exists() if type_id else self.env["nsp.device.type"]
        return self._normalize_code(device_type.code if device_type else "")

    @api.onchange("device_type_id")
    def _onchange_device_type_id_generate_management_code(self):
        """Show a generated management code immediately on a new form.

        ``create()`` remains the authoritative fallback for API/import calls.
        """
        for record in self:
            if record._origin:
                continue
            type_code = record._normalize_code(record.device_type_id.code)
            if type_code in VALID_DEVICE_TYPE_CODES:
                record.technical_code = new_management_code(DEVICE_CODE_PREFIX[type_code])
            else:
                record.technical_code = False

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for source in vals_list:
            vals = dict(source)
            type_code = self._device_type_code_from_vals(vals)
            prefix = DEVICE_CODE_PREFIX.get(type_code, "NODE")
            vals["serial_number"] = self._normalize_serial(vals.get("serial_number"))
            if type_code == "RFID_READER" and not vals["serial_number"]:
                raise ValidationError(_("Serial Number is required for RFID Reader."))
            vals["technical_code"] = self._normalize_code(
                vals.get("technical_code") or new_management_code(prefix)
            )
            vals["name"] = str(
                vals.get("serial_number") or vals["technical_code"]
            ).strip()
            prepared.append(vals)
        records = super().create(prepared)
        records._sync_runtime_records()
        return records

    def write(self, vals):
        values = dict(vals)
        if "device_type_id" in values and any(
            record.edge_server_id or record.controller_id or record.reader_id or record.antenna_id
            for record in self
        ):
            raise ValidationError(_("Device Type cannot be changed after the runtime device is created."))

        values.pop("name", None)
        if "serial_number" in values:
            values["serial_number"] = self._normalize_serial(values.get("serial_number"))
        if "technical_code" in values:
            values["technical_code"] = self._normalize_code(values.get("technical_code"))

        if len(self) > 1 and ({"device_type_id", "serial_number", "technical_code"} & set(values)):
            for record in self:
                record.write(dict(values))
            return True

        for record in self:
            type_code = record.device_type_code
            if "device_type_id" in values:
                device_type = self.env["nsp.device.type"].browse(values["device_type_id"]).exists()
                type_code = self._normalize_code(device_type.code if device_type else "")
            serial_number = values.get("serial_number", record.serial_number)
            if type_code == "RFID_READER" and not self._normalize_serial(serial_number):
                raise ValidationError(_("Serial Number is required for RFID Reader."))
            if "serial_number" in values or "technical_code" in values:
                values["name"] = str(
                    serial_number or values.get("technical_code", record.technical_code) or ""
                ).strip()

        result = super().write(values)
        if not self.env.context.get("nsp_skip_runtime_sync"):
            self._sync_runtime_records()
        return result

    @api.constrains("device_type_id", "technical_code", "serial_number")
    def _check_device_definition(self):
        for record in self:
            if record.device_type_code not in VALID_DEVICE_TYPE_CODES:
                raise ValidationError(
                    _("Unsupported Device Type: %s")
                    % (record.device_type_id.name or record.device_type_code)
                )
            if not self._normalize_code(record.technical_code):
                raise ValidationError(_("Technical Code is required."))
            if record.device_type_code == "RFID_READER" and not self._normalize_serial(record.serial_number):
                raise ValidationError(_("Serial Number is required for RFID Reader."))

    def _runtime_edge_server(self):
        self.ensure_one()
        if self.device_type_code == "SERVER":
            return self.edge_server_id
        if self.device_type_code == "CONTROLLER":
            return self.controller_id.edge_server_id if self.controller_id else self.env["nsp.edge.server"]
        if self.device_type_code == "RFID_READER":
            return (
                self.reader_id.controller_id.edge_server_id
                if self.reader_id and self.reader_id.controller_id
                else self.env["nsp.edge.server"]
            )
        if self.device_type_code == "ANTENNA":
            return (
                self.antenna_id.device_id.controller_id.edge_server_id
                if self.antenna_id and self.antenna_id.device_id and self.antenna_id.device_id.controller_id
                else self.env["nsp.edge.server"]
            )
        return self.env["nsp.edge.server"]

    def _prepare_sync_payload(self):
        self.ensure_one()
        parent_code = False
        antenna_no = False
        physical_connection = False
        tid_addr = False
        tid_len = False
        if self.device_type_code == "CONTROLLER" and self.controller_id.edge_server_id:
            parent_code = self.controller_id.edge_server_id.edge_server_code
        elif self.device_type_code == "RFID_READER" and self.reader_id:
            parent_code = self.reader_id.controller_id.controller_id if self.reader_id.controller_id else False
            physical_connection = self.reader_id.connection_type or False
            tid_addr = int(self.reader_id.tid_addr or 0)
            tid_len = int(self.reader_id.tid_len or 0)
        elif self.device_type_code == "ANTENNA" and self.antenna_id:
            parent_code = self.antenna_id.device_id.device_code if self.antenna_id.device_id else False
            antenna_no = int(self.antenna_id.antenna_no or 0) or False
        return {
            "technical_code": self.technical_code or "",
            "name": self.name or self.technical_code or "",
            "device_type_code": self.device_type_code or "",
            "device_type_name": self.device_type_id.name or "",
            "serial_number": self.serial_number or False,
            "parent_technical_code": parent_code,
            "antenna_no": antenna_no,
            "physical_connection": physical_connection,
            "tid_start_address": tid_addr,
            "tid_length": tid_len,
            "active": bool(self.active),
        }

    def _sync_runtime_records(self):
        if self.env.context.get("nsp_skip_runtime_sync"):
            return True
        for record in self.sorted(key=lambda item: ({"SERVER": 1, "CONTROLLER": 2, "RFID_READER": 3, "ANTENNA": 4}.get(item.device_type_code, 9), item.id)):
            record._sync_runtime_record()
        return True

    def _sync_runtime_record(self):
        self.ensure_one()
        context_model = lambda model: self.env[model].sudo().with_context(nsp_from_device_whitelist=True)
        type_code = self.device_type_code
        active = bool(self.active)

        if type_code == "SERVER":
            Runtime = context_model("nsp.edge.server").with_context(active_test=False)
            runtime = self.edge_server_id.exists() or Runtime.search(
                [("edge_server_code", "=", self.technical_code)], limit=1
            )
            vals = {"name": self.name, "edge_server_code": self.technical_code, "active": active}
            if not active:
                vals["status"] = "revoked"
            runtime.write(vals) if runtime else None
            if not runtime:
                runtime = Runtime.create(vals)
            if runtime.whitelist_id != self:
                runtime.write({"whitelist_id": self.id})
            self.with_context(nsp_skip_runtime_sync=True).write({
                "edge_server_id": runtime.id,
                "controller_id": False,
                "reader_id": False,
                "antenna_id": False,
            })
            return True

        if type_code == "CONTROLLER":
            Runtime = context_model("nsp.controller").with_context(active_test=False)
            runtime = self.controller_id.exists() or Runtime.search(
                [("controller_id", "=", self.technical_code)], limit=1
            )
            vals = {
                "controller_name": self.name,
                "controller_id": self.technical_code,
                "edge_server_id": (
                    self.parent_id.edge_server_id.id
                    if self.parent_id and self.parent_id.edge_server_id
                    else (self.controller_id.edge_server_id.id if self.controller_id and self.controller_id.edge_server_id else False)
                ),
                "active": active,
            }
            if not active:
                vals["status"] = "revoked"
            runtime.write(vals) if runtime else None
            if not runtime:
                runtime = Runtime.create(vals)
            if runtime.whitelist_id != self:
                runtime.write({"whitelist_id": self.id})
            self.with_context(nsp_skip_runtime_sync=True).write({
                "edge_server_id": False,
                "controller_id": runtime.id,
                "reader_id": False,
                "antenna_id": False,
            })
            return True

        if type_code == "RFID_READER":
            Runtime = context_model("nsp.device").with_context(active_test=False)
            runtime = self.reader_id.exists() or Runtime.search(
                [("device_code", "=", self.technical_code)], limit=1
            )
            vals = {
                "name": self.name,
                "serial_number": self.serial_number,
                "device_code": self.technical_code,
                "controller_id": (
                    self.parent_id.controller_id.id
                    if self.parent_id and self.parent_id.controller_id
                    else (self.reader_id.controller_id.id if self.reader_id and self.reader_id.controller_id else False)
                ),
                "connection_type": self.connection_type or False,
                "tid_addr": int(self.tid_addr or 0),
                "tid_len": int(self.tid_len or 6),
                "active": active,
            }
            if not active:
                vals["status"] = "offline"
            runtime.write(vals) if runtime else None
            if not runtime:
                runtime = Runtime.create(vals)
            if runtime.whitelist_id != self:
                runtime.write({"whitelist_id": self.id})
            self.with_context(nsp_skip_runtime_sync=True).write({
                "edge_server_id": False,
                "controller_id": False,
                "reader_id": runtime.id,
                "antenna_id": False,
            })
            return True

        if type_code == "ANTENNA":
            Runtime = context_model("nsp.device.antenna").with_context(active_test=False)
            runtime = self.antenna_id.exists() or Runtime.search(
                [("technical_code", "=", self.technical_code)], limit=1
            )
            vals = {
                "device_id": (
                    self.parent_id.reader_id.id
                    if self.parent_id and self.parent_id.reader_id
                    else (self.antenna_id.device_id.id if self.antenna_id and self.antenna_id.device_id else False)
                ),
                "antenna_no": int(self.antenna_no or (self.antenna_id.antenna_no if self.antenna_id else 0) or 0),
                "technical_code": self.technical_code,
                "serial_number": self.serial_number or False,
                "active": active,
            }
            runtime.write(vals) if runtime else None
            if not runtime:
                runtime = Runtime.create(vals)
            if runtime.whitelist_id != self:
                runtime.write({"whitelist_id": self.id})
            self.with_context(nsp_skip_runtime_sync=True).write({
                "edge_server_id": False,
                "controller_id": False,
                "reader_id": False,
                "antenna_id": runtime.id,
            })
        return True
