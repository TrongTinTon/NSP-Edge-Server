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

# T4 Core API route prefix used by NSP Edge-to-Cloud APIs.
# It is an internal protocol constant, not user-managed connection data.
CLOUD_API_PREFIX = "EdgeServer"


class NspSyncAuth(models.Model):
    _name = "nsp.sync.auth"
    _description = "NSP Cloud Connection"
    _order = "sequence, name, id"
    _rec_name = "display_name"

    display_name = fields.Char(compute="_compute_display_name", store=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    name = fields.Char(string="Name", required=True, default="NSP Cloud")
    edge_server_code = fields.Char(
        string="Edge Server Code",
        required=True,
        copy=False,
        index=True,
        help="Code assigned to this Edge Server by the Cloud Server.",
    )
    remote_server_url = fields.Char(
        string="Cloud Server URL",
        required=True,
        copy=False,
        help="Cloud Odoo/Core API server root, for example https://cloud.example.com. Do not enter /auth/token or an API route.",
    )
    remote_base_url = fields.Char(
        string="Resolved Cloud URL",
        compute="_compute_remote_base_url",
        store=True,
        readonly=True,
    )
    client_id = fields.Char(string="Core API Client ID", required=True, copy=False)
    client_secret = fields.Char(
        string="Core API Secret",
        required=True,
        copy=False,
        groups="base.group_system",
    )

    access_token = fields.Char(readonly=True, copy=False, groups="base.group_system")
    refresh_token = fields.Char(readonly=True, copy=False, groups="base.group_system")
    token_expiry = fields.Datetime(readonly=True, copy=False)
    refresh_expiry = fields.Datetime(readonly=True, copy=False)
    connected = fields.Boolean(readonly=True, copy=False)
    last_auth_at = fields.Datetime(string="Last Authentication", readonly=True, copy=False)
    last_error = fields.Text(string="Last Error", readonly=True, copy=False)
    job_count = fields.Integer(string="Sync Jobs", compute="_compute_job_count")

    _sql_constraints = [
        (
            "auth_remote_client_unique",
            "unique(remote_server_url, client_id)",
            "This Cloud Server URL and Client ID are already configured.",
        ),
    ]

    def _deployment_role(self):
        role = (
            self.env["ir.config_parameter"].sudo().get_param("nsp.deployment_role")
            or os.getenv("NSP_DEPLOYMENT_ROLE")
            or os.getenv("NSP_SERVER_ROLE")
            or ""
        ).strip().lower()
        return role if role in ("cloud", "edge_server") else "edge_server"

    def _ensure_edge_server_instance(self):
        if self._deployment_role() != "edge_server":
            raise UserError(_("Cloud Connections and outbound Sync Jobs are configured only on the Edge Server."))

    def _ensure_edge_server_node(self, previous_code=False):
        """Create or rename the local Edge Server identity from its assigned code."""
        EdgeServer = self.env["nsp.edge.server"].sudo().with_context(active_test=False)
        for rec in self:
            code = str(rec.edge_server_code or "").strip().upper()
            if not code:
                continue
            edge = EdgeServer.search([("edge_server_code", "=", code)], limit=1)
            if edge:
                if not edge.active:
                    edge.write({"active": True})
                continue
            old_code = str(previous_code or "").strip().upper()
            old_edge = EdgeServer.search([("edge_server_code", "=", old_code)], limit=1) if old_code else EdgeServer.browse()
            other_auth = self.search_count([("id", "!=", rec.id), ("edge_server_code", "=", old_code)]) if old_code else 0
            if old_edge and not other_auth:
                old_edge.write({
                    "edge_server_code": code,
                    "name": old_edge.name or ("Edge Server %s" % code),
                    "active": True,
                })
            else:
                EdgeServer.create({
                    "edge_server_code": code,
                    "name": "Edge Server %s" % code,
                    "status": "offline",
                    "active": True,
                })
        return True

    @api.model_create_multi
    def create(self, vals_list):
        self._ensure_edge_server_instance()
        prepared = []
        for source in vals_list:
            vals = dict(source)
            vals["edge_server_code"] = str(vals.get("edge_server_code") or "").strip().upper()
            vals.setdefault("name", "NSP Cloud")
            prepared.append(vals)
        records = super().create(prepared)
        records._ensure_edge_server_node()
        self.env["nsp.sync.job"].ensure_default_jobs(records)
        return records

    def write(self, vals):
        protected_fields = {
            "edge_server_code", "remote_server_url", "client_id", "client_secret",
            "access_token", "refresh_token", "token_expiry", "refresh_expiry",
            "connected",
        }
        if protected_fields.intersection(vals):
            self._ensure_edge_server_instance()
        values = dict(vals)
        values.pop("remote_base_url", None)
        old_codes = {rec.id: rec.edge_server_code for rec in self}
        if "edge_server_code" in values:
            values["edge_server_code"] = str(values.get("edge_server_code") or "").strip().upper()
        result = super().write(values)
        if "edge_server_code" in values:
            for rec in self:
                rec._ensure_edge_server_node(previous_code=old_codes.get(rec.id))
        return result

    @api.depends("edge_server_code", "remote_base_url")
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = " / ".join(filter(None, [
                rec.edge_server_code or "Edge Server",
                rec.remote_base_url or "NSP Cloud",
            ]))

    @api.depends("remote_server_url")
    def _compute_remote_base_url(self):
        for rec in self:
            rec.remote_base_url = rec._normalize_remote_base_url() if rec.remote_server_url else False

    def _compute_job_count(self):
        Job = self.env["nsp.sync.job"].sudo()
        for rec in self:
            rec.job_count = Job.search_count([("auth_id", "=", rec.id)])

    @api.constrains("edge_server_code")
    def _check_edge_server_code(self):
        for rec in self:
            code = str(rec.edge_server_code or "").strip()
            if not code:
                raise ValidationError(_("Edge Server Code is required."))
            if code != code.upper():
                raise ValidationError(_("Edge Server Code must be uppercase."))

    @api.constrains("remote_server_url")
    def _check_remote_server_url(self):
        for rec in self:
            if not (rec.remote_server_url or "").strip():
                raise ValidationError(_("Cloud Server URL is required."))
            parsed = urlsplit(rec._normalize_remote_base_url() or "")
            if not parsed.scheme or not parsed.netloc:
                raise ValidationError(_("Cloud Server URL must be a server root such as https://cloud.example.com."))
            raw = rec.remote_server_url if "://" in rec.remote_server_url else "http://%s" % rec.remote_server_url
            if (urlsplit(raw).path or "").strip("/"):
                raise ValidationError(_("Cloud Server URL must be the server root. Do not enter /auth/token or an API route."))

    @api.onchange("remote_server_url")
    def _onchange_remote_server_url(self):
        for rec in self:
            if rec.remote_server_url:
                rec.remote_server_url = rec._normalize_remote_base_url()

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

    @staticmethod
    def _running_in_container():
        """Best-effort container detection used only for loopback URL handling."""
        if os.path.exists("/.dockerenv"):
            return True
        try:
            content = ""
            for filename in ("/proc/1/cgroup", "/proc/self/cgroup"):
                if os.path.exists(filename):
                    with open(filename, "r", encoding="utf-8", errors="ignore") as stream:
                        content += stream.read().lower()
            return any(marker in content for marker in ("docker", "containerd", "kubepods", "podman"))
        except OSError:
            return False

    @staticmethod
    def _is_loopback_hostname(hostname):
        return str(hostname or "").strip().lower() in {"localhost", "127.0.0.1", "::1"}

    def _replace_url_hostname(self, base_url, hostname):
        self.ensure_one()
        parsed = urlsplit(base_url)
        if not parsed.netloc:
            return base_url
        host = str(hostname or "").strip()
        if not host:
            return base_url
        userinfo = ""
        if parsed.username:
            userinfo = parsed.username
            if parsed.password:
                userinfo += ":%s" % parsed.password
            userinfo += "@"
        port = ":%s" % parsed.port if parsed.port else ""
        if ":" in host and not host.startswith("["):
            host = "[%s]" % host
        netloc = "%s%s%s" % (userinfo, host, port)
        return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment)).rstrip("/")

    def _connection_base_candidates(self):
        """Return reachable URL candidates without embedding Docker topology in stored data.

        A loopback address inside an Edge container points to that container, not to the
        Docker host. Docker Desktop exposes the host through ``host.docker.internal``.
        The configured URL is always attempted first so localhost still works for
        non-container and host-network deployments.
        """
        self.ensure_one()
        configured = self._normalize_remote_base_url().rstrip("/")
        candidates = [configured] if configured else []
        parsed = urlsplit(configured)
        if self._running_in_container() and self._is_loopback_hostname(parsed.hostname):
            aliases = []
            explicit = (
                os.getenv("NSP_CLOUD_DOCKER_HOST")
                or self.env["ir.config_parameter"].sudo().get_param("nsp_sync.docker_host_alias")
                or "host.docker.internal"
            )
            aliases.extend(part.strip() for part in str(explicit).split(",") if part.strip())
            for alias in aliases:
                candidate = self._replace_url_hostname(configured, alias)
                if candidate and candidate not in candidates:
                    candidates.append(candidate)
        return candidates

    def _effective_remote_base_url(self):
        self.ensure_one()
        return self._normalize_remote_base_url().rstrip("/")

    def _effective_database_name(self):
        self.ensure_one()
        send_db = self.env["ir.config_parameter"].sudo().get_param("nsp_sync.send_db_param", "1")
        if str(send_db).strip().lower() in ("0", "false", "no", "off"):
            return False
        return (self.env.cr.dbname or "").strip() or False

    def _url(self, path, base_url=None):
        self.ensure_one()
        base = str(base_url or self._effective_remote_base_url()).rstrip("/")
        if not base:
            raise UserError(_("Cloud Server URL is required."))
        url = urljoin(base.rstrip("/") + "/", str(path or "").lstrip("/"))
        dbname = self._effective_database_name()
        if dbname:
            url = "%s%sdb=%s" % (url, "&" if "?" in url else "?", dbname)
        return url

    def gateway_url(self, route_suffix, version_code="v1"):
        self.ensure_one()
        suffix = str(route_suffix or "").strip().strip("/")
        if not suffix:
            raise UserError(_("Route is required for NSP Sync."))
        version = str(version_code or "v1").strip().strip("/") or "v1"
        return self._url("/%s/%s/%s" % (CLOUD_API_PREFIX, version, suffix))

    def base_headers(self):
        return {"Content-Type": "application/json"}

    def _token_expiring(self, margin_seconds=60):
        self.ensure_one()
        if not self.access_token:
            return True
        if not self.token_expiry:
            return False
        return self.token_expiry <= fields.Datetime.now() + timedelta(seconds=margin_seconds)

    def _parse_auth_response(self, data):
        self.ensure_one()
        data = dict(data or {})
        if isinstance(data.get("data"), dict):
            nested = dict(data["data"])
            for key in ("api_token", "refresh_token", "access_token", "token", "application"):
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
            "token_expiry": now + timedelta(seconds=int(api_token.get("expires_in") or 0)) if api_token.get("expires_in") else False,
            "refresh_expiry": now + timedelta(seconds=int(refresh_token.get("expires_in") or 0)) if refresh_token.get("expires_in") else False,
        }
        self.sudo().write(vals)
        return access

    def _authenticate_client_credentials(self):
        self.ensure_one()
        if not self.client_id or not self.client_secret:
            raise UserError(_("Core API Client ID and Core API Secret are required."))
        payload = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        attempts = []
        response = None
        successful_base = False
        last_exception = None
        candidates = self._connection_base_candidates()
        for base_url in candidates:
            url = self._url("/auth/token", base_url=base_url)
            try:
                response = requests.post(url, data=json.dumps(payload), headers=self.base_headers(), timeout=30)
                successful_base = base_url
                break
            except requests.exceptions.RequestException as exc:
                last_exception = exc
                attempts.append("%s (%s)" % (url, exc))

        if response is None:
            configured = self._effective_remote_base_url()
            parsed = urlsplit(configured)
            docker_hint = ""
            if self._running_in_container() and self._is_loopback_hostname(parsed.hostname):
                docker_hint = _(
                    " In Docker, localhost points to the Edge container. Use the Cloud container service name "
                    "(for example http://cloud-web:8069), the Cloud host LAN IP, or "
                    "http://host.docker.internal:%(port)s when Cloud is published on the Docker host."
                ) % {"port": parsed.port or 80}
            detail = "; ".join(attempts) or str(last_exception or "Connection failed")
            message = _("Cannot connect to Cloud Server. Attempts: %(detail)s.%(hint)s") % {
                "detail": detail,
                "hint": docker_hint,
            }
            self.sudo().write({"connected": False, "last_error": message})
            raise UserError(message) from last_exception

        try:
            data = response.json()
        except Exception:
            data = {"status": "error", "message": response.text}
        if response.status_code >= 400 or data.get("status") == "error":
            message = data.get("message") or data.get("error") or ("HTTP %s" % response.status_code)
            self.sudo().write({"connected": False, "last_error": message})
            raise UserError(message)

        configured_base = self._effective_remote_base_url()
        if successful_base and successful_base != configured_base:
            # Persist the URL that is actually reachable so token refresh and all sync jobs
            # use the same route instead of retrying an invalid container-local address.
            self.sudo().write({"remote_server_url": successful_base})
        return self._parse_auth_response(data)

    def _refresh_access_token(self):
        self.ensure_one()
        if not self.refresh_token:
            return self._authenticate_client_credentials()
        try:
            response = requests.post(
                self._url("/auth/token"),
                data=json.dumps({"grant_type": "refresh_token", "refresh_token": self.refresh_token}),
                headers=self.base_headers(),
                timeout=30,
            )
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
        headers = self.base_headers()
        headers.update({
            "Authorization": "Bearer %s" % self.get_access_token(),
            "X-NSP-Core-Application": self.client_id or "",
            "X-Edge-Server-Code": self.edge_server_code or "",
        })
        return headers

    def action_authenticate(self):
        self._ensure_edge_server_instance()
        self._ensure_edge_server_node()
        for rec in self:
            rec.get_access_token(force=True)
        created_jobs = self.env["nsp.sync.job"].ensure_default_jobs(self)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("NSP Sync"),
                "message": _(
                    "Cloud Core API authentication completed. %(count)s missing Sync Job(s) were created."
                ) % {"count": len(created_jobs)},
                "type": "success",
                "sticky": False,
            },
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
