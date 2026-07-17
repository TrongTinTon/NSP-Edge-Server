# -*- coding: utf-8 -*-
"""Core API application bootstrap for NSP Notification clients.

External notification consumers (mobile apps, parking monitor/kiosk/browser)
must authenticate through Core API Application.  This module owns notification
endpoint definitions, while t4_coreapi remains the owner of applications,
tokens, rate limits, allowed IPs and gateway routes.
"""

import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class CoreApiApplication(models.Model):
    _inherit = "core.api.application"

    application_kind = fields.Selection(
        selection_add=[
            ("nsp_mobile", "NSP Mobile App"),
            ("parking_monitor", "Parking Monitor"),
        ],
        ondelete={
            "nsp_mobile": "set default",
            "parking_monitor": "set default",
        },
    )

    @api.model
    def _ensure_nsp_notification_applications(self):
        """Create/update official Core API Applications for external notification clients.

        Design rule:
        - Mobile app, kiosk, browser monitor and any external screen must use
          /auth/token + /<service_code>/v1/<route>.
        - nsp_notification only provides endpoint actions and payloads.
        - t4_coreapi owns Application, credentials, route authorization and logs.

        We intentionally create two separate applications so their allowed routes,
        rate limits and IP allowlists can be managed independently:
        - NSP Mobile App       -> mobile/* routes only
        - NSP Parking Monitor  -> parking-monitor/* routes only

        Secrets generated during module upgrade are not shown to the user session;
        admins should use Regenerate Secret on the Core API Application form before
        deploying a real external client.
        """
        Application = self.sudo()
        Endpoint = self.env["core.api.endpoint"].sudo()
        Version = self.env["core.api.version"].sudo()
        Action = self.env["ir.actions.core_api"].sudo()
        Domain = self.env["core.api.domain"].sudo()

        version = self.env.ref("t4_coreapi.core_api_version_v1", raise_if_not_found=False)
        if not version:
            version = Version.get_default_version()
        manager = self.env.ref(
            "nsp_notification.action_endpoint_manager_nsp_notification",
            raise_if_not_found=False,
        )
        domain = self.env.ref("t4_coreapi.core_api_domain_default", raise_if_not_found=False)
        if not domain:
            domain = Domain.get_default()

        if not version or not manager:
            _logger.warning(
                "NSP Notification Core API applications were not bootstrapped: missing version or endpoint manager."
            )
            return False

        specs = [
            {
                "name": "NSP Mobile App",
                "service_code": "nsp-mobile",
                "kind": "nsp_mobile",
                "rate_limit": 120,
                "auth_rate_limit": 20,
                "notes": (
                    "Official Core API Application for NSP mobile app notification clients. "
                    "Allowed routes are limited to mobile/push/* and mobile/notifications/*."
                ),
                "actions": [
                    "nsp_notification.action_core_api_nsp_mobile_push_register_token",
                    "nsp_notification.action_core_api_nsp_mobile_push_unregister_token",
                    "nsp_notification.action_core_api_nsp_mobile_push_heartbeat",
                    "nsp_notification.action_core_api_nsp_mobile_notifications_list",
                    "nsp_notification.action_core_api_nsp_mobile_notifications_ack",
                    "nsp_notification.action_core_api_nsp_mobile_notifications_read",
                    "nsp_notification.action_core_api_nsp_mobile_notifications_read_all",
                ],
            },
            {
                "name": "NSP Parking Monitor",
                "service_code": "parking-monitor",
                "kind": "parking_monitor",
                "rate_limit": 300,
                "auth_rate_limit": 10,
                "notes": (
                    "Official Core API Application for parking monitor, kiosk or browser displays. "
                    "Allowed routes are limited to parking-monitor/*."
                ),
                "actions": [
                    "nsp_notification.action_core_api_nsp_parking_monitor_events",
                ],
            },
        ]

        for spec in specs:
            app = Application.search([("service_code", "=", spec["service_code"])], limit=1)
            vals = {
                "name": spec["name"],
                "application_kind": spec["kind"],
                "service_code": spec["service_code"],
                "rate_limit_per_minute": spec["rate_limit"],
                "auth_rate_limit_per_minute": spec["auth_rate_limit"],
                "notes": spec["notes"],
            }
            if domain:
                vals["domain_id"] = domain.id

            if app:
                # Keep admin-controlled state/credentials. Only normalize metadata/routes.
                app.write(vals)
            else:
                vals["state"] = "active"
                app = Application.create(vals)
                # The autogenerated secret cannot be displayed during module upgrade.
                # Admins must click Regenerate Secret before using this application.
                app.write({"credentials_pending": False})

            allowed_codes = set()
            for xmlid in spec["actions"]:
                action = self.env.ref(xmlid, raise_if_not_found=False)
                if not action:
                    continue
                action = Action.browse(action.id).exists()
                if not action:
                    continue
                code = action.endpoint_code or action.name
                allowed_codes.add(code)
                route_vals = {
                    "name": action.name,
                    "code": code,
                    "version_id": version.id,
                    "route_suffix": (action.route_suffix or "").strip("/"),
                    "http_methods": action.http_methods or "POST",
                    "action_id": action.id,
                    "application_id": app.id,
                    "endpoint_manager_id": manager.id,
                    "route_active": True,
                }
                endpoint = Endpoint.search([
                    ("application_id", "=", app.id),
                    ("version_id", "=", version.id),
                    ("route_suffix", "=", route_vals["route_suffix"]),
                ], limit=1)
                if endpoint:
                    endpoint.write(route_vals)
                else:
                    Endpoint.create(route_vals)

            # Enforce separation between Mobile App and Parking Monitor routes for
            # the default applications managed by this module. Manually created
            # applications are left untouched.
            extra_routes = Endpoint.search([
                ("application_id", "=", app.id),
                ("version_id", "=", version.id),
                ("endpoint_manager_id", "=", manager.id),
                ("code", "not in", list(allowed_codes or {"__none__"})),
            ])
            if extra_routes:
                extra_routes.write({"route_active": False})

        return True
