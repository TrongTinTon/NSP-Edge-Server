# -*- coding: utf-8 -*-
import json
import logging
import time
from datetime import timedelta

import requests

from odoo import api, fields, models, _

_logger = logging.getLogger(__name__)


class NspNotificationDelivery(models.Model):
    _name = "nsp.notification.delivery"
    _description = "NSP Notification Delivery"
    _order = "create_date desc, id desc"

    notification_id = fields.Many2one(
        "nsp.notification", required=True, index=True, ondelete="cascade"
    )
    recipient_user_id = fields.Many2one(
        "nsp.user",
        related="notification_id.recipient_user_id",
        store=True,
        index=True,
        readonly=True,
    )
    device_uid = fields.Char(required=True, index=True, readonly=True)
    channel = fields.Selection(
        [("realtime", "Realtime"), ("push", "Push")],
        required=True,
        index=True,
        readonly=True,
    )
    provider = fields.Char(required=True, index=True, readonly=True)
    state = fields.Selection(
        [
            ("pending", "Pending"),
            ("sent", "Sent"),
            ("delivered", "Delivered"),
            ("failed", "Failed"),
        ],
        default="pending",
        required=True,
        index=True,
        readonly=True,
    )
    attempt_count = fields.Integer(default=0, readonly=True)
    next_attempt_at = fields.Datetime(readonly=True, index=True)
    sent_at = fields.Datetime(readonly=True)
    delivered_at = fields.Datetime(readonly=True)
    provider_message_id = fields.Char(readonly=True, copy=False)
    last_error = fields.Text(readonly=True)

    _sql_constraints = [
        (
            "delivery_unique",
            "unique(notification_id, device_uid, channel, provider)",
            "Notification delivery already exists for this device/channel/provider.",
        ),
    ]

    @api.model
    def enqueue(self, notification, device_uid, channel="realtime", provider="realtime"):
        notification.ensure_one()
        vals = {
            "notification_id": notification.id,
            "device_uid": str(device_uid or "").strip(),
            "channel": channel,
            "provider": str(provider or "").strip().lower() or "none",
        }
        existing = self.search(
            [
                ("notification_id", "=", notification.id),
                ("device_uid", "=", vals["device_uid"]),
                ("channel", "=", channel),
                ("provider", "=", vals["provider"]),
            ],
            limit=1,
        )
        if existing:
            return existing
        delivery = self.create(vals)
        # Realtime is an internal queue and is safe to mark sent immediately.
        # Push delivery is intentionally deferred to the cron dispatcher so an
        # external provider outage never rolls back the business transaction.
        if channel == "realtime" and vals["provider"] == "realtime":
            self.env["nsp.notification.delivery.service"].dispatch(delivery)
        return delivery

    def mark_delivered(self):
        pending = self.filtered(lambda rec: rec.state != "delivered")
        if pending:
            pending.write(
                {"state": "delivered", "delivered_at": fields.Datetime.now()}
            )
        return True

    def action_retry(self):
        self.filtered(lambda rec: rec.channel == "push").write(
            {
                "state": "pending",
                "next_attempt_at": False,
                "last_error": False,
            }
        )
        return True

    @api.model
    def cron_dispatch_pending(self, limit=100):
        now = fields.Datetime.now()
        deliveries = self.sudo().search(
            [
                ("channel", "=", "push"),
                ("state", "=", "pending"),
                "|",
                ("next_attempt_at", "=", False),
                ("next_attempt_at", "<=", now),
            ],
            order="next_attempt_at asc, id asc",
            limit=max(1, min(int(limit or 100), 500)),
        )
        service = self.env["nsp.notification.delivery.service"].sudo()
        for delivery in deliveries:
            try:
                with self.env.cr.savepoint():
                    service.dispatch(delivery)
            except Exception:
                # dispatch() contains its own retry bookkeeping. This guard keeps
                # one unexpected provider failure from stopping the whole batch.
                _logger.exception(
                    "Unexpected NSP push dispatch failure for delivery %s", delivery.id
                )
        return len(deliveries)


class NspNotificationDeliveryService(models.AbstractModel):
    _name = "nsp.notification.delivery.service"
    _description = "NSP Notification Delivery Service"

    @api.model
    def dispatch(self, delivery):
        delivery.ensure_one()
        if delivery.state in ("sent", "delivered"):
            return True
        method = getattr(
            self,
            "_dispatch_%s" % (delivery.provider or "").replace("-", "_"),
            None,
        )
        if not method:
            return self._record_failure(
                delivery, _("Unsupported notification provider: %s") % delivery.provider
            )

        config = self.env["nsp.notification.provider.config"].get_active_config()
        try:
            provider_message_id = method(delivery, config)
            delivery.write(
                {
                    "state": "sent",
                    "attempt_count": delivery.attempt_count + 1,
                    "next_attempt_at": False,
                    "sent_at": fields.Datetime.now(),
                    "provider_message_id": str(provider_message_id or "") or False,
                    "last_error": False,
                }
            )
            return True
        except Exception as exc:
            _logger.warning(
                "NSP notification delivery failed: id=%s provider=%s error=%s",
                delivery.id,
                delivery.provider,
                exc,
            )
            return self._record_failure(delivery, str(exc), config=config)

    @api.model
    def _record_failure(self, delivery, error, config=False):
        config = config or self.env[
            "nsp.notification.provider.config"
        ].get_active_config()
        max_attempts = max(1, int(config.max_attempts if config else 5))
        delay = max(1, int(config.retry_delay_seconds if config else 60))
        attempts = delivery.attempt_count + 1
        terminal = attempts >= max_attempts
        delivery.write(
            {
                "state": "failed" if terminal else "pending",
                "attempt_count": attempts,
                "next_attempt_at": False
                if terminal
                else fields.Datetime.now() + timedelta(seconds=delay),
                "last_error": str(error or _("Unknown provider error"))[:4000],
            }
        )
        return False

    @api.model
    def _dispatch_realtime(self, delivery, _config=False):
        return "realtime:%s" % delivery.id

    @api.model
    def _dispatch_none(self, _delivery, _config=False):
        raise ValueError(_("Push provider is not configured."))

    @api.model
    def _mobile_device(self, delivery):
        if "nsp.mobile.device" not in self.env.registry.models:
            raise ValueError(_("NSP Mobile is not installed."))
        device = self.env["nsp.mobile.device"].sudo().search(
            [
                ("device_uid", "=", delivery.device_uid),
                ("active", "=", True),
                ("push_enabled", "=", True),
            ],
            limit=1,
        )
        if not device or not device.push_token:
            raise ValueError(_("Mobile push token is inactive or missing."))
        if device.push_provider != delivery.provider:
            raise ValueError(_("Mobile push provider changed after enqueue."))
        return device

    @api.model
    def _message_data(self, delivery):
        notification = delivery.notification_id
        return {
            "notification_id": str(notification.id),
            "category": str(notification.category or "system"),
            "severity": str(notification.severity or "info"),
            "transaction_uid": str(notification.transaction_uid or ""),
            "parking_event_type": str(notification.parking_event_type or ""),
        }

    @api.model
    def _dispatch_fcm(self, delivery, config):
        if not config or not config.fcm_enabled:
            raise ValueError(_("FCM is disabled or not configured."))
        device = self._mobile_device(delivery)
        try:
            import jwt
        except ImportError as exc:
            raise ValueError(_("Python dependency 'PyJWT' is required for FCM.")) from exc

        try:
            credentials = json.loads(config.fcm_service_account_json or "{}")
        except (TypeError, ValueError) as exc:
            raise ValueError(_("FCM Service Account JSON is invalid.")) from exc
        client_email = str(credentials.get("client_email") or "").strip()
        private_key = str(credentials.get("private_key") or "").replace("\\n", "\n")
        token_uri = str(
            credentials.get("token_uri") or "https://oauth2.googleapis.com/token"
        ).strip()
        project_id = str(config.fcm_project_id or credentials.get("project_id") or "").strip()
        if not client_email or not private_key or not token_uri or not project_id:
            raise ValueError(_("FCM credentials are incomplete."))

        now = int(time.time())
        assertion = jwt.encode(
            {
                "iss": client_email,
                "scope": "https://www.googleapis.com/auth/firebase.messaging",
                "aud": token_uri,
                "iat": now,
                "exp": now + 3600,
            },
            private_key,
            algorithm="RS256",
        )
        timeout = max(1, int(config.request_timeout_seconds or 15))
        token_response = requests.post(
            token_uri,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            },
            timeout=timeout,
        )
        if token_response.status_code >= 400:
            raise ValueError(
                "FCM OAuth HTTP %s: %s"
                % (token_response.status_code, token_response.text[:1000])
            )
        access_token = str((token_response.json() or {}).get("access_token") or "")
        if not access_token:
            raise ValueError(_("FCM OAuth response has no access token."))

        notification = delivery.notification_id
        response = requests.post(
            "https://fcm.googleapis.com/v1/projects/%s/messages:send" % project_id,
            headers={
                "Authorization": "Bearer %s" % access_token,
                "Content-Type": "application/json; charset=UTF-8",
            },
            json={
                "message": {
                    "token": device.push_token,
                    "notification": {
                        "title": notification.name,
                        "body": notification.message,
                    },
                    "data": self._message_data(delivery),
                }
            },
            timeout=timeout,
        )
        if response.status_code >= 400:
            raise ValueError(
                "FCM send HTTP %s: %s" % (response.status_code, response.text[:1000])
            )
        return (response.json() or {}).get("name")

    @api.model
    def _dispatch_apns(self, delivery, config):
        if not config or not config.apns_enabled:
            raise ValueError(_("APNS is disabled or not configured."))
        device = self._mobile_device(delivery)
        try:
            import httpx
            import jwt
        except ImportError as exc:
            raise ValueError(
                _("Python dependencies 'httpx[http2]' and 'PyJWT' are required for APNS.")
            ) from exc

        team_id = str(config.apns_team_id or "").strip()
        key_id = str(config.apns_key_id or "").strip()
        topic = str(config.apns_topic or "").strip()
        private_key = str(config.apns_private_key or "").replace("\\n", "\n")
        if not team_id or not key_id or not topic or not private_key:
            raise ValueError(_("APNS credentials are incomplete."))

        provider_token = jwt.encode(
            {"iss": team_id, "iat": int(time.time())},
            private_key,
            algorithm="ES256",
            headers={"kid": key_id},
        )
        host = (
            "https://api.sandbox.push.apple.com"
            if config.apns_sandbox
            else "https://api.push.apple.com"
        )
        notification = delivery.notification_id
        payload = {
            "aps": {
                "alert": {
                    "title": notification.name,
                    "body": notification.message,
                },
                "sound": "default",
            },
            "nsp": self._message_data(delivery),
        }
        timeout = max(1, int(config.request_timeout_seconds or 15))
        with httpx.Client(http2=True, timeout=timeout) as client:
            response = client.post(
                "%s/3/device/%s" % (host, device.push_token),
                headers={
                    "authorization": "bearer %s" % provider_token,
                    "apns-topic": topic,
                    "apns-push-type": "alert",
                    "apns-priority": "10",
                    "content-type": "application/json",
                },
                json=payload,
            )
        if response.status_code != 200:
            raise ValueError(
                "APNS send HTTP %s: %s" % (response.status_code, response.text[:1000])
            )
        return response.headers.get("apns-id")

    @api.model
    def _dispatch_custom(self, _delivery, _config):
        raise ValueError(
            _("Custom push provider has no installed delivery adapter.")
        )
