# -*- coding: utf-8 -*-
import json
import logging
import os
from datetime import datetime, timedelta, timezone

import requests

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

SYNC_ROUTE_DIRECTIONS = {
    "edge-server/status": "push",
    "device-whitelist/sync": "pull",
    "branches/sync": "pull",
    "employees/sync": "pull",
    "vehicle-config/sync": "pull",
    "vehicles/sync": "pull",
    "cards/sync": "pull",
    "vehicle-borrow/sync": "pull",
    "parking-config/sync": "pull",
    "measurement-config/sync": "pull",
    "measurement-events/sync": "push",
    "measurement-status/sync": "push",
    "parking-transactions/sync": "push",
}
NSP_SYNC_ALLOWED_ROUTES = tuple(SYNC_ROUTE_DIRECTIONS)
JOB_SEQUENCE = {route: sequence * 10 for sequence, route in enumerate(NSP_SYNC_ALLOWED_ROUTES, start=1)}
DEFAULT_JOB_SETTINGS = {
    "edge-server/status": {"schedule_interval_minutes": 1, "batch_size": 1},
    "device-whitelist/sync": {"schedule_interval_minutes": 1, "batch_size": 1000},
    "branches/sync": {"schedule_interval_minutes": 5, "batch_size": 500},
    "employees/sync": {"schedule_interval_minutes": 5, "batch_size": 500},
    "vehicle-config/sync": {"schedule_interval_minutes": 5, "batch_size": 1000},
    "vehicles/sync": {"schedule_interval_minutes": 5, "batch_size": 500},
    "cards/sync": {"schedule_interval_minutes": 5, "batch_size": 1000},
    "vehicle-borrow/sync": {"schedule_interval_minutes": 5, "batch_size": 500},
    "parking-config/sync": {"schedule_interval_minutes": 5, "batch_size": 100},
    "measurement-config/sync": {"schedule_interval_minutes": 1, "batch_size": 100},
    "measurement-events/sync": {"schedule_interval_minutes": 1, "batch_size": 100},
    "measurement-status/sync": {"schedule_interval_minutes": 1, "batch_size": 100},
    "parking-transactions/sync": {"schedule_interval_minutes": 1, "batch_size": 200},
}
ACTION_KINDS = {
    "edge-server/status": "edge_server_status",
    "device-whitelist/sync": "device_whitelist",
    "branches/sync": "branch",
    "employees/sync": "user",
    "vehicle-config/sync": "vehicle_config",
    "vehicles/sync": "vehicle",
    "cards/sync": "card",
    "vehicle-borrow/sync": "vehicle_borrow",
    "parking-config/sync": "parking_config",
    "measurement-config/sync": "measurement_config",
    "measurement-events/sync": "measurement_event",
    "measurement-status/sync": "measurement_status",
    "parking-transactions/sync": "parking_transaction",
}

class NspSyncJob(models.Model):
    _name = "nsp.sync.job"
    _description = "NSP Sync Job"
    _order = "sequence, sync_action_name, id"
    _rec_name = "display_name"

    display_name = fields.Char(compute="_compute_display_name", store=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)

    auth_id = fields.Many2one(
        "nsp.sync.auth", string="Cloud Connection", required=True, index=True, ondelete="restrict"
    )
    sync_action_id = fields.Many2one(
        "ir.actions.core_api",
        string="Sync API",
        required=True,
        domain=[
            ("endpoint_manager_id", "!=", False),
            ("endpoint_code", "!=", False),
            ("route_suffix", "in", list(NSP_SYNC_ALLOWED_ROUTES)),
        ],
        ondelete="restrict",
    )
    version_id = fields.Many2one(
        "core.api.version",
        string="API Version",
        default=lambda self: self.env["core.api.version"].get_default_version(),
        required=True,
    )
    sync_action_code = fields.Char(compute="_compute_action_meta", store=True, index=True)
    sync_action_name = fields.Char(compute="_compute_action_meta", store=True, index=True)
    route_suffix = fields.Char(string="Route", compute="_compute_action_meta", store=True)
    direction = fields.Selection(
        [("pull", "Pull from Cloud"), ("push", "Push to Cloud")],
        required=True,
        default="pull",
        index=True,
    )
    schedule_interval_minutes = fields.Integer(default=1, required=True, string="Schedule Interval (Minutes)", help="Fallback retry interval. Measurement Events and status are forwarded immediately; this schedule is used only when immediate forwarding fails.")
    batch_size = fields.Integer(default=100, required=True)
    sync_cursor = fields.Char(
        string="Pull Cursor",
        readonly=True,
        copy=False,
        help="Internal cursor for incremental Pull jobs. It is preserved after the last page and is not user-managed.",
    )
    last_push_at = fields.Datetime(readonly=True)
    last_push_record_id = fields.Integer(readonly=True, copy=False)
    last_pull_at = fields.Datetime(readonly=True)
    next_run_at = fields.Datetime(readonly=True, index=True)
    status = fields.Selection(
        [
            ("idle", "Idle"),
            ("running", "Running"),
            ("success", "Success"),
            ("failed", "Failed"),
            ("disabled", "Disabled"),
        ],
        default="idle",
        readonly=True,
        index=True,
    )
    last_message = fields.Text(readonly=True)

    edge_server_code = fields.Char(
        related="auth_id.edge_server_code", readonly=True, store=True, index=True
    )
    nsp_remote_base_url = fields.Char(related="auth_id.remote_base_url", readonly=True)
    nsp_connected = fields.Boolean(related="auth_id.connected", readonly=True)
    nsp_last_error = fields.Text(related="auth_id.last_error", readonly=True)

    _sql_constraints = [
        ("interval_positive", "CHECK(schedule_interval_minutes >= 1)", "Schedule Interval (Minutes) must be at least 1."),
        ("batch_positive", "CHECK(batch_size >= 1)", "Batch Size must be at least 1."),
        (
            "job_unique",
            "unique(sync_action_id, auth_id, direction)",
            "Only one Sync Job is allowed per API, Cloud Connection, and direction.",
        ),
    ]

    @api.depends("sync_action_name", "direction", "schedule_interval_minutes", "auth_id", "auth_id.display_name")
    def _compute_display_name(self):
        labels = dict(self._fields["direction"].selection)
        for rec in self:
            rec.display_name = "%s / %s / %s / %s min" % (
                rec.auth_id.display_name or "Cloud",
                rec.sync_action_name or rec.route_suffix or "-",
                labels.get(rec.direction, rec.direction or "-"),
                rec.schedule_interval_minutes or 0,
            )

    @api.depends(
        "sync_action_id",
        "sync_action_id.endpoint_code",
        "sync_action_id.name",
        "sync_action_id.route_suffix",
    )
    def _compute_action_meta(self):
        for rec in self:
            action = rec.sync_action_id
            rec.sync_action_code = action.endpoint_code if action else False
            rec.sync_action_name = action.name if action else False
            rec.route_suffix = action.route_suffix if action else False

    def _deployment_role(self):
        role = (
            self.env["ir.config_parameter"].sudo().get_param("nsp.deployment_role")
            or os.getenv("NSP_DEPLOYMENT_ROLE")
            or os.getenv("NSP_SERVER_ROLE")
            or "edge_server"
        ).strip().lower()
        return role if role in ("cloud", "edge_server") else "edge_server"

    def _ensure_edge_server_instance(self):
        if self._deployment_role() != "edge_server":
            raise UserError(_("Outbound Sync Jobs run only on the Edge Server."))

    @api.model
    def ensure_default_jobs(self, auth_records):
        """Create the supported job set for each Cloud Connection exactly once."""
        auth_records = auth_records.exists()
        if not auth_records:
            return self.browse()
        self._ensure_edge_server_instance()
        obsolete_jobs = self.search([
            ("auth_id", "in", auth_records.ids),
            ("sync_action_id.route_suffix", "=", "devices-status/sync"),
        ])
        if obsolete_jobs:
            obsolete_jobs.unlink()
        Action = self.env["ir.actions.core_api"].sudo()
        Version = self.env["core.api.version"].sudo()
        version = Version.get_default_version()
        if not version:
            raise UserError(_("A default Core API Version is required before creating Sync Jobs."))

        actions = Action.search([
            ("endpoint_manager_id", "!=", False),
            ("endpoint_code", "!=", False),
            ("route_suffix", "in", list(NSP_SYNC_ALLOWED_ROUTES)),
        ])
        action_by_route = {}
        for action in actions.sorted(key=lambda rec: rec.id):
            route = str(action.route_suffix or "").strip().strip("/")
            action_by_route.setdefault(route, action)

        missing_routes = [route for route in NSP_SYNC_ALLOWED_ROUTES if route not in action_by_route]
        if missing_routes:
            raise UserError(
                _("Missing NSP Core API endpoint definitions: %s") % ", ".join(missing_routes)
            )

        created = self.browse()
        now = fields.Datetime.now()
        for auth in auth_records:
            existing_routes = set(
                self.search([("auth_id", "=", auth.id)]).mapped("route_suffix")
            )
            vals_list = []
            for route in NSP_SYNC_ALLOWED_ROUTES:
                if route in existing_routes:
                    continue
                settings = DEFAULT_JOB_SETTINGS[route]
                vals_list.append({
                    "sequence": JOB_SEQUENCE[route],
                    "auth_id": auth.id,
                    "sync_action_id": action_by_route[route].id,
                    "version_id": version.id,
                    "direction": SYNC_ROUTE_DIRECTIONS[route],
                    "schedule_interval_minutes": settings["schedule_interval_minutes"],
                    "batch_size": settings["batch_size"],
                    "next_run_at": now,
                    "active": True,
                })
            if vals_list:
                created |= self.create(vals_list)
            existing_jobs = self.search([("auth_id", "=", auth.id)])
            for job in existing_jobs:
                route = (job.route_suffix or "").strip().strip("/")
                values = {}
                if route in JOB_SEQUENCE and job.sequence != JOB_SEQUENCE[route]:
                    values["sequence"] = JOB_SEQUENCE[route]
                if route in DEFAULT_JOB_SETTINGS:
                    settings = DEFAULT_JOB_SETTINGS[route]
                    if job.schedule_interval_minutes < 1:
                        values["schedule_interval_minutes"] = settings["schedule_interval_minutes"]
                    if job.batch_size < 1:
                        values["batch_size"] = settings["batch_size"]
                if values:
                    job.write(values)
        return created

    @api.onchange("sync_action_id")
    def _onchange_sync_action(self):
        for rec in self:
            route = (rec.sync_action_id.route_suffix or "").strip().strip("/") if rec.sync_action_id else ""
            if route in SYNC_ROUTE_DIRECTIONS:
                rec.direction = SYNC_ROUTE_DIRECTIONS[route]

    @api.constrains("sync_action_id", "direction")
    def _check_sync_actions(self):
        for rec in self:
            route = (rec.route_suffix or "").strip().strip("/")
            if route not in NSP_SYNC_ALLOWED_ROUTES:
                raise ValidationError(_("Route %s is not supported by NSP Sync.") % (route or "-"))
            expected = SYNC_ROUTE_DIRECTIONS[route]
            if rec.direction != expected:
                raise ValidationError(
                    _("Route %(route)s must use direction %(direction)s.")
                    % {"route": route, "direction": expected}
                )

    @api.model_create_multi
    def create(self, vals_list):
        self._ensure_edge_server_instance()
        Action = self.env["ir.actions.core_api"].sudo()
        prepared = []
        for source in vals_list:
            vals = dict(source)
            action = Action.browse(vals.get("sync_action_id")).exists() if vals.get("sync_action_id") else Action.browse()
            route = (action.route_suffix or "").strip().strip("/") if action else ""
            if route in SYNC_ROUTE_DIRECTIONS:
                vals["direction"] = SYNC_ROUTE_DIRECTIONS[route]
            vals["schedule_interval_minutes"] = max(1, int(vals.get("schedule_interval_minutes") or 1))
            vals["batch_size"] = max(1, min(int(vals.get("batch_size") or 100), 1000))
            vals.setdefault("next_run_at", fields.Datetime.now())
            prepared.append(vals)
        return super().create(prepared)

    def write(self, vals):
        if {"auth_id", "sync_action_id", "direction", "active", "schedule_interval_minutes", "batch_size"}.intersection(vals):
            self._ensure_edge_server_instance()
        values = dict(vals)
        if "sync_action_id" in values:
            action = self.env["ir.actions.core_api"].sudo().browse(values["sync_action_id"]).exists()
            route = (action.route_suffix or "").strip().strip("/") if action else ""
            if route in SYNC_ROUTE_DIRECTIONS:
                values["direction"] = SYNC_ROUTE_DIRECTIONS[route]
            values["sync_cursor"] = False
            values["last_push_at"] = False
            values["last_push_record_id"] = 0
        if "schedule_interval_minutes" in values:
            values["schedule_interval_minutes"] = max(1, int(values.get("schedule_interval_minutes") or 1))
        if "batch_size" in values:
            values["batch_size"] = max(1, min(int(values.get("batch_size") or 1), 1000))
        return super().write(values)

    # --------------------------- remote API ---------------------------
    def _auth(self):
        self.ensure_one()
        if not self.auth_id:
            raise UserError(_("Select a Cloud Connection."))
        return self.auth_id

    def _nsp_gateway_url(self, route_suffix, version_code="v1"):
        self.ensure_one()
        return self._auth().gateway_url(route_suffix, version_code=version_code)

    def nsp_sync_headers(self):
        self.ensure_one()
        return self._auth().sync_headers()

    def action_authenticate_application(self):
        for rec in self:
            rec._auth().action_authenticate()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("NSP Sync"),
                "message": _("Cloud authentication completed."),
                "type": "success",
                "sticky": False,
            },
        }

    def _schedule_next(self, immediate=False):
        now = fields.Datetime.now()
        for rec in self:
            rec.next_run_at = (
                now if immediate and rec.active
                else now + timedelta(minutes=max(1, rec.schedule_interval_minutes or 1)) if rec.active
                else False
            )

    def _post_remote(self, sync_action, payload=None, timeout=60):
        self.ensure_one()
        if not sync_action:
            raise UserError(_("Sync API is required."))
        route = (sync_action.route_suffix or "").strip().strip("/")
        if route not in NSP_SYNC_ALLOWED_ROUTES:
            raise UserError(_("Route %s is not an NSP Sync route.") % route)
        version_code = self.version_id.code if self.version_id else "v1"
        url = self._nsp_gateway_url(route, version_code=version_code)
        try:
            return requests.post(
                url,
                json=payload or {},
                headers=self.nsp_sync_headers(),
                timeout=timeout,
            )
        except requests.exceptions.RequestException as exc:
            raise UserError(
                _("Cannot call Cloud NSP API at %(url)s: %(detail)s")
                % {"url": url, "detail": str(exc)}
            ) from exc

    def _json_or_error(self, response):
        try:
            data = response.json()
        except Exception:
            data = {"success": False, "error": response.text}
        if not isinstance(data, dict):
            raise UserError(_("Cloud API returned an invalid response."))
        ok = data.get("success", data.get("ok", data.get("status") == "success"))
        if response.status_code >= 400 or not ok:
            raise UserError(data.get("error") or data.get("message") or ("HTTP %s" % response.status_code))
        if isinstance(data.get("data"), dict):
            merged = dict(data["data"])
            for key, value in data.items():
                merged.setdefault(key, value)
            return merged
        return data

    def _action_kind(self):
        self.ensure_one()
        return ACTION_KINDS.get((self.route_suffix or "").strip().strip("/"), "unsupported")

    @api.model
    def _dt(self, value):
        return fields.Datetime.to_string(value) if value else False

    @api.model
    def _iso_utc(self, value):
        if not value:
            return False
        parsed = fields.Datetime.to_datetime(value)
        if not parsed:
            return False
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()

    @api.model
    def _remote_datetime(self, value):
        if value in (None, ""):
            return False
        if isinstance(value, datetime):
            parsed = value
        else:
            try:
                parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
            except Exception as exc:
                raise UserError(_("Invalid datetime value: %s") % value) from exc
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return fields.Datetime.to_string(parsed)

    # --------------------------- local identity -----------------------
    def _edge_server_record(self):
        self.ensure_one()
        return self.env["nsp.edge.server"].sudo().with_context(active_test=False).search(
            [("edge_server_code", "=", self.edge_server_code)], limit=1
        )

    def _require_edge_server_record(self):
        self.ensure_one()
        edge = self._edge_server_record()
        if not edge:
            self.auth_id._ensure_edge_server_node()
            edge = self._edge_server_record()
        if not edge:
            raise UserError(_("Edge Server Code %s is not configured.") % self.edge_server_code)
        return edge

    # --------------------------- push payloads ------------------------
    def _serialize_edge_server_status(self):
        """Serialize one complete Edge runtime snapshot without server management codes."""
        self.ensure_one()
        edge = self._require_edge_server_record()
        controllers = []
        for controller in edge.controller_ids.filtered("active").sorted(
            key=lambda record: (record.controller_id or "", record.id)
        ):
            devices = []
            for device in controller.device_ids.sorted(
                key=lambda record: (record.serial_number or "", record.id)
            ):
                status = str(device.status or "offline").lower()
                devices.append({
                    "serial_number": device.serial_number or "",
                    "antennas": sorted(int(number) for number in device.antennas_ids.mapped("antenna_no")),
                    "device_status": status if status in ("online", "offline", "degraded") else "offline",
                    "last_seen_at": self._dt(device.last_seen) if device.last_seen else False,
                    **({"firmware_version": device.firmware_version} if device.firmware_version else {}),
                })
            controller_status = str(controller.status or "offline").lower()
            controllers.append({
                "controller_code": controller.controller_id or "",
                "current_status": controller_status,
                "last_seen_at": self._dt(controller.timestamp) if controller.timestamp else False,
                "devices": devices,
            })
        return {
            "record_key": self.edge_server_code,
            "edge_server_code": self.edge_server_code,
            "current_status": "online",
            "last_seen_at": self._dt(fields.Datetime.now()),
            "controllers": controllers,
        }

    @api.model
    def _serialize_parking_transaction(self, record):
        decision = record.status if record.status in ("allowed", "denied") else "denied"
        antenna = record.antenna_id
        device = antenna.device_id if antenna else self.env["nsp.device"].browse()
        parking_area = record.lane_id.parking_area_id if record.lane_id else self.env["nsp.parking.area"].browse()
        payload = {
            "record_key": record.transaction_uid,
            "transaction_uid": record.transaction_uid,
            "controller_code": record.controller_id.controller_id if record.controller_id else "",
            "parking_area_code": parking_area.code if parking_area else "",
            "lane_code": record.lane_id.code if record.lane_id else "",
            "serial_number": device.serial_number if device else "",
            "antenna_no": int(antenna.antenna_no) if antenna else 0,
            "event_type": record.event_type,
            "event_time": self._dt(record.event_time),
            "vehicle_tid": record.vehicle_tid or "",
            "user_tid": record.user_tid or "",
            "decision": decision,
        }
        if decision == "denied":
            payload["decision_reason_code"] = record.error_code or "unknown"
            if record.error_message:
                payload["decision_message"] = record.error_message
        return payload

    def _push_cursor_domain(self):
        self.ensure_one()
        if not self.last_push_at:
            return []
        return [
            "|",
            ("write_date", ">", self.last_push_at),
            "&",
            ("write_date", "=", self.last_push_at),
            ("id", ">", int(self.last_push_record_id or 0)),
        ]

    def _serialize_push_batch(self, kind):
        self.ensure_one()
        limit = max(1, min(int(self.batch_size or 100), 1000))
        if kind == "edge_server_status":
            return {
                "items": [self._serialize_edge_server_status()],
                "cursor_at": fields.Datetime.now(),
                "cursor_id": 0,
                "has_more": False,
            }
        edge = self._require_edge_server_record()
        domain = self._push_cursor_domain()
        if kind == "parking_transaction":
            records = self.env["nsp.parking.transaction"].sudo().search(
                domain + [("controller_id.edge_server_id", "=", edge.id)],
                order="write_date asc, id asc",
                limit=limit + 1,
            )
            serializer = self._serialize_parking_transaction
        else:
            raise UserError(_("Unsupported push route: %s") % self.route_suffix)
        has_more = len(records) > limit
        selected = records[:limit]
        last = selected[-1:] if selected else selected
        return {
            "items": [serializer(record) for record in selected],
            "cursor_at": last.write_date if last else self.last_push_at,
            "cursor_id": last.id if last else self.last_push_record_id,
            "has_more": has_more,
        }

    @api.model
    def _remote_push_item(self, item):
        payload = dict(item or {})
        payload.pop("record_key", None)
        return payload

    def _build_push_payload(self, items):
        self.ensure_one()
        route = (self.route_suffix or "").strip().strip("/")
        base = {"edge_server_code": self.edge_server_code}
        if route == "edge-server/status":
            base.update(self._remote_push_item(items[0] if items else self._serialize_edge_server_status()))
            return base
        base["items"] = [self._remote_push_item(item) for item in items]
        return base

    # --------------------------- pull application ---------------------
    @api.model
    def _card(self, tid, card_type):
        tid = str(tid or "").strip().upper().replace(" ", "")
        if not tid:
            return self.env["nsp.rfid.card"].browse()
        Card = self.env["nsp.rfid.card"].sudo()
        card = Card.search([("tid", "=", tid)], limit=1)
        vals = {"tid": tid, "card_type": card_type}
        if card:
            card.write(vals)
            return card
        return Card.create(vals)

    def _find_or_create_controller(self, code, name=False):
        self.ensure_one()
        code = str(code or "").strip().upper()
        if not code:
            return self.env["nsp.controller"].browse()
        edge = self._require_edge_server_record()
        Controller = self.env["nsp.controller"].sudo().with_context(active_test=False)
        controller = Controller.search([("controller_id", "=", code)], limit=1)
        vals = {
            "controller_name": name or (controller.controller_name if controller else code),
            "edge_server_id": edge.id,
            "active": True,
        }
        if controller:
            controller.write(vals)
            return controller
        vals["controller_id"] = code
        return Controller.create(vals)

    @api.model
    def _apply_device_whitelist(self, item):
        serial = str(item.get("serial_number") or "").strip().upper()
        if not serial:
            raise UserError(_("Device Whitelist Serial is required."))
        device_type = str(item.get("device_type") or "rfid_reader").strip().lower()
        if device_type not in ("rfid_reader", "camera", "other"):
            raise UserError(_("Invalid Device Type: %s") % device_type)
        Whitelist = self.env["nsp.device.whitelist"].sudo()
        record = Whitelist.search([("serial_number", "=", serial)], limit=1)
        vals = {
            "serial_number": serial,
            "model_number": str(item.get("model_number") or "").strip() or False,
            "device_vendor": str(item.get("vendor") or item.get("device_vendor") or "").strip() or False,
            "device_type": device_type,
        }
        if record:
            changed = {name: value for name, value in vals.items() if record[name] != value}
            if changed:
                record.write(changed)
            return record
        return Whitelist.create(vals)

    @api.model
    def _reconcile_device_whitelist_snapshot(self, items):
        serials = {
            str(item.get("serial_number") or "").strip().upper()
            for item in (items or [])
            if isinstance(item, dict) and str(item.get("serial_number") or "").strip()
        }
        Whitelist = self.env["nsp.device.whitelist"].sudo()
        stale = Whitelist.search([("serial_number", "not in", list(serials))]) if serials else Whitelist.search([])
        if stale:
            stale.unlink()
        return len(stale)

    @api.model
    def _apply_branch(self, item):
        code = str(item.get("branch_code") or "").strip().upper()
        if not code:
            raise UserError(_("Branch Code is required."))
        Branch = self.env["nsp.branch"].sudo().with_context(active_test=False)
        branch = Branch.search([("code", "=", code)], limit=1)
        vals = {
            "code": code,
            "name": item.get("branch_name") or code,
            "timezone": item.get("timezone") or "Asia/Ho_Chi_Minh",
            "status": "active" if bool(item.get("active", True)) else "inactive",
        }
        if branch:
            branch.write(vals)
            return branch
        return Branch.create(vals)

    @api.model
    def _apply_user(self, item):
        code = str(item.get("user_code") or "").strip()
        if not code:
            raise UserError(_("User Code is required."))
        User = self.env["nsp.user"].sudo().with_context(active_test=False)
        user = User.search([("user_code", "=", code)], limit=1)
        vals = {"user_code": code, "name": item.get("name") or code, "active": bool(item.get("active", True))}
        if user:
            user.write(vals)
            return user
        return User.create(vals)

    @api.model
    def _upsert_vehicle_master(self, model_name, item, extra_vals=None):
        if not isinstance(item, dict):
            raise UserError(_("Vehicle Configuration items must be objects."))
        code = str(item.get("code") or "").strip().upper()
        name = str(item.get("name") or "").strip()
        if not code or not name:
            raise UserError(_("Vehicle Configuration Code and Name are required."))
        Model = self.env[model_name].sudo().with_context(active_test=False)
        record = Model.search([("code", "=", code)], limit=1)
        vals = {
            "code": code,
            "name": name,
            "active": bool(item.get("active", True)),
        }
        vals.update(extra_vals or {})
        if record:
            changed = {field_name: value for field_name, value in vals.items() if record[field_name] != value}
            if changed:
                record.write(changed)
            return record
        return Model.create(vals)

    @api.model
    def _reconcile_vehicle_master_snapshot(self, model_name, incoming_codes):
        Model = self.env[model_name].sudo().with_context(active_test=False)
        codes = [str(code).strip().upper() for code in incoming_codes if str(code or "").strip()]
        domain = [("code", "not in", codes)] if codes else []
        stale_active = Model.search(domain + [("active", "=", True)])
        if stale_active:
            stale_active.write({"active": False})
        return len(stale_active)

    def _apply_vehicle_config_snapshot(self, data, request_payload=False):
        self.ensure_one()
        if not isinstance(data, dict):
            raise UserError(_("Vehicle Configuration response must be an object."))
        groups = {
            "vehicle_types": data.get("vehicle_types") or [],
            "brands": data.get("brands") or [],
            "models": data.get("models") or [],
            "colors": data.get("colors") or [],
        }
        for group_name, values in groups.items():
            if not isinstance(values, list):
                raise UserError(_("Vehicle Configuration field %s must be an array.") % group_name)

        results = []
        applied = []
        codes = {key: [] for key in groups}
        with self.env.cr.savepoint():
            for item in groups["vehicle_types"]:
                record = self._upsert_vehicle_master("nsp.vehicle.type", item)
                codes["vehicle_types"].append(record.code)
                results.append(record)
                applied.append(("vehicle_type", item, record))
            for item in groups["brands"]:
                record = self._upsert_vehicle_master("nsp.vehicle.brand", item)
                codes["brands"].append(record.code)
                results.append(record)
                applied.append(("brand", item, record))
            Brand = self.env["nsp.vehicle.brand"].sudo().with_context(active_test=False)
            for item in groups["models"]:
                brand_code = str(item.get("brand_code") or "").strip().upper()
                brand = Brand.search([("code", "=", brand_code)], limit=1) if brand_code else Brand.browse()
                if brand_code and not brand:
                    raise UserError(_("Vehicle Brand %s was not found for Vehicle Model %s.") % (brand_code, item.get("code") or "-"))
                record = self._upsert_vehicle_master(
                    "nsp.vehicle.model",
                    item,
                    extra_vals={"brand_id": brand.id if brand else False},
                )
                codes["models"].append(record.code)
                results.append(record)
                applied.append(("model", item, record))
            for item in groups["colors"]:
                record = self._upsert_vehicle_master("nsp.vehicle.color", item)
                codes["colors"].append(record.code)
                results.append(record)
                applied.append(("color", item, record))

            removed = {
                "vehicle_types": self._reconcile_vehicle_master_snapshot("nsp.vehicle.type", codes["vehicle_types"]),
                "brands": self._reconcile_vehicle_master_snapshot("nsp.vehicle.brand", codes["brands"]),
                "models": self._reconcile_vehicle_master_snapshot("nsp.vehicle.model", codes["models"]),
                "colors": self._reconcile_vehicle_master_snapshot("nsp.vehicle.color", codes["colors"]),
            }

        Record = self.env["nsp.sync.record"].sudo()
        for group_name, item, record in applied:
            Record.mark_result(
                sync_job=self,
                action_code=self.sync_action_code,
                action_name=self.sync_action_name,
                route_suffix=self.route_suffix,
                record=record,
                record_key="%s:%s" % (group_name, record.code),
                status="synced",
                message="Applied Vehicle Configuration snapshot.",
                payload=request_payload,
                response=item,
                operation="pull",
            )
        return results, removed

    @api.model
    def _vehicle_master_by_code(self, model_name, code, label):
        normalized = str(code or "").strip().upper()
        if not normalized:
            return self.env[model_name].browse()
        record = self.env[model_name].sudo().with_context(active_test=False).search(
            [("code", "=", normalized)], limit=1
        )
        if not record:
            raise UserError(_("%(label)s %(code)s was not found. Run vehicle-config/sync first.") % {
                "label": label,
                "code": normalized,
            })
        return record

    @api.model
    def _find_vehicle(self, code):
        code = str(code or "").strip()
        Vehicle = self.env["nsp.vehicle"].sudo().with_context(active_test=False)
        if not code:
            return Vehicle.browse()
        for field_name in ("vehicle_code", "code"):
            if field_name in Vehicle._fields:
                record = Vehicle.search([(field_name, "=", code)], limit=1)
                if record:
                    return record
        return Vehicle.search([("license_plate", "=", code)], limit=1)

    @api.model
    def _apply_vehicle(self, item):
        code = str(item.get("vehicle_code") or item.get("license_plate") or "").strip()
        plate = str(item.get("license_plate") or code).strip()
        if not code or not plate:
            raise UserError(_("Vehicle Code and License Plate are required."))
        vehicle = self._find_vehicle(code)
        owner_user_code = str(item.get("owner_user_code") or "").strip()
        owner = (
            self.env["nsp.user"].sudo().search(
                [("user_code", "=", owner_user_code)], limit=1
            )
            if owner_user_code else self.env["nsp.user"].browse()
        )
        if not owner:
            owner = self._apply_user({
                "user_code": owner_user_code or ("OWNER-%s" % code),
                "name": owner_user_code or code,
                "active": True,
            })
        vehicle_type = self._vehicle_master_by_code("nsp.vehicle.type", item.get("vehicle_type_code"), _("Vehicle Type"))
        brand = self._vehicle_master_by_code("nsp.vehicle.brand", item.get("brand_code"), _("Vehicle Brand"))
        vehicle_model = self._vehicle_master_by_code("nsp.vehicle.model", item.get("model_code"), _("Vehicle Model"))
        color = self._vehicle_master_by_code("nsp.vehicle.color", item.get("color_code"), _("Vehicle Color"))
        if vehicle_model and brand and vehicle_model.brand_id and vehicle_model.brand_id != brand:
            raise UserError(_("Vehicle Model %(model)s does not belong to Brand %(brand)s.") % {
                "model": vehicle_model.code,
                "brand": brand.code,
            })
        vals = {
            "license_plate": plate,
            "owner_id": owner.id,
            "vehicle_type_id": vehicle_type.id if vehicle_type else False,
            "brand_id": brand.id if brand else False,
            "model_id": vehicle_model.id if vehicle_model else False,
            "color_id": color.id if color else False,
            "state": "approved" if bool(item.get("active", True)) else "pending",
        }
        for field_name in ("vehicle_code", "code"):
            if field_name in self.env["nsp.vehicle"]._fields:
                vals[field_name] = code
                break
        if vehicle:
            vehicle.write(vals)
            return vehicle
        return self.env["nsp.vehicle"].sudo().create(vals)

    @api.model
    def _card_assignment_values(self, item):
        assignment = item.get("assignment")
        if not isinstance(assignment, dict):
            raise UserError(_("Card assignment must be an object."))
        assignment_type = str(assignment.get("type") or "unassigned").strip().lower()
        assignment_code = str(assignment.get("code") or "").strip()
        if assignment_type not in ("unassigned", "user", "vehicle"):
            raise UserError(
                _("Invalid Card assignment type: %s") % (assignment_type or "-")
            )
        if assignment_type != "unassigned" and not assignment_code:
            raise UserError(_("Assigned Card requires assignment.code."))
        assigned_at = (
            self._remote_datetime(item.get("assigned_at"))
            if item.get("assigned_at") else False
        )
        return assignment_type, assignment_code, assigned_at

    @api.model
    def _apply_card_master(self, item):
        if not isinstance(item, dict):
            raise UserError(_("Cards snapshot items must be objects."))
        supported_fields = {
            "card_uid", "card_type", "active", "assigned", "assignment", "assigned_at",
        }
        unsupported_fields = sorted(set(item) - supported_fields)
        if unsupported_fields:
            raise UserError(
                _("Unsupported Card field(s): %s") % ", ".join(unsupported_fields)
            )
        tid = str(item.get("card_uid") or "").strip().upper().replace(" ", "")
        card_type = str(item.get("card_type") or "").strip().lower()
        if not tid:
            raise UserError(_("Card UID is required."))
        if card_type not in ("vehicle_card", "user_card"):
            raise UserError(_("Invalid Card Type for %s.") % tid)
        assignment_type, _assignment_code, _assigned_at = self._card_assignment_values(item)
        expected_type = {
            "user": "user_card",
            "vehicle": "vehicle_card",
        }.get(assignment_type)
        if expected_type and card_type != expected_type:
            raise UserError(
                _("Card %(tid)s type %(card_type)s does not match %(assignment_type)s assignment.")
                % {"tid": tid, "card_type": card_type, "assignment_type": assignment_type}
            )
        return self._card(tid, card_type)

    @api.model
    def _apply_card_assignment(self, card, item):
        """Create the concrete User Card or Vehicle Card assignment on Edge."""
        assignment_type, assignment_code, assigned_at = self._card_assignment_values(item)
        UserLine = self.env["nsp.user.card"].sudo().with_context(active_test=False)
        VehicleLine = self.env["nsp.vehicle.card"].sudo().with_context(active_test=False)
        user_lines = UserLine.search([("card_id", "=", card.id)])
        vehicle_lines = VehicleLine.search([("card_id", "=", card.id)])

        if not bool(item.get("active", True)) or assignment_type == "unassigned":
            active_user = user_lines.filtered(lambda line: line.state == "active")
            active_vehicle = vehicle_lines.filtered(lambda line: line.state == "active")
            if active_user:
                active_user.action_revoke()
            if active_vehicle:
                active_vehicle.action_revoke()
            return card

        if assignment_type == "user":
            owner = self.env["nsp.user"].sudo().with_context(active_test=False).search(
                [("user_code", "=", assignment_code)], limit=1
            )
            if not owner:
                raise UserError(
                    _("Card %(tid)s owner User %(code)s was not found. Run employees/sync first.")
                    % {"tid": card.tid, "code": assignment_code}
                )
            other_active = user_lines.filtered(
                lambda line: line.state == "active" and line.user_id != owner
            )
            if other_active:
                other_active.action_revoke()
            active_vehicle = vehicle_lines.filtered(lambda line: line.state == "active")
            if active_vehicle:
                active_vehicle.action_revoke()
            line = user_lines.filtered(lambda value: value.user_id == owner)[:1]
            vals = {
                "user_id": owner.id,
                "card_id": card.id,
                "state": "active",
                "revoked_at": False,
            }
            if assigned_at:
                vals["assigned_at"] = assigned_at
            if line:
                line.write(vals)
                return line
            return UserLine.create(vals)

        owner = self._find_vehicle(assignment_code)
        if not owner:
            raise UserError(
                _("Card %(tid)s owner Vehicle %(code)s was not found. Run vehicles/sync first.")
                % {"tid": card.tid, "code": assignment_code}
            )
        other_active = vehicle_lines.filtered(
            lambda line: line.state == "active" and line.vehicle_id != owner
        )
        if other_active:
            other_active.action_revoke()
        active_user = user_lines.filtered(lambda line: line.state == "active")
        if active_user:
            active_user.action_revoke()
        line = vehicle_lines.filtered(lambda value: value.vehicle_id == owner)[:1]
        vals = {
            "vehicle_id": owner.id,
            "card_id": card.id,
            "state": "active",
            "revoked_at": False,
        }
        if assigned_at:
            vals["assigned_at"] = assigned_at
        if line:
            line.write(vals)
            return line
        return VehicleLine.create(vals)

    def _apply_card_snapshot(self, data, request_payload=False):
        """Apply the complete Card snapshot atomically in two phases.

        Phase 1 creates/updates every Master Card. Phase 2 creates the concrete
        User Card / Vehicle Card records. A missing owner rolls back the whole
        snapshot, so Edge never exposes a partially assigned card set.
        """
        self.ensure_one()
        items = self._items_from_response(data)
        if not isinstance(items, list):
            raise UserError(_("Cards snapshot must contain an items array."))
        normalized_tids = []
        seen = set()
        master_by_tid = {}
        assignment_records = []
        counts = {"master_cards": 0, "user_cards": 0, "vehicle_cards": 0, "unassigned_cards": 0}

        with self.env.cr.savepoint():
            for item in items:
                card = self._apply_card_master(item)
                if card.tid in seen:
                    raise UserError(_("Duplicate Card UID in snapshot: %s") % card.tid)
                seen.add(card.tid)
                normalized_tids.append(card.tid)
                master_by_tid[card.tid] = (card, item)

            for tid in normalized_tids:
                card, item = master_by_tid[tid]
                assignment_record = self._apply_card_assignment(card, item)
                assignment_type, _assignment_code, _assigned_at = self._card_assignment_values(item)
                if not bool(item.get("active", True)):
                    assignment_type = "unassigned"
                counts["master_cards"] += 1
                counts[{
                    "user": "user_cards",
                    "vehicle": "vehicle_cards",
                    "unassigned": "unassigned_cards",
                }[assignment_type]] += 1
                assignment_records.append((card, assignment_record, item, assignment_type))

            Card = self.env["nsp.rfid.card"].sudo()
            stale = Card.search([("tid", "not in", normalized_tids)]) if normalized_tids else Card.search([])
            removed = len(stale)
            if stale:
                stale.unlink()

        Record = self.env["nsp.sync.record"].sudo()
        for card, assignment_record, item, assignment_type in assignment_records:
            record = assignment_record if assignment_record._name in ("nsp.user.card", "nsp.vehicle.card") else card
            Record.mark_result(
                sync_job=self,
                action_code=self.sync_action_code,
                action_name=self.sync_action_name,
                route_suffix=self.route_suffix,
                record=record,
                record_key=card.tid,
                status="synced",
                message=(
                    "Created/updated User Card assignment." if assignment_type == "user"
                    else "Created/updated Vehicle Card assignment." if assignment_type == "vehicle"
                    else "Master Card synchronized without an active assignment."
                ),
                payload=request_payload,
                response=item,
                operation="pull",
            )
        return counts, removed

    @api.model
    def _apply_vehicle_borrow(self, item):
        code = str(item.get("borrow_uid") or "").strip()
        if not code:
            raise UserError(_("Borrow UID is required."))
        Borrow = self.env["nsp.vehicle.borrow.request"].sudo()
        borrow = Borrow.search([("borrow_code", "=", code)], limit=1)
        vehicle = self._find_vehicle(item.get("vehicle_code"))
        borrower = self.env["nsp.user"].sudo().search([("user_code", "=", item.get("borrower_user_code"))], limit=1)
        if not vehicle or not borrower:
            raise UserError(_("Vehicle and borrower must exist before Vehicle Borrow sync."))
        valid_from = self._remote_datetime(item.get("valid_from")) or fields.Datetime.now()
        valid_to = self._remote_datetime(item.get("valid_to")) or fields.Datetime.to_string(
            fields.Datetime.to_datetime(valid_from) + timedelta(days=1)
        )
        if fields.Datetime.to_datetime(valid_to) <= fields.Datetime.to_datetime(valid_from):
            raise UserError(_("Vehicle Borrow valid_to must be later than valid_from."))
        is_active = bool(item.get("active", True))
        vals = {
            "vehicle_id": vehicle.id,
            "borrower_id": borrower.id,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "state": "approved" if is_active else "returned",
            "returned_at": False if is_active else fields.Datetime.now(),
        }
        if borrow:
            borrow.write(vals)
            return borrow
        vals["borrow_code"] = code
        return Borrow.create(vals)

    def _find_or_create_device(self, controller, serial_number):
        self.ensure_one()
        serial = str(serial_number or "").strip().upper()
        if not serial:
            raise UserError(_("Reader Serial Number is required."))
        Device = self.env["nsp.device"].sudo()
        device = Device.search([("serial_number", "=", serial)], limit=1)
        vals = {
            "controller_id": controller.id,
        }
        if device:
            device.write(vals)
            return device
        vals["serial_number"] = serial
        return Device.create(vals)

    def _apply_measurement_config(self, item):
        self.ensure_one()
        code = str(item.get("measurement_code") or "").strip().upper()
        controller_code = str(item.get("controller_code") or "").strip().upper()
        if not code or not controller_code:
            raise UserError(_("Measurement Code and Controller Code are required."))
        status = str(item.get("status") or "ready").strip().lower()
        if status not in ("ready", "running", "completed", "failed", "cancelled"):
            raise UserError(_("Invalid Measurement Session status: %s") % status)
        controller = self._find_or_create_controller(controller_code)
        Session = self.env["nsp.measurement.session"].sudo().with_context(measurement_sync=True)
        session = Session.search([("measurement_code", "=", code)], limit=1)
        vals = {
            "measurement_code": code,
            "controller_id": controller.id,
            "status": status,
            "planned_start_at": self._remote_datetime(item.get("planned_start_at")),
            "planned_end_at": self._remote_datetime(item.get("planned_end_at")),
            "note": str(item.get("note") or "").strip() or False,
        }

        antenna_refs = self.env["nsp.device.antenna"].sudo().browse()
        seen = set()
        for device_item in item.get("measurement_antennas") or []:
            if not isinstance(device_item, dict):
                raise UserError(_("Measurement Antennas must contain objects."))
            serial = str(device_item.get("serial_number") or "").strip().upper()
            numbers = device_item.get("antennas")
            if not serial or not isinstance(numbers, list) or not numbers:
                raise UserError(_("Each Measurement Reader requires serial_number and antennas."))
            device = self._find_or_create_device(controller, serial)
            for raw_number in numbers:
                try:
                    antenna_no = int(raw_number)
                except Exception as exc:
                    raise UserError(_("Invalid Measurement Antenna number.")) from exc
                key = (serial, antenna_no)
                if antenna_no <= 0 or key in seen:
                    raise UserError(_("Invalid or duplicate Measurement Antenna %s/%s.") % (serial, raw_number))
                seen.add(key)
                Antenna = self.env["nsp.device.antenna"].sudo()
                antenna = Antenna.search([
                    ("device_id", "=", device.id),
                    ("antenna_no", "=", antenna_no),
                ], limit=1)
                if not antenna:
                    antenna = Antenna.create({
                        "device_id": device.id,
                        "antenna_no": antenna_no,
                    })
                antenna_refs |= antenna
        if not antenna_refs:
            raise UserError(_("Measurement Configuration has no antennas."))
        vals["antenna_ids"] = [(6, 0, antenna_refs.ids)]
        if session:
            session.write(vals)
        else:
            session = Session.create(vals)
        return session

    def _apply_parking_config(self, item):
        self.ensure_one()

        def write_changed(record, values):
            changes = {}
            for field_name, target in values.items():
                field = record._fields[field_name]
                current = record[field_name]
                if field.type == "many2one":
                    current = current.id or False
                if current != target:
                    changes[field_name] = target
            if changes:
                record.write(changes)

        branch_code = str(item.get("branch_code") or "").strip().upper()
        area_code = str(item.get("parking_area_code") or "").strip().upper()
        if not branch_code or not area_code:
            raise UserError(_("Branch Code and Parking Area Code are required."))

        state = str(item.get("state") or "draft").strip().lower()
        if state not in ("draft", "operational", "maintenance", "blocked"):
            raise UserError(_("Invalid Parking Area state: %s") % state)

        branch = self.env["nsp.branch"].sudo().search([("code", "=", branch_code)], limit=1)
        if not branch:
            branch = self._apply_branch({
                "branch_code": branch_code,
                "branch_name": branch_code,
                "active": True,
            })

        Parking = self.env["nsp.parking.area"].sudo()
        parking = Parking.search([("code", "=", area_code)], limit=1)
        parking_vals = {
            "code": area_code,
            "name": item.get("parking_area_name") or area_code,
            "branch_id": branch.id,
            "state": state,
        }
        if parking:
            write_changed(parking, parking_vals)
        else:
            parking = Parking.create(parking_vals)

        Controller = self.env["nsp.controller"].sudo()
        Device = self.env["nsp.device"].sudo()
        Antenna = self.env["nsp.device.antenna"].sudo()
        configured_controllers = {}

        controllers_data = item.get("controllers") or []
        if not isinstance(controllers_data, list):
            raise UserError(_("Parking controllers must be an array."))
        for controller_item in controllers_data:
            if not isinstance(controller_item, dict):
                raise UserError(_("Parking controllers must contain objects."))
            controller_code = str(controller_item.get("controller_code") or "").strip().upper()
            if not controller_code:
                raise UserError(_("Each Parking Controller requires controller_code."))
            controller = self._find_or_create_controller(controller_code)
            configured_controllers[controller_code] = controller

            devices_data = controller_item.get("devices") or []
            if not isinstance(devices_data, list):
                raise UserError(_("Controller devices must be an array."))
            for device_item in devices_data:
                if not isinstance(device_item, dict):
                    raise UserError(_("Controller devices must contain objects."))
                serial = str(device_item.get("serial_number") or "").strip().upper()
                if not serial:
                    raise UserError(_("Each Reader requires serial_number."))
                device = self._find_or_create_device(controller, serial)
                reader_parameters = device_item.get("reader_parameters") or {}
                if not isinstance(reader_parameters, dict):
                    raise UserError(_("reader_parameters must be an object."))
                connection_type = device_item.get("physical_connection") or False
                allowed_connections = {
                    key for key, _label in Device._fields["connection_type"].selection
                }
                if connection_type and connection_type not in allowed_connections:
                    raise UserError(_("Invalid Physical Connection for Reader %s.") % serial)
                write_changed(device, {
                    "model_number": str(device_item.get("model_number") or "").strip() or False,
                    "device_vendor": str(device_item.get("vendor") or "").strip() or False,
                    "connection_type": connection_type,
                    "power_dbm": int(
                        reader_parameters.get("power_dbm")
                        if reader_parameters.get("power_dbm") is not None
                        else 30
                    ),
                    "read_interval_ms": int(reader_parameters.get("read_interval_ms") or 200),
                    "tid_addr": int(reader_parameters.get("tid_start_address") or 0),
                    "tid_len": int(reader_parameters.get("tid_length") or 4),
                })

                antennas_data = device_item.get("antennas") or []
                if not isinstance(antennas_data, list):
                    raise UserError(_("Reader antennas must be an array."))
                seen_numbers = set()
                for antenna_item in antennas_data:
                    if not isinstance(antenna_item, dict):
                        raise UserError(_("Reader antennas must contain objects."))
                    antenna_no = int(antenna_item.get("antenna_no") or 0)
                    if antenna_no <= 0 or antenna_no in seen_numbers:
                        raise UserError(_("Invalid or duplicate Antenna No for Reader %s.") % serial)
                    seen_numbers.add(antenna_no)
                    antenna = Antenna.search([
                        ("device_id", "=", device.id),
                        ("antenna_no", "=", antenna_no),
                    ], limit=1)
                    antenna_vals = {
                        "device_id": device.id,
                        "antenna_no": antenna_no,
                        "minimum_rssi_dbm": float(
                            antenna_item.get("minimum_rssi_dbm")
                            if antenna_item.get("minimum_rssi_dbm") is not None
                            else -65.0
                        ),
                    }
                    if antenna:
                        write_changed(antenna, antenna_vals)
                    else:
                        Antenna.create(antenna_vals)

        Lane = self.env["nsp.parking.lane"].sudo().with_context(active_test=False)
        Mapping = self.env["nsp.parking.lane.antenna.mapping"].sudo()
        incoming_codes = []
        lanes_data = item.get("lanes") or []
        if not isinstance(lanes_data, list):
            raise UserError(_("Parking lanes must be an array."))

        for lane_index, lane_item in enumerate(lanes_data, start=1):
            if not isinstance(lane_item, dict):
                raise UserError(_("Parking lanes must contain objects."))
            lane_code = str(lane_item.get("lane_code") or "").strip().upper()
            controller_code = str(lane_item.get("controller_code") or "").strip().upper()
            direction = str(lane_item.get("direction") or "").strip().lower()
            if not lane_code or not controller_code:
                raise UserError(_("Each Parking Lane requires lane_code and controller_code."))
            if direction not in ("entry", "exit", "both"):
                raise UserError(_("Parking Lane direction must be entry, exit or both."))
            controller = configured_controllers.get(controller_code)
            if not controller:
                controller = Controller.search([("controller_id", "=", controller_code)], limit=1)
            if not controller:
                raise UserError(_("Controller %s is missing from Parking controller configuration.") % controller_code)

            try:
                grouping_window = int(lane_item.get("grouping_window_seconds") or 3)
                repeat_suppression = int(lane_item.get("repeat_suppression_seconds") or 10)
            except (TypeError, ValueError) as exc:
                raise UserError(_("Parking Lane timing values must be integers.")) from exc
            if grouping_window < 1:
                raise UserError(_("Grouping window must be at least one second."))
            if repeat_suppression < grouping_window:
                raise UserError(_(
                    "Repeat read suppression must be greater than or equal to the grouping window."
                ))

            incoming_codes.append(lane_code)
            lane = Lane.search([
                ("parking_area_id", "=", parking.id),
                ("code", "=", lane_code),
            ], limit=1)
            lane_vals = {
                "parking_area_id": parking.id,
                "code": lane_code,
                "name": lane_item.get("lane_name") or lane_code,
                "controller_id": controller.id,
                "lane_no": int(lane_item.get("lane_no") or lane_index),
                "direction": direction,
                "required_vehicle_tid": bool(lane_item.get("required_vehicle_tid", True)),
                "required_user_tid": bool(lane_item.get("required_user_tid", False)),
                "grouping_window_seconds": grouping_window,
                "repeat_suppression_seconds": repeat_suppression,
                "active": True,
            }
            if lane:
                write_changed(lane, lane_vals)
            else:
                lane = Lane.create(lane_vals)

            existing_lane_mappings = Mapping.search([("lane_id", "=", lane.id)])
            desired_antenna_ids = set()
            mappings_data = lane_item.get("antenna_mappings") or []
            if not isinstance(mappings_data, list):
                raise UserError(_("Antenna mappings must be an array."))
            for mapping_item in mappings_data:
                if not isinstance(mapping_item, dict):
                    raise UserError(_("Antenna mappings must contain objects."))
                serial = str(mapping_item.get("serial_number") or "").strip().upper()
                antenna_no = int(mapping_item.get("antenna_no") or 0)
                mapping_direction = str(mapping_item.get("direction") or "").strip().lower()
                if not serial or antenna_no <= 0:
                    raise UserError(_("Each antenna mapping requires serial_number and antenna_no."))
                if mapping_direction not in ("entry", "exit", "both"):
                    raise UserError(_("Antenna mapping direction must be entry, exit or both."))
                if direction != "both" and mapping_direction != direction:
                    raise UserError(_("A one-way Lane antenna mapping must match the Lane direction."))
                device = Device.search([
                    ("controller_id", "=", controller.id),
                    ("serial_number", "=", serial),
                ], limit=1)
                if not device:
                    raise UserError(_("Reader %s is missing from Parking controller configuration.") % serial)
                antenna = Antenna.search([
                    ("device_id", "=", device.id),
                    ("antenna_no", "=", antenna_no),
                ], limit=1)
                if not antenna:
                    raise UserError(_("Antenna %s/%s is missing from Reader configuration.") % (serial, antenna_no))
                if antenna.id in desired_antenna_ids:
                    raise UserError(_("Antenna %s/%s is duplicated in the Lane mapping.") % (serial, antenna_no))
                desired_antenna_ids.add(antenna.id)
                mapping_vals = {
                    "lane_id": lane.id,
                    "direction": mapping_direction,
                    "antenna_ref_id": antenna.id,
                }
                mapping = Mapping.search([("antenna_ref_id", "=", antenna.id)], limit=1)
                if mapping:
                    write_changed(mapping, mapping_vals)
                else:
                    Mapping.create(mapping_vals)

            stale_mappings = existing_lane_mappings.filtered(
                lambda mapping: mapping.antenna_ref_id.id not in desired_antenna_ids
            )
            if stale_mappings:
                stale_mappings.unlink()

        stale_domain = [("parking_area_id", "=", parking.id)]
        if incoming_codes:
            stale_domain.append(("code", "not in", incoming_codes))
        stale_lanes = Lane.search(stale_domain)
        if stale_lanes:
            stale_lanes.mapped("antenna_mapping_ids").unlink()
            stale_lanes.write({"active": False})
        return parking

    def _reconcile_parking_config_snapshot(self, items):
        self.ensure_one()
        incoming_codes = {
            str(item.get("parking_area_code") or "").strip().upper()
            for item in (items or [])
            if isinstance(item, dict) and item.get("parking_area_code")
        }
        Parking = self.env["nsp.parking.area"].sudo()
        stale = Parking.search([])
        if incoming_codes:
            stale = stale.filtered(lambda record: record.code not in incoming_codes)
        if stale:
            stale.mapped("lane_ids.antenna_mapping_ids").unlink()
            stale.mapped("lane_ids").write({"active": False})
            stale.write({"state": "blocked"})
        return len(stale)

    def _apply_items(self, kind, items, request_payload=False):
        self.ensure_one()
        results, failed = [], []
        Record = self.env["nsp.sync.record"].sudo()
        handlers = {
            "device_whitelist": self._apply_device_whitelist,
            "branch": self._apply_branch,
            "user": self._apply_user,
            "vehicle": self._apply_vehicle,
            "vehicle_borrow": self._apply_vehicle_borrow,
            "parking_config": self._apply_parking_config,
            "measurement_config": self._apply_measurement_config,
        }
        handler = handlers.get(kind)
        if not handler:
            raise UserError(_("Unsupported pull route: %s") % self.route_suffix)
        for index, item in enumerate(items if isinstance(items, list) else []):
            key = self._record_key_from_item(item)
            try:
                with self.env.cr.savepoint():
                    record = handler(item)
                key = key or record.display_name or str(record.id)
                Record.mark_result(
                    sync_job=self,
                    action_code=self.sync_action_code,
                    action_name=self.sync_action_name,
                    route_suffix=self.route_suffix,
                    record=record,
                    record_key=key,
                    status="synced",
                    message="Applied by Edge Server.",
                    payload=request_payload,
                    response=item,
                    operation="pull",
                )
                results.append({
                    "index": index,
                    "record_key": key,
                    "record_model": record._name,
                    "record_id": record.id,
                    "success": True,
                })
            except Exception as exc:
                message = str(exc)
                Record.mark_result(
                    sync_job=self,
                    action_code=self.sync_action_code,
                    action_name=self.sync_action_name,
                    route_suffix=self.route_suffix,
                    record_key=key or str(index),
                    status="failed",
                    message=message,
                    payload=request_payload,
                    response=item,
                    operation="pull",
                )
                failed.append({"index": index, "record_key": key, "error": message})
        return results, failed

    # --------------------------- protocol adapters --------------------
    @api.model
    def _items_from_response(self, data):
        items = data.get("items") if isinstance(data, dict) else []
        if isinstance(items, dict):
            return [items]
        return items if isinstance(items, list) else []

    def _build_pull_payload(self):
        self.ensure_one()
        # Full snapshots do not use incremental cursors. This guarantees that
        # deletions, revocations and assignment changes are reflected on Edge.
        if self._action_kind() in ("device_whitelist", "vehicle_config", "card", "parking_config"):
            return {"edge_server_code": self.edge_server_code}
        payload = {"edge_server_code": self.edge_server_code, "limit": self.batch_size}
        if self.sync_cursor:
            payload["sync_cursor"] = self.sync_cursor
        return payload

    @api.model
    def _record_key_from_item(self, item):
        if not isinstance(item, dict):
            return False
        for field_name in (
            "record_key", "card_uid", "borrow_uid", "branch_code", "user_code",
            "vehicle_code", "license_plate", "parking_area_code", "transaction_uid",
            "measurement_code", "event_uid", "serial_number", "code",
            "controller_code", "edge_server_code",
        ):
            if item.get(field_name):
                return str(item[field_name])
        return False

    # --------------------------- measurement push ---------------------
    @api.model
    def _measurement_event_payload(self, event):
        payload = {
            "event_uid": event.event_uid,
            "serial_number": event.serial_number,
            "antenna_no": int(event.antenna_no),
            "tid": event.tid,
            "read_at": self._iso_utc(event.read_at),
        }
        if event.rssi_dbm not in (False, None):
            payload["rssi_dbm"] = float(event.rssi_dbm)
        return payload

    def _pending_measurement_events(self, limit):
        self.ensure_one()
        edge = self._require_edge_server_record()
        source_code = str(self.edge_server_code or "NSP").strip() or "NSP"
        action_code = str(self.sync_action_code or "").strip()
        self.env.cr.execute(
            """
            SELECT event.id
              FROM nsp_measurement_event event
              JOIN nsp_measurement_session session ON session.id = event.session_id
              JOIN nsp_controller controller ON controller.id = session.controller_id
             WHERE controller.edge_server_id = %s
               AND NOT EXISTS (
                    SELECT 1
                      FROM nsp_sync_record record
                     WHERE record.source_code = %s
                       AND record.sync_action_code = %s
                       AND record.operation = 'push'
                       AND record.record_key = event.event_uid
                       AND record.status = 'synced'
               )
             ORDER BY event.id
             LIMIT %s
            """,
            (edge.id, source_code, action_code, max(1, int(limit or 1))),
        )
        ids = [row[0] for row in self.env.cr.fetchall()]
        if not ids:
            return self.env["nsp.measurement.event"].browse()
        first = self.env["nsp.measurement.event"].sudo().browse(ids[0])
        return self.env["nsp.measurement.event"].sudo().browse(ids).filtered(
            lambda event: event.session_id == first.session_id
        )

    def _push_measurement_event_records(self, events, timeout=120):
        self.ensure_one()
        events = events.sudo().exists().sorted(key=lambda event: event.id)
        if not events:
            return {"pushed": 0, "failed": 0, "has_more": False, "message": "No Measurement Events to push."}
        session = events[0].session_id
        events = events.filtered(lambda event: event.session_id == session)
        Record = self.env["nsp.sync.record"].sudo()
        payload = {
            "edge_server_code": self.edge_server_code,
            "measurement_code": session.measurement_code,
            "events": [self._measurement_event_payload(event) for event in events],
        }
        for event in events:
            Record.mark_pending(
                sync_job=self,
                action_code=self.sync_action_code,
                action_name=self.sync_action_name,
                route_suffix=self.route_suffix,
                record=event,
                record_key=event.event_uid,
                message="Waiting for Cloud response.",
                payload=self._measurement_event_payload(event),
                operation="push",
            )
        try:
            data = self._json_or_error(self._post_remote(self.sync_action_id, payload, timeout=timeout))
        except Exception as exc:
            for event in events:
                Record.mark_result(
                    sync_job=self,
                    action_code=self.sync_action_code,
                    action_name=self.sync_action_name,
                    route_suffix=self.route_suffix,
                    record=event,
                    record_key=event.event_uid,
                    status="failed",
                    message=str(exc),
                    payload=self._measurement_event_payload(event),
                    operation="push",
                )
            raise
        result_by_key = {
            str(result.get("record_key") or ""): result
            for result in (data.get("results") or [])
            if isinstance(result, dict)
        }
        reported_failed = int(data.get("failed") or 0)
        failed = 0
        for event in events:
            result = result_by_key.get(event.event_uid)
            rejected = bool(result and result.get("status") in ("rejected", "failed", "error"))
            if not result_by_key and reported_failed:
                rejected = True
            Record.mark_result(
                sync_job=self,
                action_code=self.sync_action_code,
                action_name=self.sync_action_name,
                route_suffix=self.route_suffix,
                record=event,
                record_key=event.event_uid,
                status="failed" if rejected else "synced",
                message=(result or {}).get("message") or ("Rejected by Cloud." if rejected else "Accepted by Cloud."),
                payload=self._measurement_event_payload(event),
                response=result or data,
                operation="push",
            )
            failed += int(rejected)
        if failed:
            raise UserError(_("Cloud rejected %s Measurement Event(s).") % failed)
        self.last_push_at = fields.Datetime.now()
        return {
            "pushed": len(events),
            "failed": 0,
            "has_more": bool(self._pending_measurement_events(1)),
            "message": "Pushed %s Measurement Event(s)." % len(events),
        }

    def _run_measurement_event_push_once(self):
        self.ensure_one()
        events = self._pending_measurement_events(
            max(1, min(int(self.batch_size or 100), 100))
        )
        return self._push_measurement_event_records(events)

    @api.model
    def push_measurement_events_now(self, events):
        job = self.sudo().search([
            ("active", "=", True),
            ("route_suffix", "=", "measurement-events/sync"),
            ("direction", "=", "push"),
        ], order="sequence, id", limit=1)
        if not job:
            return False
        try:
            job._push_measurement_event_records(events, timeout=3)
            return True
        except Exception:
            _logger.exception("Immediate Measurement Event forwarding failed; fallback retry remains pending.")
            return False

    @api.model
    def _measurement_status_payload(self, session):
        occurred_at = session.ended_at or session.started_at or session.write_date or fields.Datetime.now()
        return {
            "edge_server_code": self.edge_server_code,
            "measurement_code": session.measurement_code,
            "status": session.status,
            "occurred_at": self._iso_utc(occurred_at),
        }

    def _pending_measurement_status_sessions(self, limit):
        self.ensure_one()
        edge = self._require_edge_server_record()
        source_code = str(self.edge_server_code or "NSP").strip() or "NSP"
        action_code = str(self.sync_action_code or "").strip()
        self.env.cr.execute(
            """
            SELECT session.id
              FROM nsp_measurement_session session
              JOIN nsp_controller controller ON controller.id = session.controller_id
             WHERE controller.edge_server_id = %s
               AND session.status != 'draft'
               AND NOT EXISTS (
                    SELECT 1
                      FROM nsp_sync_record record
                     WHERE record.source_code = %s
                       AND record.sync_action_code = %s
                       AND record.operation = 'push'
                       AND record.record_key = session.measurement_code
                       AND record.status = 'synced'
                       AND record.last_synced_at >= session.write_date
               )
             ORDER BY session.write_date, session.id
             LIMIT %s
            """,
            (edge.id, source_code, action_code, max(1, int(limit or 1))),
        )
        return self.env["nsp.measurement.session"].sudo().browse(
            [row[0] for row in self.env.cr.fetchall()]
        )

    def _push_measurement_status_records(self, sessions, timeout=120):
        self.ensure_one()
        sessions = sessions.sudo().exists().sorted(key=lambda session: (session.write_date, session.id))
        if not sessions:
            return {"pushed": 0, "failed": 0, "has_more": False, "message": "No Measurement status to push."}
        Record = self.env["nsp.sync.record"].sudo()
        pushed = 0
        for session in sessions:
            payload = self._measurement_status_payload(session)
            Record.mark_pending(
                sync_job=self,
                action_code=self.sync_action_code,
                action_name=self.sync_action_name,
                route_suffix=self.route_suffix,
                record=session,
                record_key=session.measurement_code,
                message="Waiting for Cloud response.",
                payload=payload,
                operation="push",
            )
            try:
                data = self._json_or_error(self._post_remote(self.sync_action_id, payload, timeout=timeout))
            except Exception as exc:
                Record.mark_result(
                    sync_job=self,
                    action_code=self.sync_action_code,
                    action_name=self.sync_action_name,
                    route_suffix=self.route_suffix,
                    record=session,
                    record_key=session.measurement_code,
                    status="failed",
                    message=str(exc),
                    payload=payload,
                    operation="push",
                )
                raise
            Record.mark_result(
                sync_job=self,
                action_code=self.sync_action_code,
                action_name=self.sync_action_name,
                route_suffix=self.route_suffix,
                record=session,
                record_key=session.measurement_code,
                status="synced",
                message="Measurement status accepted by Cloud.",
                payload=payload,
                response=data,
                operation="push",
            )
            pushed += 1
        self.last_push_at = fields.Datetime.now()
        return {
            "pushed": pushed,
            "failed": 0,
            "has_more": bool(self._pending_measurement_status_sessions(1)),
            "message": "Pushed %s Measurement status record(s)." % pushed,
        }

    def _run_measurement_status_push_once(self):
        self.ensure_one()
        sessions = self._pending_measurement_status_sessions(
            max(1, min(int(self.batch_size or 100), 1000))
        )
        return self._push_measurement_status_records(sessions)

    @api.model
    def push_measurement_status_now(self, session):
        job = self.sudo().search([
            ("active", "=", True),
            ("route_suffix", "=", "measurement-status/sync"),
            ("direction", "=", "push"),
        ], order="sequence, id", limit=1)
        if not job:
            return False
        try:
            job._push_measurement_status_records(session, timeout=3)
            return True
        except Exception:
            _logger.exception("Immediate Measurement status forwarding failed; fallback retry remains pending.")
            return False

    # --------------------------- execution ----------------------------
    def _mark_push_failure(self, items, data_or_error):
        Record = self.env["nsp.sync.record"].sudo()
        message = str(data_or_error)
        for item in items:
            key = self._record_key_from_item(item)
            if key:
                Record.mark_result(
                    sync_job=self,
                    action_code=self.sync_action_code,
                    action_name=self.sync_action_name,
                    route_suffix=self.route_suffix,
                    record_key=key,
                    status="failed",
                    message=message,
                    payload=item,
                    response=data_or_error if isinstance(data_or_error, dict) else False,
                    operation="push",
                )

    def run_push_once(self):
        self.ensure_one()
        kind = self._action_kind()
        if kind == "measurement_event":
            return self._run_measurement_event_push_once()
        if kind == "measurement_status":
            return self._run_measurement_status_push_once()
        batch = self._serialize_push_batch(kind)
        items = batch["items"]
        if not items:
            return {"pushed": 0, "failed": 0, "has_more": False, "message": "No changed records to push."}
        Record = self.env["nsp.sync.record"].sudo()
        for item in items:
            key = self._record_key_from_item(item)
            if key:
                Record.mark_pending(
                    sync_job=self,
                    action_code=self.sync_action_code,
                    action_name=self.sync_action_name,
                    route_suffix=self.route_suffix,
                    record_key=key,
                    message="Waiting for Cloud response.",
                    payload=item,
                    operation="push",
                )
        try:
            data = self._json_or_error(self._post_remote(self.sync_action_id, self._build_push_payload(items), timeout=120))
        except Exception as exc:
            self._mark_push_failure(items, exc)
            raise
        rejected = [
            result for result in (data.get("results") or [])
            if isinstance(result, dict) and result.get("status") in ("rejected", "failed", "error")
        ]
        if rejected or int(data.get("failed") or 0):
            rejected_by_key = {str(item.get("record_key") or ""): item for item in rejected}
            if rejected_by_key:
                for item in items:
                    key = self._record_key_from_item(item)
                    result = rejected_by_key.get(str(key or ""))
                    if result:
                        Record.mark_result(
                            sync_job=self,
                            action_code=self.sync_action_code,
                            action_name=self.sync_action_name,
                            route_suffix=self.route_suffix,
                            record_key=key,
                            status="failed",
                            message=result.get("message") or result.get("error"),
                            payload=item,
                            response=data,
                            operation="push",
                        )
            else:
                self._mark_push_failure(items, data)
            raise UserError(json.dumps(rejected or data, ensure_ascii=False))
        for item in items:
            key = self._record_key_from_item(item)
            if key:
                Record.mark_result(
                    sync_job=self,
                    action_code=self.sync_action_code,
                    action_name=self.sync_action_name,
                    route_suffix=self.route_suffix,
                    record_key=key,
                    status="synced",
                    message="Cloud accepted.",
                    payload=item,
                    response=data,
                    operation="push",
                )
        self.write({
            "last_push_at": batch.get("cursor_at") or fields.Datetime.now(),
            "last_push_record_id": int(batch.get("cursor_id") or 0),
        })
        return {
            "pushed": len(items),
            "failed": 0,
            "has_more": bool(batch.get("has_more")),
            "message": "Pushed %s record(s)." % len(items),
        }

    def run_pull_once(self):
        self.ensure_one()
        request_payload = self._build_pull_payload()
        data = self._json_or_error(
            self._post_remote(self.sync_action_id, request_payload, timeout=120)
        )
        kind = self._action_kind()

        if kind == "vehicle_config":
            records, removed = self._apply_vehicle_config_snapshot(
                data, request_payload=request_payload
            )
            self.write({"last_pull_at": fields.Datetime.now(), "sync_cursor": False})
            return {
                "pulled": len(records),
                "failed": 0,
                "has_more": False,
                "message": (
                    "Pulled Vehicle Configuration snapshot: %(count)s record(s); "
                    "archived %(types)s type(s), %(brands)s brand(s), %(models)s model(s), %(colors)s color(s)."
                ) % {
                    "count": len(records),
                    "types": removed["vehicle_types"],
                    "brands": removed["brands"],
                    "models": removed["models"],
                    "colors": removed["colors"],
                },
            }

        if kind == "card":
            counts, removed = self._apply_card_snapshot(data, request_payload=request_payload)
            self.write({"last_pull_at": fields.Datetime.now(), "sync_cursor": False})
            return {
                "pulled": counts["master_cards"],
                "failed": 0,
                "has_more": False,
                "message": (
                    "Cards snapshot applied: %(master)s Master Card(s), %(users)s User Card(s), "
                    "%(vehicles)s Vehicle Card(s), %(unassigned)s unassigned; removed %(removed)s stale Card(s)."
                ) % {
                    "master": counts["master_cards"],
                    "users": counts["user_cards"],
                    "vehicles": counts["vehicle_cards"],
                    "unassigned": counts["unassigned_cards"],
                    "removed": removed,
                },
            }

        items = self._items_from_response(data)
        next_cursor = data.get("next_sync_cursor") or False
        has_more = bool(data.get("has_more"))
        full_snapshot = kind in ("device_whitelist", "parking_config")
        if not items:
            removed = 0
            if kind == "device_whitelist":
                removed = self._reconcile_device_whitelist_snapshot([])
            elif kind == "parking_config":
                removed = self._reconcile_parking_config_snapshot([])
            self.write({
                "last_pull_at": fields.Datetime.now(),
                "sync_cursor": False if full_snapshot else (next_cursor or self.sync_cursor),
            })
            if kind == "device_whitelist":
                message = "Device Whitelist snapshot is empty; removed %s stale record(s)." % removed
            elif kind == "parking_config":
                message = "Parking Configuration snapshot is empty; blocked %s stale area(s)." % removed
            else:
                message = "No changed records to pull."
            return {
                "pulled": 0,
                "failed": 0,
                "has_more": False if full_snapshot else has_more,
                "message": message,
            }
        results, failed = self._apply_items(kind, items, request_payload=request_payload)
        if failed:
            raise UserError(json.dumps(failed, ensure_ascii=False))
        removed = 0
        if kind == "device_whitelist":
            removed = self._reconcile_device_whitelist_snapshot(items)
        elif kind == "parking_config":
            removed = self._reconcile_parking_config_snapshot(items)
        self.write({
            "last_pull_at": fields.Datetime.now(),
            "sync_cursor": False if full_snapshot else (next_cursor or self.sync_cursor),
        })
        if kind == "device_whitelist":
            message = "Pulled %s Device Whitelist record(s); removed %s stale record(s)." % (len(results), removed)
        elif kind == "parking_config":
            message = "Applied %s Parking Configuration record(s); blocked %s stale area(s)." % (len(results), removed)
        else:
            message = "Pulled %s record(s)." % len(results)
        return {
            "pulled": len(results),
            "failed": 0,
            "has_more": False if full_snapshot else has_more,
            "message": message,
        }

    def run_once(self):
        self._ensure_edge_server_instance()
        for rec in self:
            if not rec.active:
                rec.write({"status": "disabled", "last_message": "Sync Job disabled."})
                continue
            rec.write({"status": "running", "last_message": False})
            result = {}
            try:
                result = rec.run_pull_once() if rec.direction == "pull" else rec.run_push_once()
                rec.write({"status": "success", "last_message": result.get("message") or "Done."})
            except Exception as exc:
                rec.write({"status": "failed", "last_message": str(exc)})
                _logger.exception("NSP Sync Job failed: %s", rec.display_name)
            finally:
                rec._schedule_next(immediate=bool(result.get("has_more")))
        return True

    def action_run_now(self):
        self.run_once()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("NSP Sync"),
                "message": _("Sync Job completed. Check Status and Last Message."),
                "type": "success",
                "sticky": False,
            },
        }

    @api.model
    def run_due_jobs(self):
        now = fields.Datetime.now()
        jobs = self.sudo().search([
            ("active", "=", True),
            "|", ("next_run_at", "=", False), ("next_run_at", "<=", now),
        ], order="sequence, id")
        if jobs:
            jobs.run_once()
        return len(jobs)

    @api.model
    def cron_run_job_loop(self):
        return self.run_due_jobs()
