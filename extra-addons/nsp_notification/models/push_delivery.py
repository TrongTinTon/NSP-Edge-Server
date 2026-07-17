# -*- coding: utf-8 -*-
from datetime import timedelta
import logging

from odoo import api, fields, models, _

_logger = logging.getLogger(__name__)


class NspPushDelivery(models.Model):
    _name = "nsp.push.delivery"
    _description = "NSP Push Delivery"
    _order = "create_date desc, id desc"

    notification_id = fields.Many2one("nsp.notification", required=True, ondelete="cascade", index=True)
    target_user_id = fields.Many2one("nsp.user", string="NSP User", ondelete="set null", index=True)
    recipient_user_id = fields.Many2one("res.users", string="Odoo User", ondelete="set null", index=True)
    device_id = fields.Many2one("nsp.push.device", required=True, ondelete="cascade", index=True)
    provider_id = fields.Many2one("nsp.push.provider", string="Provider", ondelete="set null", index=True)
    provider_type = fields.Selection([
        ("fcm", "FCM"),
        ("apns", "APNs"),
        ("hms", "HMS"),
        ("in_app", "In-App"),
    ], required=True, index=True)
    priority = fields.Selection([
        ("normal", "Normal"),
        ("high", "High"),
        ("emergency", "Emergency"),
    ], default="normal", required=True, index=True)
    status = fields.Selection([
        ("queued", "Queued"),
        ("sending", "Sending"),
        ("sent", "Sent"),
        ("delivered", "Delivered"),
        ("acked", "Acked"),
        ("read", "Read"),
        ("failed", "Failed"),
        ("expired", "Expired"),
        ("cancelled", "Cancelled"),
    ], default="queued", required=True, index=True)
    attempt_count = fields.Integer(default=0)
    max_retry = fields.Integer(default=lambda self: int(self.env["ir.config_parameter"].sudo().get_param("nsp_notification.push_max_retry", "3") or 3))
    next_retry_at = fields.Datetime(index=True)
    sent_at = fields.Datetime(readonly=True)
    delivered_at = fields.Datetime(readonly=True)
    acked_at = fields.Datetime(readonly=True)
    read_at = fields.Datetime(readonly=True)
    provider_message_id = fields.Char(readonly=True)
    error_code = fields.Char(readonly=True)
    error_message = fields.Char(readonly=True)

    _sql_constraints = [
        ("delivery_notification_device_unique", "unique(notification_id, device_id)", "This notification is already queued for this device."),
    ]

    def _retry_delay_seconds(self):
        try:
            return max(int(self.env["ir.config_parameter"].sudo().get_param("nsp_notification.push_retry_delay_sec", "60") or 60), 10)
        except Exception:
            return 60

    def action_send_now(self):
        for delivery in self.sudo():
            if delivery.status not in ("queued", "failed"):
                continue
            provider = delivery.provider_id or delivery.device_id.provider_id
            if not provider and delivery.provider_type == "in_app":
                provider = self.env["nsp.push.provider"].sudo().search([("provider_type", "=", "in_app"), ("active", "=", True)], limit=1)
            if not provider:
                delivery.write({
                    "status": "failed",
                    "attempt_count": delivery.attempt_count + 1,
                    "error_code": "missing_provider",
                    "error_message": _("No active push provider is configured for this device."),
                    "next_retry_at": fields.Datetime.now() + timedelta(seconds=delivery._retry_delay_seconds()),
                })
                continue
            delivery.write({"status": "sending", "attempt_count": delivery.attempt_count + 1, "provider_id": provider.id})
            ok, message_id_or_code, error_message = provider.send_delivery(delivery)
            if ok:
                delivery.write({
                    "status": "sent",
                    "sent_at": fields.Datetime.now(),
                    "provider_message_id": message_id_or_code or False,
                    "error_code": False,
                    "error_message": False,
                    "next_retry_at": False,
                })
                provider.write({"last_success_at": fields.Datetime.now(), "last_error": False})
            else:
                terminal = delivery.attempt_count >= delivery.max_retry
                delivery.write({
                    "status": "failed",
                    "error_code": message_id_or_code or "send_failed",
                    "error_message": error_message or _("Push send failed."),
                    "next_retry_at": False if terminal else fields.Datetime.now() + timedelta(seconds=delivery._retry_delay_seconds()),
                })
                provider.write({"last_failure_at": fields.Datetime.now(), "last_error": (error_message or message_id_or_code or "send_failed")[:512]})
        return True

    def action_mark_acked(self):
        self.write({"status": "acked", "acked_at": fields.Datetime.now()})
        return True

    def action_mark_read(self):
        self.write({"status": "read", "read_at": fields.Datetime.now()})
        notifications = self.mapped("notification_id")
        for notification in notifications:
            if notification.state != "read":
                notification.write({"state": "read", "read_at": fields.Datetime.now(), "read_by": self.env.user.id})
        return True

    def action_cancel(self):
        self.write({"status": "cancelled"})
        return True

    @api.model
    def cron_send_queue(self, limit=200):
        now = fields.Datetime.now()
        domain = [
            "|",
            ("status", "=", "queued"),
            "&", ("status", "=", "failed"), ("next_retry_at", "!=", False),
            "|", ("next_retry_at", "=", False), ("next_retry_at", "<=", now),
        ]
        deliveries = self.sudo().search(domain, order="priority desc, create_date asc", limit=limit)
        deliveries = deliveries.filtered(lambda d: d.status == "queued" or d.attempt_count < d.max_retry)
        deliveries.action_send_now()
        return True

    @api.model
    def cron_cleanup(self):
        try:
            days = int(self.env["ir.config_parameter"].sudo().get_param("nsp_notification.push_delivery_retention_days", "30") or 30)
        except Exception:
            days = 30
        if days <= 0:
            return True
        cutoff = fields.Datetime.now() - timedelta(days=days)
        old = self.sudo().search([("create_date", "<", cutoff), ("status", "in", ("sent", "delivered", "acked", "read", "failed", "expired", "cancelled"))], limit=5000)
        old.unlink()
        return True
