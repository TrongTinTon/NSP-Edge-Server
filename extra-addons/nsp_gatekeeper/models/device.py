import json
import hashlib
import requests
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class Device(models.Model):
    _name = "nsp.device"
    _description = "NSP Managed Device"
    _rec_name = "serial_number"
    _order = "controller_id, serial_number, id"

    device_code = fields.Char(string="Device code", help="Code of the device from the controller")
    device_name = fields.Char(string="Device name", help="Name of the device for easier identification")
    device_type_id = fields.Many2one(
        "nsp.device.type",
        string="Device Type",
        ondelete="set null",
        help="Optional Admin-maintained device type. Controller reports may leave this blank; Admin can set or quick-create it later.",
    )
    device_type = fields.Char(
        string="Device Type Code",
        copy=False,
        index=True,
        help="Technical code copied from Device Type for API payloads. Blank when no Device Type is selected.",
    )
    model_number = fields.Char(string="Model number", help="Product model number of the device")
    device_vendor = fields.Char(string="Vendor", help="Device vendor")
    serial_number = fields.Char(string="Serial", help="Device serial number", required=True)
    device_ip = fields.Char(string="IP Address", help="IP address of the device")
    device_port = fields.Integer(string="Port", help="Port number of the device")
    status = fields.Selection([
        ('online', "Online"),
        ('offline', "Offline"),
        ('degraded', 'Degraded'),
    ], string="Status", help="Current status of the device")
    last_seen = fields.Datetime(string="Last seen", help="Last time the device was seen online")
    firmware_version = fields.Char(string="Firmware version", help="Firmware version of the device")
    whitelist_record_id = fields.Many2one(
        "nsp.device.whitelist",
        string="Whitelist Record",
        ondelete="restrict",
        index=True,
        help="Approved/candidate Device Whitelist record used to declare this RFID Reader. Device identity is taken from this whitelist record.",
    )
    whitelist_status = fields.Selection([
        ("valid", "Valid Device"),
        ("invalid", "Invalid Device"),
    ], string="Whitelist Status", compute="_compute_whitelist_status", store=False)

    # Controller/device runtime configuration kept directly on the device.
    power_dbm = fields.Integer(string="Power dBm", default=30, help="Default RF power to apply to the reader/antennas")
    read_interval_ms = fields.Integer(string="Read interval ms", default=200, help="Reader polling/read interval in milliseconds")
    tid_addr = fields.Integer(string="TID start address", default=2, help="TID start address used by SetTIDParameter")
    tid_len = fields.Integer(string="TID length", default=4, help="TID length used by SetTIDParameter")
    check_antenna_enabled = fields.Boolean(string="Check antenna", default=True, help="Enable SDK antenna checking before reading/writing tags")
    config_revision = fields.Integer(string="Config revision", default=0, readonly=True)
    config_hash = fields.Char(string="Config hash", readonly=True)
    config_sync_status = fields.Selection([
        ('not_synced', 'Not synced'),
        ('pending', 'Pending'),
        ('success', 'Success'),
        ('failed', 'Failed'),
    ], string="Config sync", default='not_synced', readonly=True)
    config_applied_status = fields.Selection([
        ('not_applied', 'Not applied'),
        ('applied', 'Applied'),
        ('failed', 'Failed'),
    ], string="Applied status", default='not_applied', readonly=True)
    last_config_sync_at = fields.Datetime(string="Last config sync", readonly=True)
    config_sync_message = fields.Char(string="Config sync message", readonly=True)

    managed = fields.Boolean(string="Managed", help="Whether the device is managed by the system")
    antennas = fields.Integer(string="Number of antennas", compute="_compute_antenna_count", store=False)
    antennas_ids = fields.One2many(
        'nsp.device.antenna',
        'device_id',
        string="Antennas",
        help="Antennas of the device"
    )

    controller_id = fields.Many2one(
        'nsp.controller',
        string="Managing controller",
        ondelete="set null",
        help="Controller that manages this device"
    )

    @api.onchange("device_type_id")
    def _onchange_device_type_id(self):
        for rec in self:
            rec.device_type = rec.device_type_id.code if rec.device_type_id else False

    @api.model
    def _device_type_vals_from_report(self, reported_value):
        """Map controller-reported type to an existing NSP Device Type only.

        No default and no auto-create: missing or unknown Device Type stays blank.
        """
        device_type = self.env["nsp.device.type"].sudo().find_by_reported_value(reported_value)
        if device_type:
            return {"device_type_id": device_type.id, "device_type": device_type.code}
        return {"device_type_id": False, "device_type": False}

    def _device_type_code(self):
        self.ensure_one()
        return self.device_type_id.code if self.device_type_id else (self.device_type or False)

    @api.depends("whitelist_record_id", "whitelist_record_id.approval_state", "serial_number", "device_code")
    def _compute_whitelist_status(self):
        Whitelist = self.env["nsp.device.whitelist"].sudo()
        for record in self:
            rec = record.whitelist_record_id
            if not rec and record.serial_number:
                rec = Whitelist.search([("serial_number", "=", record.serial_number)], limit=1)
            if not rec and record.device_code:
                rec = Whitelist.search([("device_code", "=", record.device_code)], limit=1)
            record.whitelist_status = "valid" if rec and rec.approval_state == "valid" else "invalid"

    @api.model
    def _normalize_serial(self, value):
        return str(value or "").strip().upper()

    @api.model
    def _device_vals_from_whitelist(self, whitelist, existing_vals=None, overwrite=False):
        """Prepare nsp.device identity values from a Device Whitelist record.

        Device identity is sourced from the whitelist record. The business keys
        are Serial and Device Code; nsp.device does not keep a separate technical identifier field.
        """
        vals = dict(existing_vals or {})
        if not whitelist:
            return vals

        def put(name, value):
            if value in (None, False, ""):
                return
            if overwrite or name not in vals or vals.get(name) in (None, False, ""):
                vals[name] = value

        serial = self._normalize_serial(whitelist.serial_number)
        device_code = whitelist.device_code or serial
        put("serial_number", serial)
        put("device_code", device_code)
        put("device_name", whitelist.device_name or whitelist.display_name or serial)
        put("model_number", whitelist.model_number)
        put("device_vendor", whitelist.device_vendor)

        reported_type = whitelist.device_type or False
        if reported_type and (overwrite or not vals.get("device_type_id")):
            dtype_vals = self._device_type_vals_from_report(reported_type)
            if dtype_vals.get("device_type_id"):
                vals.update(dtype_vals)
            elif overwrite or not vals.get("device_type"):
                vals["device_type"] = reported_type
        return vals

    @api.onchange("whitelist_record_id")
    def _onchange_whitelist_record_id(self):
        for rec in self:
            if rec.whitelist_record_id:
                vals = rec._device_vals_from_whitelist(rec.whitelist_record_id, overwrite=True)
                for key, value in vals.items():
                    if key in rec._fields:
                        rec[key] = value

    @api.constrains('serial_number')
    def _check_serial_number_unique(self):
        for record in self:
            if record.serial_number:
                existing = self.search([
                    ('serial_number', '=', record.serial_number),
                    ('id', '!=', record.id)
                ], limit=1)
                if existing:
                    raise ValidationError(_("Serial number '%s' already exists") % record.serial_number)

    @api.model_create_multi
    def create(self, vals_list):
        Whitelist = self.env["nsp.device.whitelist"].sudo()
        prepared = []
        for vals in vals_list:
            vals = dict(vals)
            if vals.get("whitelist_record_id"):
                wl = Whitelist.browse(vals.get("whitelist_record_id")).exists()
                if wl:
                    vals = self._device_vals_from_whitelist(wl, vals, overwrite=False)
            if vals.get("serial_number"):
                vals["serial_number"] = self._normalize_serial(vals.get("serial_number"))
            if not vals.get("device_code") and vals.get("serial_number"):
                vals["device_code"] = vals.get("serial_number")
            if vals.get("device_type_id"):
                dtype = self.env["nsp.device.type"].sudo().browse(vals["device_type_id"]).exists()
                vals["device_type"] = dtype.code if dtype else False
            elif "device_type" not in vals:
                vals["device_type"] = False
            prepared.append(vals)
        return super().create(prepared)

    def write(self, vals):
        config_fields = {'power_dbm', 'read_interval_ms', 'tid_addr', 'tid_len', 'check_antenna_enabled'}
        vals = dict(vals)
        if vals.get("whitelist_record_id"):
            wl = self.env["nsp.device.whitelist"].sudo().browse(vals.get("whitelist_record_id")).exists()
            if wl:
                vals = self._device_vals_from_whitelist(wl, vals, overwrite=True)
        if vals.get("serial_number"):
            vals["serial_number"] = self._normalize_serial(vals.get("serial_number"))
        if not vals.get("device_code") and vals.get("serial_number"):
            vals["device_code"] = vals.get("serial_number")
        if "device_type_id" in vals:
            dtype = self.env["nsp.device.type"].sudo().browse(vals.get("device_type_id")).exists() if vals.get("device_type_id") else self.env["nsp.device.type"].browse()
            vals["device_type"] = dtype.code if dtype else False
        if config_fields.intersection(vals.keys()):
            vals['config_revision'] = (self.config_revision or 0) + 1 if len(self) == 1 else vals.get('config_revision', 0)
            vals['config_sync_status'] = 'not_synced'
            vals['config_applied_status'] = 'not_applied'
        result = super().write(vals)
        if config_fields.intersection(vals.keys()):
            for rec in self:
                rec._update_config_hash()
        return result

    @api.depends("antennas_ids")
    def _compute_antenna_count(self):
        for record in self:
            record.antennas = len(record.antennas_ids)

    def _build_config_payload(self):
        """Return only technical Reader configuration required by Controller.

        Integration identity is serial_number + device_code. Database IDs and
        configuration ownership (branch/gate/lane/direction) never belong here.
        """
        self.ensure_one()
        antennas = []
        for antenna in self.antennas_ids.sorted(key=lambda rec: (rec.antenna_id or 0, rec.id)):
            antennas.append({
                "antenna_no": int(antenna.antenna_id or 0),
                "enabled": bool(antenna.is_active),
                "power_dbm": int(antenna.power_dbm if antenna.power_dbm is not None else (self.power_dbm or 0)),
            })
        return {
            "serial_number": self.serial_number or "",
            "device_code": self.device_code or self.serial_number or "",
            "active": bool(self.managed),
            "connection": {
                "protocol": "tcp",
                "ip_address": self.device_ip or "",
                "port": int(self.device_port or 0),
            },
            "antennas": antennas,
        }

    def _update_config_hash(self):
        for rec in self:
            try:
                payload = rec._build_config_payload()
                payload.pop("config_hash", None)
                digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode('utf-8')).hexdigest()
                super(Device, rec).write({"config_hash": digest})
            except Exception:
                pass

    def action_sync_config_to_controller(self):
        for rec in self:
            if not rec.controller_id or not rec.controller_id.url:
                raise ValidationError(_("Device %s does not have a controller URL.") % (rec.display_name or rec.serial_number))
            identifier = rec.device_code or rec.serial_number
            if not identifier:
                raise ValidationError(_("Device does not have an identifier."))
            payload = rec._build_config_payload()
            endpoint = "%s/api/v1/devices/%s/config" % (rec.controller_id.url.rstrip('/'), identifier)
            headers = {}
            if rec.controller_id.session_token:
                headers["X-SESSION-TOKEN"] = rec.controller_id.session_token
                headers["X-Controller-Session"] = rec.controller_id.session_token
            rec.write({"config_sync_status": "pending", "config_applied_status": "not_applied", "config_sync_message": "Sending configuration to controller..."})
            try:
                response = requests.post(endpoint, json=payload, headers=headers, timeout=15)
                try:
                    result = response.json()
                except Exception:
                    result = {"message": response.text}
                success = bool(result.get("success", False))
                applied = result.get("applied_status") or ("applied" if success else "failed")
                message = result.get("message") or response.reason or ("Configuration applied by controller" if success else "Controller returned failure")
                if response.status_code >= 400:
                    success = False
                    applied = "failed"
                    message = "HTTP %s from Controller: %s" % (response.status_code, message)
                rec.write({
                    "config_sync_status": "success" if success else "failed",
                    "config_applied_status": "applied" if applied == "applied" else "failed",
                    "last_config_sync_at": fields.Datetime.now(),
                    "config_sync_message": message,
                })
                if not success:
                    raise ValidationError(_("Controller did not apply configuration: %s") % message)
            except Exception as exc:
                rec.write({
                    "config_sync_status": "failed",
                    "config_applied_status": "failed",
                    "last_config_sync_at": fields.Datetime.now(),
                    "config_sync_message": str(exc),
                })
                raise ValidationError(_("Could not sync configuration to Controller: %s") % str(exc))
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Configuration sync"),
                "message": _("Device configuration sent to Controller."),
                "type": "success",
                "sticky": False,
            }
        }

    @api.model
    def cron_mark_offline_devices(self):
        try:
            timeout_sec = int(self.env['ir.config_parameter'].sudo().get_param('nsp_gatekeeper.device_report_timeout_sec', '300') or '300')
        except Exception:
            timeout_sec = 300
        timeout_sec = max(30, timeout_sec)
        self.env.cr.execute("""
            UPDATE nsp_device
               SET status='offline'
             WHERE COALESCE(status, 'offline') != 'offline'
               AND (last_seen IS NULL OR last_seen < (NOW() AT TIME ZONE 'UTC') - (%s || ' seconds')::interval)
        """, (str(timeout_sec),))
        self.env.cr.execute("""
            UPDATE nsp_device d
               SET status='offline'
              FROM nsp_controller c
             WHERE d.controller_id = c.id
               AND COALESCE(d.status, 'offline') != 'offline'
               AND COALESCE(c.status, 'offline') = 'offline'
        """)
        return True

    def action_send_config(self):
        return self.action_sync_config_to_controller()
