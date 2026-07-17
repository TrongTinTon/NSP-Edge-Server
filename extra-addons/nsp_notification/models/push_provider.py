# -*- coding: utf-8 -*-
import json
import logging
import time

from odoo import api, fields, models, _

_logger = logging.getLogger(__name__)


class NspPushProvider(models.Model):
    _name = "nsp.push.provider"
    _description = "NSP Push Provider"
    _order = "sequence, name, id"

    name = fields.Char(required=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True, index=True)
    provider_type = fields.Selection([
        ("fcm", "Firebase Cloud Messaging"),
        ("apns", "Apple Push Notification service"),
        ("hms", "Huawei Push Kit"),
        ("in_app", "In-App Only"),
    ], string="Provider", required=True, default="fcm", index=True)
    environment = fields.Selection([
        ("sandbox", "Sandbox"),
        ("production", "Production"),
    ], default="production", required=True)
    state = fields.Selection([
        ("draft", "Draft"),
        ("ready", "Ready"),
        ("disabled", "Disabled"),
    ], default="draft", required=True, index=True)

    # Generic / FCM
    project_id = fields.Char(string="Project ID")
    auth_mode = fields.Selection([
        ("server_key", "Server Key"),
        ("bearer_token", "Bearer Token"),
        ("service_account", "Service Account"),
    ], default="server_key", string="Auth Mode")
    endpoint_url = fields.Char(string="Endpoint URL")
    secret_param_key = fields.Char(
        string="Secret Config Parameter",
        help="Name of ir.config_parameter containing the provider secret/token. The secret itself is not stored on this record.",
    )

    # APNs token-based metadata. Private key remains in ir.config_parameter.
    apns_team_id = fields.Char(string="APNs Team ID")
    apns_key_id = fields.Char(string="APNs Key ID")
    apns_bundle_id = fields.Char(string="APNs Topic / Bundle ID")

    timeout_sec = fields.Integer(default=10)
    last_error = fields.Char(readonly=True)
    last_success_at = fields.Datetime(readonly=True)
    last_failure_at = fields.Datetime(readonly=True)
    note = fields.Text()

    _sql_constraints = [
        ("provider_name_unique", "unique(name)", "Push provider name must be unique."),
    ]

    def action_mark_ready(self):
        self.write({"state": "ready", "active": True})
        return True

    def action_disable(self):
        self.write({"state": "disabled", "active": False})
        return True

    def _get_secret(self):
        self.ensure_one()
        if not self.secret_param_key:
            return False
        return self.env["ir.config_parameter"].sudo().get_param(self.secret_param_key)

    def _default_endpoint(self):
        self.ensure_one()
        if self.endpoint_url:
            return self.endpoint_url
        if self.provider_type == "fcm":
            if self.auth_mode in ("bearer_token", "service_account") and self.project_id:
                return "https://fcm.googleapis.com/v1/projects/%s/messages:send" % self.project_id
            return "https://fcm.googleapis.com/fcm/send"
        if self.provider_type == "apns":
            host = "api.sandbox.push.apple.com" if self.environment == "sandbox" else "api.push.apple.com"
            return "https://%s/3/device/{token}" % host
        return self.endpoint_url or ""

    def _build_notification_payload(self, delivery):
        notification = delivery.notification_id
        title = notification.name or _("NSP Notification")
        message = notification.message or title
        data = {
            "notification_id": str(notification.id),
            "notification_type": notification.notification_type or "system_alert",
            "severity": notification.severity or "info",
            "event_time": fields.Datetime.to_string(notification.event_time) if notification.event_time else "",
        }
        return title, message, data

    def send_delivery(self, delivery):
        self.ensure_one()
        if self.state != "ready" or not self.active:
            return False, "provider_disabled", _("Provider is not ready or inactive.")
        if self.provider_type == "in_app":
            delivery.notification_id._push_realtime_bus()
            return True, "in_app", False
        if self.provider_type == "fcm":
            return self._send_fcm(delivery)
        if self.provider_type == "apns":
            return self._send_apns(delivery)
        if self.provider_type == "hms":
            return False, "hms_not_configured", _("Huawei Push Kit connector is not configured in this module.")
        return False, "unsupported_provider", _("Unsupported push provider.")

    def _send_fcm(self, delivery):
        self.ensure_one()
        try:
            import requests
        except Exception:
            return False, "missing_requests", _("Python requests package is required for FCM sending.")
        token = delivery.device_id.push_token
        if not token:
            return False, "missing_device_token", _("Device push token is empty.")
        secret = self._get_secret()
        if not secret:
            return False, "missing_provider_secret", _("Provider secret config parameter is empty.")
        title, message, data = self._build_notification_payload(delivery)
        headers = {"Content-Type": "application/json"}
        endpoint = self._default_endpoint()
        if self.auth_mode == "server_key":
            headers["Authorization"] = "key=%s" % secret
            payload = {
                "to": token,
                "priority": "high" if delivery.priority in ("high", "emergency") else "normal",
                "notification": {"title": title, "body": message},
                "data": data,
            }
        else:
            headers["Authorization"] = "Bearer %s" % secret
            payload = {
                "message": {
                    "token": token,
                    "notification": {"title": title, "body": message},
                    "data": data,
                    "android": {"priority": "HIGH" if delivery.priority in ("high", "emergency") else "NORMAL"},
                }
            }
        try:
            resp = requests.post(endpoint, headers=headers, data=json.dumps(payload), timeout=max(self.timeout_sec or 10, 1))
            if 200 <= resp.status_code < 300:
                message_id = ""
                try:
                    body = resp.json()
                    message_id = body.get("name") or body.get("message_id") or body.get("multicast_id") or ""
                except Exception:
                    message_id = ""
                return True, str(message_id or "fcm"), False
            return False, "fcm_http_%s" % resp.status_code, (resp.text or "")[:512]
        except Exception as exc:
            return False, "fcm_exception", str(exc)[:512]

    def _send_apns(self, delivery):
        self.ensure_one()
        token = delivery.device_id.push_token
        if not token:
            return False, "missing_device_token", _("Device push token is empty.")
        private_key = self._get_secret()
        if not private_key:
            return False, "missing_apns_key", _("APNs private key config parameter is empty.")
        if not self.apns_team_id or not self.apns_key_id or not self.apns_bundle_id:
            return False, "missing_apns_metadata", _("APNs Team ID, Key ID and Bundle ID are required.")
        try:
            import jwt
        except Exception:
            return False, "missing_pyjwt", _("PyJWT with ES256 support is required for APNs token authentication.")
        try:
            import httpx
        except Exception:
            return False, "missing_httpx", _("httpx with HTTP/2 support is required for APNs sending.")
        title, message, data = self._build_notification_payload(delivery)
        try:
            auth_token = jwt.encode(
                {"iss": self.apns_team_id, "iat": int(time.time())},
                private_key,
                algorithm="ES256",
                headers={"alg": "ES256", "kid": self.apns_key_id},
            )
            if isinstance(auth_token, bytes):
                auth_token = auth_token.decode("utf-8")
            endpoint = self._default_endpoint().format(token=token)
            headers = {
                "authorization": "bearer %s" % auth_token,
                "apns-topic": self.apns_bundle_id,
                "apns-push-type": "alert",
                "apns-priority": "10" if delivery.priority in ("high", "emergency") else "5",
            }
            payload = {
                "aps": {
                    "alert": {"title": title, "body": message},
                    "sound": "default",
                },
                "nsp": data,
            }
            with httpx.Client(http2=True, timeout=max(self.timeout_sec or 10, 1)) as client:
                resp = client.post(endpoint, headers=headers, json=payload)
            if 200 <= resp.status_code < 300:
                return True, resp.headers.get("apns-id", "apns"), False
            return False, "apns_http_%s" % resp.status_code, (resp.text or "")[:512]
        except Exception as exc:
            return False, "apns_exception", str(exc)[:512]

    def action_test_provider(self):
        self.ensure_one()
        self.write({"last_error": False})
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Provider saved"),
                "message": _("Provider configuration is saved. Send a real delivery from Push Deliveries to test external provider connectivity."),
                "type": "success",
                "sticky": False,
            },
        }
