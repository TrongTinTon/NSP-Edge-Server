# -*- coding: utf-8 -*-
import logging

from odoo import api, fields, models, _

_logger = logging.getLogger(__name__)


class NspNotification(models.Model):
    _name = "nsp.notification"
    _description = "NSP Notification"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "event_time desc, id desc"

    name = fields.Char(string="Title", required=True, tracking=True)
    message = fields.Text(string="Message", required=True, tracking=True)
    category = fields.Selection([
        ("system", "System"),
        ("device_security", "Device Security"),
    ], string="Category", required=True, default="system", index=True, tracking=True)
    severity = fields.Selection([
        ("info", "Info"),
        ("warning", "Warning"),
        ("critical", "Critical"),
    ], string="Severity", required=True, default="warning", index=True, tracking=True)
    state = fields.Selection([
        ("unread", "Unread"),
        ("read", "Read"),
        ("archived", "Archived"),
    ], string="State", required=True, default="unread", index=True, tracking=True)
    event_time = fields.Datetime(string="Event Time", required=True, default=fields.Datetime.now, index=True)

    controller_code = fields.Char(string="Controller Code", index=True, readonly=True)
    device_serial = fields.Char(string="Device Serial", index=True, readonly=True)
    source_model = fields.Char(string="Source Model", readonly=True)
    source_record_id = fields.Integer(string="Source Record ID", readonly=True)
    dedupe_key = fields.Char(string="Dedupe Key", index=True, copy=False, readonly=True)

    read_at = fields.Datetime(string="Read At", readonly=True)
    read_by = fields.Many2one("res.users", string="Read By", readonly=True)
    active = fields.Boolean(default=True)

    _sql_constraints = [
        ("dedupe_key_unique", "unique(dedupe_key)", "Notification dedupe key must be unique."),
    ]

    @api.model
    def _normalize_code(self, value):
        return str(value or "").strip().upper()

    @api.model
    def _notify_it_users(self, notification):
        """Best-effort realtime alert to active NSP IT users."""
        group = self.env.ref("nsp_core.group_nsp_it_parking", raise_if_not_found=False)
        if not group:
            return True
        payload = {
            "type": "nsp_notification",
            "notification_id": notification.id,
            "title": notification.name,
            "message": notification.message,
            "category": notification.category,
            "severity": notification.severity,
            "event_time": fields.Datetime.to_string(notification.event_time),
            "controller_code": notification.controller_code or "",
            "device_serial": notification.device_serial or "",
        }
        bus = self.env["bus.bus"].sudo()
        for user in group.user_ids.filtered(lambda item: item.active and item.partner_id):
            try:
                if hasattr(bus, "_sendone"):
                    bus._sendone(user.partner_id, "nsp_notification", payload)
                elif hasattr(bus, "sendone"):
                    bus.sendone((self._cr.dbname, "res.partner", user.partner_id.id), payload)
            except Exception:
                _logger.debug("Unable to send NSP notification over bus", exc_info=True)
        return True

    @api.model
    def notify_device_not_whitelisted(self, serial_number, controller_code=None, details=None):
        serial = self._normalize_code(serial_number)
        controller = self._normalize_code(controller_code)
        if not serial:
            return self.browse()
        dedupe_key = "device-not-whitelisted:%s:%s" % (controller or "unknown", serial)
        message = _(
            "Reader %(serial)s reported by Controller %(controller)s is not present in Device Whitelist. "
            "The device configuration/status was rejected."
        ) % {
            "serial": serial,
            "controller": controller or _("Unknown"),
        }
        details = details if isinstance(details, dict) else {}
        metadata = []
        for label, key in (("Model", "model_number"), ("Vendor", "vendor"), ("Device Type", "device_type")):
            value = str(details.get(key) or "").strip()
            if value:
                metadata.append("%s: %s" % (label, value))
        if metadata:
            message = "%s\n%s" % (message, "; ".join(metadata))

        values = {
            "name": _("Device is not whitelisted"),
            "message": message,
            "category": "device_security",
            "severity": "warning",
            "state": "unread",
            "event_time": fields.Datetime.now(),
            "controller_code": controller or False,
            "device_serial": serial,
            "source_model": "nsp.device.whitelist",
            "source_record_id": 0,
            "dedupe_key": dedupe_key,
            "read_at": False,
            "read_by": False,
            "active": True,
        }
        notification = self.sudo().with_context(active_test=False).search([("dedupe_key", "=", dedupe_key)], limit=1)
        if notification:
            notification.write(values)
        else:
            notification = self.sudo().create(values)
        self._notify_it_users(notification)
        return notification

    def action_mark_read(self):
        self.write({"state": "read", "read_at": fields.Datetime.now(), "read_by": self.env.user.id})
        return True

    def action_mark_unread(self):
        self.write({"state": "unread", "read_at": False, "read_by": False})
        return True

    def action_archive(self):
        self.write({"state": "archived", "active": False})
        return True
