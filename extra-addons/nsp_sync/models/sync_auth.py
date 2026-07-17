# -*- coding: utf-8 -*-
import json
import logging
import os
from datetime import timedelta
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)


class NspSyncAuth(models.Model):
    _name = "nsp.sync.auth"
    _description = "NSP Sync Authentication"
    _order = "sequence, name, id"
    _rec_name = "display_name"

    display_name = fields.Char(compute="_compute_display_name", store=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    name = fields.Char(string="Name", required=True, default="Remote NSP Server")
    edge_server_id = fields.Many2one(
        "nsp.controller",
        string="Edge Server Identity",
        required=True,
        index=True,
        ondelete="restrict",
        domain="[('node_type', '=', 'edge_server')]",
        help="Edge Server node whose managed Controllers are synchronized using these remote credentials.",
    )

    remote_server_url = fields.Char(
        string="Remote Server URL",
        required=True,
        copy=False,
        help="Remote Odoo/Core API server root, for example http://localhost:8070 or http://cloud_web:8069. Do not enter a gateway route here.",
    )
    remote_base_url = fields.Char(
        string="Resolved Remote URL",
        compute="_compute_remote_base_url",
        store=True,
        readonly=True,
        help="Normalized URL derived from Remote Server URL. It is not written by Sync Jobs.",
    )
    remote_service_code = fields.Char(
        string="Resolved Remote Server Code",
        readonly=True,
        copy=False,
        index=True,
        help="Resolved from the remote Core API token response. Used internally to build /<server_code>/v1/<route>.",
    )
    client_id = fields.Char(string="Remote Client ID", required=True, copy=False)
    client_secret = fields.Char(string="Remote Client Secret", required=True, copy=False, groups="base.group_system")

    access_token = fields.Char(string="Access Token", readonly=True, copy=False, groups="base.group_system")
    refresh_token = fields.Char(string="Refresh Token", readonly=True, copy=False, groups="base.group_system")
    token_expiry = fields.Datetime(string="Token Expiry", readonly=True, copy=False)
    refresh_expiry = fields.Datetime(string="Refresh Expiry", readonly=True, copy=False)
    connected = fields.Boolean(string="Connected", readonly=True, copy=False)
    last_auth_at = fields.Datetime(string="Last Auth At", readonly=True, copy=False)
    last_error = fields.Text(string="Last Auth Error", readonly=True, copy=False)
    job_count = fields.Integer(string="Sync Jobs", compute="_compute_job_count")

    _sql_constraints = [
        ("auth_name_unique", "unique(name)", "Authentication name must be unique."),
        ("auth_remote_client_unique", "unique(remote_server_url, client_id)", "This Remote Server URL and Client ID are already configured."),
    ]

    def init(self):
        self.env.cr.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS nsp_sync_auth_active_edge_server_uniq
                ON nsp_sync_auth (edge_server_id)
             WHERE active = TRUE
        """)

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._ensure_controller_pairing_jobs()
        return records

    def _ensure_controller_pairing_jobs(self):
        Job = self.env["nsp.sync.job"].sudo()
        specifications = (
            ("nsp_gatekeeper.action_core_api_nsp_controller_pairing_requests_sync", "push", 5),
            ("nsp_gatekeeper.action_core_api_nsp_controller_pairing_decisions_sync", "pull", 5),
        )
        for auth in self.filtered(lambda item: item.active and item.edge_server_id):
            for xmlid, direction, interval in specifications:
                action = self.env.ref(xmlid, raise_if_not_found=False)
                if not action:
                    continue
                job = Job.search([
                    ("auth_id", "=", auth.id),
                    ("sync_action_id", "=", action.id),
                    ("direction", "=", direction),
                ], limit=1)
                values = {
                    "active": True,
                    "interval_seconds": interval,
                    "batch_size": 100,
                }
                if job:
                    job.write(values)
                else:
                    values.update({
                        "auth_id": auth.id,
                        "sync_action_id": action.id,
                        "direction": direction,
                    })
                    Job.create(values)
        return True

    def write(self, vals):
        protected_fields = {
            "edge_server_id", "remote_server_url", "client_id", "client_secret",
            "access_token", "refresh_token", "token_expiry", "refresh_expiry",
            "remote_service_code", "connected",
        }
        vals = dict(vals)
        vals.pop("remote_base_url", None)
        result = super().write(vals)
        if {"edge_server_id", "active"}.intersection(vals):
            self._ensure_controller_pairing_jobs()
        return result

    @api.depends("name", "remote_base_url", "client_id", "remote_service_code")
    def _compute_display_name(self):
        for rec in self:
            parts = [rec.name or "Remote NSP Server"]
            if rec.remote_base_url:
                parts.append(rec.remote_base_url)
            if rec.remote_service_code:
                parts.append(rec.remote_service_code)
            elif rec.client_id:
                parts.append(rec.client_id)
            rec.display_name = " / ".join(parts)

    @api.depends("remote_server_url")
    def _compute_remote_base_url(self):
        for rec in self:
            rec.remote_base_url = rec._normalize_remote_base_url() if rec.remote_server_url else False

    def _compute_job_count(self):
        Job = self.env["nsp.sync.job"].sudo()
        for rec in self:
            rec.job_count = Job.search_count([("auth_id", "=", rec.id)])

    @api.constrains("edge_server_id")
    def _check_edge_server_identity(self):
        for rec in self.filtered("edge_server_id"):
            if rec.edge_server_id.node_type != "edge_server":
                raise ValidationError(_("Edge Server Identity must reference a node of type Edge Server."))

    @api.constrains("remote_server_url")
    def _check_remote_server_url(self):
        for rec in self:
            if not (rec.remote_server_url or "").strip():
                raise ValidationError(_("Remote Server URL is required."))
            parsed = urlsplit(rec._normalize_remote_base_url() or "")
            if not parsed.scheme or not parsed.netloc:
                raise ValidationError(_("Remote Server URL must be a server root such as http://localhost:8070 or http://cloud_web:8069."))
            raw = rec.remote_server_url if "://" in rec.remote_server_url else "http://%s" % rec.remote_server_url
            path = (urlsplit(raw).path or "").strip("/")
            if path:
                raise ValidationError(_("Remote Server URL must be the Odoo server root. Do not enter /auth/token or gateway route paths."))

    @api.onchange("remote_server_url")
    def _onchange_remote_server_url(self):
        for rec in self:
            if rec.remote_server_url:
                rec.remote_server_url = rec._normalize_remote_base_url()

    # --------------------------- URL/auth helpers -------------------------
    def _normalize_remote_base_url(self):
        self.ensure_one()
        raw = (self.remote_server_url or "").strip()
        if not raw:
            return ""
        if "://" not in raw:
            raw = "http://%s" % raw
        parsed = urlsplit(raw)
        if not parsed.netloc:
            return raw.rstrip("/")
        return urlunsplit((parsed.scheme or "http", parsed.netloc, "", "", "")).rstrip("/")

    def _effective_remote_base_url(self):
        self.ensure_one()
        raw = self._normalize_remote_base_url()
        if not raw:
            return ""
        public_cloud_port = int(os.getenv("NSP_CLOUD_PUBLIC_PORT", "8070") or 8070)
        internal_cloud_url = os.getenv("NSP_CLOUD_INTERNAL_URL", "http://cloud_web:8069").rstrip("/")
        public_edge_port = int(os.getenv("NSP_EDGE_PUBLIC_PORT", "8069") or 8069)
        internal_edge_url = os.getenv("NSP_EDGE_INTERNAL_URL", "http://web:8069").rstrip("/")
        parsed = urlsplit(raw if "://" in raw else "http://%s" % raw)
        host = (parsed.hostname or "").lower()
        port = parsed.port
        if host in ("localhost", "127.0.0.1") and port == public_cloud_port:
            return internal_cloud_url
        if host in ("localhost", "127.0.0.1") and port == public_edge_port:
            return internal_edge_url
        return raw.rstrip("/")

    def _effective_database_name(self):
        self.ensure_one()
        send_db = self.env["ir.config_parameter"].sudo().get_param("nsp_sync.send_db_param", "1")
        if str(send_db).strip().lower() in ("0", "false", "no", "off"):
            return False
        return (self.env.cr.dbname or "").strip() or False

    def _url(self, path):
        self.ensure_one()
        base = self._effective_remote_base_url()
        if not base:
            raise UserError(_("Remote Server URL is required in NSP Sync Authentication."))
        url = urljoin(base.rstrip("/") + "/", str(path or "").lstrip("/"))
        dbname = self._effective_database_name()
        if dbname and "?" not in url:
            url = "%s?db=%s" % (url, dbname)
        elif dbname:
            url = "%s&db=%s" % (url, dbname)
        return url

    def _remote_service_code(self):
        self.ensure_one()
        code = (self.remote_service_code or "").strip().strip("/")
        if not code:
            self._authenticate_client_credentials()
            code = (self.remote_service_code or "").strip().strip("/")
        if not code:
            raise UserError(_("Remote Server Code could not be resolved from Core API token response. Authenticate again and check the remote Core API Application."))
        return code

    def gateway_url(self, route_suffix, version_code="v1"):
        self.ensure_one()
        suffix = str(route_suffix or "").strip().strip("/")
        if not suffix:
            raise UserError(_("Route Path is required for NSP Sync."))
        return self._url("/%s/%s/%s" % (self._remote_service_code(), version_code or "v1", suffix))

    def base_headers(self):
        return {"Content-Type": "application/json"}

    def _token_expiring(self, margin_seconds=60):
        self.ensure_one()
        if not self.access_token:
            return True
        if not self.token_expiry:
            return False
        return self.token_expiry <= fields.Datetime.now() + timedelta(seconds=margin_seconds)

    def _extract_remote_service_code(self, data):
        payloads = []
        if isinstance(data, dict):
            payloads.append(data)
            nested = data.get("data")
            if isinstance(nested, dict):
                payloads.append(nested)
                app = nested.get("application")
                if isinstance(app, dict):
                    payloads.append(app)
            app = data.get("application")
            if isinstance(app, dict):
                payloads.append(app)
        for payload in payloads:
            code = payload.get("service_code") or payload.get("server_code") or payload.get("gateway_service_code") or ""
            code = str(code or "").strip().strip("/")
            if code:
                return code
        return False

    def _parse_auth_response(self, data):
        self.ensure_one()
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            nested = dict(data.get("data") or {})
            for key in ("api_token", "refresh_token", "access_token", "token", "application", "service_code", "server_code"):
                if key in nested and key not in data:
                    data[key] = nested[key]
        api_token = data.get("api_token") or {}
        refresh_token = data.get("refresh_token") or {}
        access = api_token.get("token") or data.get("access_token") or data.get("token")
        refresh = refresh_token.get("token") or data.get("refresh_token")
        if not access:
            raise UserError(_("Core API token response does not contain an access token."))
        now = fields.Datetime.now()
        vals = {
            "access_token": access,
            "refresh_token": refresh or False,
            "connected": True,
            "last_auth_at": now,
            "last_error": False,
        }
        service_code = self._extract_remote_service_code(data)
        if service_code:
            vals["remote_service_code"] = service_code
        vals["token_expiry"] = now + timedelta(seconds=int(api_token.get("expires_in") or 0)) if api_token.get("expires_in") else False
        vals["refresh_expiry"] = now + timedelta(seconds=int(refresh_token.get("expires_in") or 0)) if refresh_token.get("expires_in") else False
        self.sudo().write(vals)
        return access

    def _authenticate_client_credentials(self):
        self.ensure_one()
        if not self.client_id or not self.client_secret:
            raise UserError(_("Remote Client ID and Remote Client Secret are required."))
        payload = {"grant_type": "client_credentials", "client_id": self.client_id, "client_secret": self.client_secret}
        url = self._url("/auth/token")
        try:
            response = requests.post(url, data=json.dumps(payload), headers=self.base_headers(), timeout=30)
        except requests.exceptions.RequestException as exc:
            message = _("Cannot connect to remote NSP server at %(url)s. Remote Server URL must be the Odoo server root. Detail: %(detail)s") % {"url": url, "detail": str(exc)}
            self.sudo().write({"connected": False, "last_error": message})
            raise UserError(message) from exc
        try:
            data = response.json()
        except Exception:
            data = {"status": "error", "message": response.text}
        if response.status_code >= 400 or data.get("status") == "error":
            message = data.get("message") or data.get("error") or ("HTTP %s" % response.status_code)
            self.sudo().write({"connected": False, "last_error": message})
            raise UserError(message)
        return self._parse_auth_response(data)

    def _refresh_access_token(self):
        self.ensure_one()
        if not self.refresh_token:
            return self._authenticate_client_credentials()
        payload = {"grant_type": "refresh_token", "refresh_token": self.refresh_token}
        try:
            response = requests.post(self._url("/auth/token"), data=json.dumps(payload), headers=self.base_headers(), timeout=30)
            data = response.json()
        except Exception:
            return self._authenticate_client_credentials()
        if response.status_code >= 400 or data.get("status") == "error":
            return self._authenticate_client_credentials()
        return self._parse_auth_response(data)

    def get_access_token(self, force=False):
        self.ensure_one()
        if force or self._token_expiring():
            return self._refresh_access_token()
        return self.access_token

    def sync_headers(self):
        self.ensure_one()
        token = self.get_access_token()
        headers = self.base_headers()
        headers["Authorization"] = "Bearer %s" % token
        headers["X-NSP-Core-Application"] = self.client_id or ""
        return headers

    def action_authenticate(self):
        for rec in self:
            rec.get_access_token(force=True)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"title": _("NSP Sync"), "message": _("Remote Core API authentication completed."), "type": "success", "sticky": False},
        }

    def action_view_jobs(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Sync Jobs"),
            "res_model": "nsp.sync.job",
            "view_mode": "list,form",
            "domain": [("auth_id", "=", self.id)],
            "context": {"default_auth_id": self.id},
        }
