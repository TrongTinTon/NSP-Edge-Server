# -*- coding: utf-8 -*-
import json

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class NspNotificationProviderConfig(models.Model):
    _name = "nsp.notification.provider.config"
    _description = "NSP Mobile Push Provider Configuration"
    _rec_name = "name"

    name = fields.Char(required=True, default="Mobile Push Providers")
    active = fields.Boolean(default=True)

    fcm_enabled = fields.Boolean(string="Enable FCM")
    fcm_project_id = fields.Char(string="Firebase Project ID")
    fcm_service_account_json = fields.Text(
        string="FCM Service Account JSON",
        groups="base.group_system",
        help="Service-account JSON used to mint short-lived OAuth 2.0 tokens for FCM HTTP v1.",
    )

    apns_enabled = fields.Boolean(string="Enable APNS")
    apns_team_id = fields.Char(string="Apple Team ID")
    apns_key_id = fields.Char(string="APNS Key ID")
    apns_topic = fields.Char(
        string="APNS Topic / Bundle ID",
        help="Usually the iOS application bundle identifier.",
    )
    apns_private_key = fields.Text(
        string="APNS Private Key (.p8)",
        groups="base.group_system",
    )
    apns_sandbox = fields.Boolean(string="Use APNS Sandbox")

    max_attempts = fields.Integer(default=5, required=True)
    retry_delay_seconds = fields.Integer(default=60, required=True)
    request_timeout_seconds = fields.Integer(default=15, required=True)

    _sql_constraints = [
        ("notification_max_attempts_positive", "CHECK(max_attempts > 0)", "Max Attempts must be greater than zero."),
        ("notification_retry_delay_positive", "CHECK(retry_delay_seconds > 0)", "Retry Delay must be greater than zero."),
        ("notification_timeout_positive", "CHECK(request_timeout_seconds > 0)", "Request Timeout must be greater than zero."),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        if self.sudo().search_count([]):
            raise ValidationError(_("Only one Mobile Push Provider Configuration is supported."))
        if len(vals_list) > 1:
            raise ValidationError(_("Only one Mobile Push Provider Configuration is supported."))
        return super().create(vals_list)

    @api.model
    def get_active_config(self):
        return self.sudo().search([("active", "=", True)], order="id", limit=1)

    @api.constrains("fcm_enabled", "fcm_project_id", "fcm_service_account_json")
    def _check_fcm_configuration(self):
        for rec in self:
            if not rec.fcm_enabled:
                continue
            if not (rec.fcm_project_id or "").strip():
                raise ValidationError(_("Firebase Project ID is required when FCM is enabled."))
            raw = (rec.fcm_service_account_json or "").strip()
            if not raw:
                raise ValidationError(_("FCM Service Account JSON is required when FCM is enabled."))
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError) as exc:
                raise ValidationError(_("FCM Service Account JSON is invalid.")) from exc
            required = {"client_email", "private_key", "token_uri"}
            missing = sorted(required - set(payload))
            if missing:
                raise ValidationError(
                    _("FCM Service Account JSON is missing: %s") % ", ".join(missing)
                )

    @api.constrains(
        "apns_enabled", "apns_team_id", "apns_key_id", "apns_topic", "apns_private_key"
    )
    def _check_apns_configuration(self):
        for rec in self:
            if not rec.apns_enabled:
                continue
            missing = [
                label
                for label, value in (
                    (_("Apple Team ID"), rec.apns_team_id),
                    (_("APNS Key ID"), rec.apns_key_id),
                    (_("APNS Topic / Bundle ID"), rec.apns_topic),
                    (_("APNS Private Key"), rec.apns_private_key),
                )
                if not (value or "").strip()
            ]
            if missing:
                raise ValidationError(
                    _("Missing APNS configuration: %s") % ", ".join(missing)
                )
