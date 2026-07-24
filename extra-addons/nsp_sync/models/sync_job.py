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
    "users/sync": "pull",
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
    "users/sync": {"schedule_interval_minutes": 5, "batch_size": 500},
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
    "users/sync": "user",
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
        """Create/repair the supported job set using bounded queries."""
        auth_records = auth_records.exists()
        if not auth_records:
            return self.browse()
        self._ensure_edge_server_instance()
        Action = self.env["ir.actions.core_api"].sudo()
        version = self.env["core.api.version"].sudo().get_default_version()
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

        existing_jobs = self.search([("auth_id", "in", auth_records.ids)])
        existing_by_auth = {}
        for job in existing_jobs:
            existing_by_auth.setdefault(job.auth_id.id, set()).add(
                (job.route_suffix or "").strip().strip("/")
            )

        now = fields.Datetime.now()
        vals_list = []
        for auth in auth_records:
            existing_routes = existing_by_auth.get(auth.id, set())
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
        created = self.create(vals_list) if vals_list else self.browse()

        for job in existing_jobs | created:
            route = (job.route_suffix or "").strip().strip("/")
            values = {}
            expected_sequence = JOB_SEQUENCE.get(route)
            if expected_sequence is not None and job.sequence != expected_sequence:
                values["sequence"] = expected_sequence
            settings = DEFAULT_JOB_SETTINGS.get(route)
            if settings:
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
        ok = data.get("success") if "success" in data else data.get("status") == "success"
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

    @api.model
    def _write_changed(self, record, values):
        """Write only fields whose persisted value actually changes."""
        if not record:
            return False
        changes = {}
        for field_name, target in (values or {}).items():
            field = record._fields[field_name]
            current = record[field_name]
            if field.type == "many2one":
                current = current.id or False
            if current != target:
                changes[field_name] = target
        if changes:
            record.write(changes)
        return bool(changes)

    # --------------------------- pull application ---------------------
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
            self._write_changed(controller, vals)
            return controller
        vals["controller_id"] = code
        return Controller.create(vals)

    @api.model
    def _prepare_apply_cache(self, kind, items):
        """Preload master records used by high-volume pull snapshots."""
        rows = [item for item in (items or []) if isinstance(item, dict)]
        if kind == "device_whitelist":
            serials = {str(item.get("serial_number") or "").strip().upper() for item in rows}
            serials.discard("")
            records = self.env["nsp.device.whitelist"].sudo().search([
                ("serial_number", "in", list(serials)),
            ]) if serials else self.env["nsp.device.whitelist"].browse()
            return {"records": {record.serial_number: record for record in records}}

        if kind == "branch":
            codes = {str(item.get("branch_code") or "").strip().upper() for item in rows}
            codes.discard("")
            records = self.env["nsp.branch"].sudo().with_context(active_test=False).search([
                ("code", "in", list(codes)),
            ]) if codes else self.env["nsp.branch"].browse()
            return {"records": {record.code: record for record in records}}

        if kind == "user":
            codes = {str(item.get("user_code") or "").strip().upper() for item in rows}
            codes.discard("")
            records = self.env["nsp.user"].sudo().with_context(active_test=False).search([
                ("user_code", "in", list(codes)),
            ]) if codes else self.env["nsp.user"].browse()
            return {"records": {record.user_code: record for record in records}}

        if kind == "vehicle":
            vehicle_codes = {str(item.get("vehicle_code") or "").strip().upper() for item in rows}
            owner_codes = {str(item.get("owner_user_code") or "").strip().upper() for item in rows}
            vehicle_codes.discard(""); owner_codes.discard("")
            Vehicle = self.env["nsp.vehicle"].sudo().with_context(active_test=False)
            vehicles = Vehicle.search([
                ("vehicle_code", "in", list(vehicle_codes)),
            ]) if vehicle_codes else Vehicle.browse()
            users = self.env["nsp.user"].sudo().with_context(active_test=False).search([
                ("user_code", "in", list(owner_codes)),
            ]) if owner_codes else self.env["nsp.user"].browse()
            cache = {
                "vehicle_by_code": {record.vehicle_code: record for record in vehicles if record.vehicle_code},
                "user_by_code": {record.user_code: record for record in users},
            }
            master_specs = (
                ("type_by_code", "nsp.vehicle.type", "vehicle_type_code"),
                ("brand_by_code", "nsp.vehicle.brand", "brand_code"),
                ("model_by_code", "nsp.vehicle.model", "model_code"),
                ("color_by_code", "nsp.vehicle.color", "color_code"),
            )
            for cache_key, model_name, payload_field in master_specs:
                codes = {str(item.get(payload_field) or "").strip().upper() for item in rows}
                codes.discard("")
                records = self.env[model_name].sudo().with_context(active_test=False).search([
                    ("code", "in", list(codes)),
                ]) if codes else self.env[model_name].browse()
                cache[cache_key] = {record.code: record for record in records}
            return cache

        if kind == "vehicle_borrow":
            borrow_codes = {str(item.get("borrow_uid") or "").strip() for item in rows}
            vehicle_codes = {str(item.get("vehicle_code") or "").strip().upper() for item in rows}
            user_codes = {str(item.get("borrower_user_code") or "").strip().upper() for item in rows}
            borrow_codes.discard(""); vehicle_codes.discard(""); user_codes.discard("")
            borrows = self.env["nsp.vehicle.borrow"].sudo().search([
                ("borrow_code", "in", list(borrow_codes)),
            ]) if borrow_codes else self.env["nsp.vehicle.borrow"].browse()
            vehicles = self.env["nsp.vehicle"].sudo().with_context(active_test=False).search([
                ("vehicle_code", "in", list(vehicle_codes)),
            ]) if vehicle_codes else self.env["nsp.vehicle"].browse()
            users = self.env["nsp.user"].sudo().with_context(active_test=False).search([
                ("user_code", "in", list(user_codes)),
            ]) if user_codes else self.env["nsp.user"].browse()
            return {
                "borrow_by_code": {record.borrow_code: record for record in borrows},
                "vehicle_by_code": {record.vehicle_code: record for record in vehicles},
                "user_by_code": {record.user_code: record for record in users},
            }
        return {}

    @api.model
    def _apply_device_whitelist(self, item, cache=None):
        serial = str(item.get("serial_number") or "").strip().upper()
        if not serial:
            raise UserError(_("Device Whitelist Serial is required."))
        device_type = str(item.get("device_type") or "rfid_reader").strip().lower()
        if device_type not in ("rfid_reader", "camera", "other"):
            raise UserError(_("Invalid Device Type: %s") % device_type)
        cache = cache or self._prepare_apply_cache("device_whitelist", [item])
        Whitelist = self.env["nsp.device.whitelist"].sudo()
        record = cache.get("records", {}).get(serial)
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
        record = Whitelist.create(vals)
        cache.setdefault("records", {})[serial] = record
        return record

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
    def _apply_branch(self, item, cache=None):
        code = str(item.get("branch_code") or "").strip().upper()
        if not code:
            raise UserError(_("Branch Code is required."))
        cache = cache or self._prepare_apply_cache("branch", [item])
        Branch = self.env["nsp.branch"].sudo().with_context(active_test=False)
        branch = cache.get("records", {}).get(code)
        vals = {
            "code": code,
            "name": item.get("branch_name") or code,
            "timezone": item.get("timezone") or "Asia/Ho_Chi_Minh",
            "status": "active" if bool(item.get("active", True)) else "inactive",
        }
        if branch:
            self._write_changed(branch, vals)
            return branch
        branch = Branch.create(vals)
        cache.setdefault("records", {})[code] = branch
        return branch

    @api.model
    def _apply_user(self, item, cache=None):
        code = str(item.get("user_code") or "").strip().upper()
        if not code:
            raise UserError(_("User Code is required."))
        cache = cache or self._prepare_apply_cache("user", [item])
        User = self.env["nsp.user"].sudo().with_context(active_test=False)
        user = cache.get("records", {}).get(code)
        vals = {"user_code": code, "name": item.get("name") or code, "active": bool(item.get("active", True))}
        if user:
            self._write_changed(user, vals)
            return user
        user = User.create(vals)
        cache.setdefault("records", {})[code] = user
        return user

    @api.model
    def _reconcile_vehicle_master_snapshot(self, model_name, incoming_codes):
        Model = self.env[model_name].sudo().with_context(active_test=False)
        codes = [str(code).strip().upper() for code in incoming_codes if str(code or "").strip()]
        domain = [("code", "not in", codes)] if codes else []
        stale_active = Model.search(domain + [("active", "=", True)])
        if stale_active:
            stale_active.write({"active": False})
        return len(stale_active)

    @api.model
    def _vehicle_master_snapshot_group(self, model_name, items, extra_values=None):
        """Apply one master-data snapshot with bounded queries and writes."""
        rows = items or []
        normalized = []
        seen = set()
        for item in rows:
            if not isinstance(item, dict):
                raise UserError(_("Vehicle Configuration items must be objects."))
            code = str(item.get("code") or "").strip().upper()
            name = str(item.get("name") or "").strip()
            if not code or not name:
                raise UserError(_("Vehicle Configuration Code and Name are required."))
            if code in seen:
                raise UserError(_("Duplicate Vehicle Configuration Code: %s") % code)
            seen.add(code)
            normalized.append((item, code, name))

        Model = self.env[model_name].sudo().with_context(active_test=False)
        existing = Model.search([("code", "in", list(seen))]) if seen else Model.browse()
        by_code = {record.code: record for record in existing}
        creates = []
        create_meta = []
        applied = []
        for item, code, name in normalized:
            vals = {"code": code, "name": name, "active": bool(item.get("active", True))}
            if extra_values:
                vals.update(extra_values(item, code) or {})
            record = by_code.get(code)
            if record:
                self._write_changed(record, vals)
                applied.append((item, record))
            else:
                creates.append(vals)
                create_meta.append(item)
        if creates:
            created = Model.create(creates)
            applied.extend(zip(create_meta, created))
        return applied, [code for _item, code, _name in normalized]

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

        applied = []
        codes = {}
        with self.env.cr.savepoint():
            type_rows, codes["vehicle_types"] = self._vehicle_master_snapshot_group(
                "nsp.vehicle.type", groups["vehicle_types"]
            )
            applied.extend(("vehicle_type", item, record) for item, record in type_rows)

            brand_rows, codes["brands"] = self._vehicle_master_snapshot_group(
                "nsp.vehicle.brand", groups["brands"]
            )
            applied.extend(("brand", item, record) for item, record in brand_rows)

            Brand = self.env["nsp.vehicle.brand"].sudo().with_context(active_test=False)
            brand_codes = {
                str(item.get("brand_code") or "").strip().upper()
                for item in groups["models"] if isinstance(item, dict)
            }
            brand_codes.discard("")
            brands = Brand.search([("code", "in", list(brand_codes))]) if brand_codes else Brand.browse()
            brand_by_code = {record.code: record for record in brands}

            def model_extra(item, _code):
                brand_code = str(item.get("brand_code") or "").strip().upper()
                brand = brand_by_code.get(brand_code) if brand_code else False
                if brand_code and not brand:
                    raise UserError(
                        _("Vehicle Brand %(brand)s was not found for Vehicle Model %(model)s.")
                        % {"brand": brand_code, "model": item.get("code") or "-"}
                    )
                return {"brand_id": brand.id if brand else False}

            model_rows, codes["models"] = self._vehicle_master_snapshot_group(
                "nsp.vehicle.model", groups["models"], extra_values=model_extra
            )
            applied.extend(("model", item, record) for item, record in model_rows)

            color_rows, codes["colors"] = self._vehicle_master_snapshot_group(
                "nsp.vehicle.color", groups["colors"]
            )
            applied.extend(("color", item, record) for item, record in color_rows)

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
        return [record for _group, _item, record in applied], removed

    @api.model
    def _apply_vehicle(self, item, cache=None):
        code = str(item.get("vehicle_code") or "").strip().upper()
        plate = str(item.get("license_plate") or "").strip().upper()
        if not code or not plate:
            raise UserError(_("Vehicle Code and License Plate are required."))
        cache = cache or self._prepare_apply_cache("vehicle", [item])
        vehicle = cache.get("vehicle_by_code", {}).get(code)

        owner_user_code = str(item.get("owner_user_code") or "").strip().upper()
        if not owner_user_code:
            raise UserError(_("Vehicle Owner User Code is required."))
        owner = cache.get("user_by_code", {}).get(owner_user_code)
        if not owner:
            raise UserError(
                _("Vehicle Owner %(code)s was not found. Run users/sync first.")
                % {"code": owner_user_code}
            )

        def master(cache_key, payload_field, label):
            master_code = str(item.get(payload_field) or "").strip().upper()
            if not master_code:
                return False
            record = cache.get(cache_key, {}).get(master_code)
            if not record:
                raise UserError(_("%(label)s %(code)s was not found. Run vehicle-config/sync first.") % {
                    "label": label, "code": master_code,
                })
            return record

        vehicle_type = master("type_by_code", "vehicle_type_code", _("Vehicle Type"))
        brand = master("brand_by_code", "brand_code", _("Vehicle Brand"))
        vehicle_model = master("model_by_code", "model_code", _("Vehicle Model"))
        color = master("color_by_code", "color_code", _("Vehicle Color"))
        if vehicle_model and brand and vehicle_model.brand_id and vehicle_model.brand_id != brand:
            raise UserError(_("Vehicle Model %(model)s does not belong to Brand %(brand)s.") % {
                "model": vehicle_model.code, "brand": brand.code,
            })
        vals = {
            "vehicle_code": code,
            "license_plate": plate,
            "owner_id": owner.id,
            "vehicle_type_id": vehicle_type.id if vehicle_type else False,
            "brand_id": brand.id if brand else False,
            "model_id": vehicle_model.id if vehicle_model else False,
            "color_id": color.id if color else False,
            "active": bool(item.get("active", True)),
        }
        Vehicle = self.env["nsp.vehicle"].sudo().with_context(active_test=False)
        if vehicle:
            old_code = vehicle.vehicle_code
            self._write_changed(vehicle, vals)
            if old_code and old_code != code:
                cache.get("vehicle_by_code", {}).pop(old_code, None)
        else:
            vehicle = Vehicle.create(vals)
        cache.setdefault("vehicle_by_code", {})[code] = vehicle
        return vehicle

    @api.model
    def _card_assignment_values(self, item):
        assignment = item.get("assignment")
        if not isinstance(assignment, dict):
            raise UserError(_("Card assignment must be an object."))
        unsupported = sorted(set(assignment) - {"type", "code"})
        if unsupported:
            raise UserError(_("Unsupported Card assignment field(s): %s") % ", ".join(unsupported))
        assignment_type = str(assignment.get("type") or "unassigned").strip().lower()
        assignment_code = str(assignment.get("code") or "").strip().upper()
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
    def _normalize_card_snapshot_item(self, item):
        if not isinstance(item, dict):
            raise UserError(_("Cards snapshot items must be objects."))
        supported_fields = {"card_uid", "card_type", "assignment", "assigned_at"}
        unsupported_fields = sorted(set(item) - supported_fields)
        if unsupported_fields:
            raise UserError(_("Unsupported Card field(s): %s") % ", ".join(unsupported_fields))

        tid = str(item.get("card_uid") or "").strip().upper().replace(" ", "")
        card_type = str(item.get("card_type") or "").strip().lower()
        if not tid:
            raise UserError(_("Card UID is required."))
        if card_type not in ("vehicle_card", "user_card"):
            raise UserError(_("Invalid Card Type for %s.") % tid)

        assignment_type, assignment_code, assigned_at = self._card_assignment_values(item)
        expected_type = {"user": "user_card", "vehicle": "vehicle_card"}.get(assignment_type)
        if expected_type and card_type != expected_type:
            raise UserError(
                _("Card %(tid)s type %(card_type)s does not match %(assignment_type)s assignment.")
                % {"tid": tid, "card_type": card_type, "assignment_type": assignment_type}
            )
        return {
            "tid": tid,
            "card_type": card_type,
            "assignment_type": assignment_type,
            "assignment_code": assignment_code,
            "assigned_at": assigned_at,
            "source": item,
        }

    def _apply_card_snapshot(self, data, request_payload=False):
        """Apply one complete Card snapshot with batched lookups and writes."""
        self.ensure_one()
        items = self._items_from_response(data)
        if not isinstance(items, list):
            raise UserError(_("Cards snapshot must contain an items array."))

        normalized = []
        seen = set()
        for item in items:
            info = self._normalize_card_snapshot_item(item)
            if info["tid"] in seen:
                raise UserError(_("Duplicate Card UID in snapshot: %s") % info["tid"])
            seen.add(info["tid"])
            normalized.append(info)

        tids = [info["tid"] for info in normalized]
        Card = self.env["nsp.rfid.card"].sudo()
        User = self.env["nsp.user"].sudo().with_context(active_test=False)
        Vehicle = self.env["nsp.vehicle"].sudo().with_context(active_test=False)
        UserLine = self.env["nsp.user.card"].sudo().with_context(active_test=False)
        VehicleLine = self.env["nsp.vehicle.card"].sudo().with_context(active_test=False)

        counts = {"master_cards": len(normalized), "user_cards": 0, "vehicle_cards": 0, "unassigned_cards": 0}
        assignment_by_card = {}

        with self.env.cr.savepoint():
            existing_cards = Card.search([("tid", "in", tids)]) if tids else Card.browse()
            card_by_tid = {card.tid: card for card in existing_cards}

            create_vals = [
                {"tid": info["tid"], "card_type": info["card_type"]}
                for info in normalized if info["tid"] not in card_by_tid
            ]
            if create_vals:
                created = Card.create(create_vals)
                card_by_tid.update({card.tid: card for card in created})

            for info in normalized:
                card = card_by_tid[info["tid"]]
                if card.card_type != info["card_type"]:
                    card.write({"card_type": info["card_type"]})
                info["card"] = card

            user_codes = {
                info["assignment_code"] for info in normalized
                if info["assignment_type"] == "user"
            }
            vehicle_codes = {
                info["assignment_code"].upper() for info in normalized
                if info["assignment_type"] == "vehicle"
            }
            users = User.search([("user_code", "in", list(user_codes))]) if user_codes else User.browse()
            vehicles = Vehicle.search([("vehicle_code", "in", list(vehicle_codes))]) if vehicle_codes else Vehicle.browse()
            user_by_code = {user.user_code: user for user in users}
            vehicle_by_code = {vehicle.vehicle_code: vehicle for vehicle in vehicles}

            for info in normalized:
                if info["assignment_type"] == "user" and info["assignment_code"] not in user_by_code:
                    raise UserError(
                        _("Card %(tid)s owner User %(code)s was not found. Run users/sync first.")
                        % {"tid": info["tid"], "code": info["assignment_code"]}
                    )
                if info["assignment_type"] == "vehicle" and info["assignment_code"].upper() not in vehicle_by_code:
                    raise UserError(
                        _("Card %(tid)s owner Vehicle %(code)s was not found. Run vehicles/sync first.")
                        % {"tid": info["tid"], "code": info["assignment_code"]}
                    )

            card_ids = [info["card"].id for info in normalized]
            user_lines = UserLine.search([("card_id", "in", card_ids)]) if card_ids else UserLine.browse()
            vehicle_lines = VehicleLine.search([("card_id", "in", card_ids)]) if card_ids else VehicleLine.browse()
            user_line_by_pair = {(line.card_id.id, line.user_id.id): line for line in user_lines}
            vehicle_line_by_pair = {(line.card_id.id, line.vehicle_id.id): line for line in vehicle_lines}

            desired = {}
            for info in normalized:
                card = info["card"]
                if info["assignment_type"] == "user":
                    owner = user_by_code[info["assignment_code"]]
                    desired[card.id] = ("user", owner.id)
                elif info["assignment_type"] == "vehicle":
                    owner = vehicle_by_code[info["assignment_code"].upper()]
                    desired[card.id] = ("vehicle", owner.id)
                else:
                    desired[card.id] = ("unassigned", 0)

            revoke_users = user_lines.filtered(
                lambda line: line.state == "active" and desired.get(line.card_id.id) != ("user", line.user_id.id)
            )
            revoke_vehicles = vehicle_lines.filtered(
                lambda line: line.state == "active" and desired.get(line.card_id.id) != ("vehicle", line.vehicle_id.id)
            )
            if revoke_users:
                revoke_users.action_revoke()
            if revoke_vehicles:
                revoke_vehicles.action_revoke()

            create_user_vals = []
            create_vehicle_vals = []
            for info in normalized:
                card = info["card"]
                assignment_type = info["assignment_type"]
                counts[{"user": "user_cards", "vehicle": "vehicle_cards", "unassigned": "unassigned_cards"}[assignment_type]] += 1

                if assignment_type == "unassigned":
                    assignment_by_card[card.id] = card
                    continue

                if assignment_type == "user":
                    owner = user_by_code[info["assignment_code"]]
                    line = user_line_by_pair.get((card.id, owner.id))
                    vals = {"state": "active", "revoked_at": False}
                    if info["assigned_at"]:
                        vals["assigned_at"] = info["assigned_at"]
                    if line:
                        changed = {name: value for name, value in vals.items() if line[name] != value}
                        if changed:
                            line.write(changed)
                        assignment_by_card[card.id] = line
                    else:
                        vals.update({"user_id": owner.id, "card_id": card.id})
                        create_user_vals.append((card.id, vals))
                    continue

                owner = vehicle_by_code[info["assignment_code"].upper()]
                line = vehicle_line_by_pair.get((card.id, owner.id))
                vals = {"state": "active", "revoked_at": False}
                if info["assigned_at"]:
                    vals["assigned_at"] = info["assigned_at"]
                if line:
                    changed = {name: value for name, value in vals.items() if line[name] != value}
                    if changed:
                        line.write(changed)
                    assignment_by_card[card.id] = line
                else:
                    vals.update({"vehicle_id": owner.id, "card_id": card.id})
                    create_vehicle_vals.append((card.id, vals))

            if create_user_vals:
                created = UserLine.create([vals for _card_id, vals in create_user_vals])
                for (card_id, _vals), line in zip(create_user_vals, created):
                    assignment_by_card[card_id] = line
            if create_vehicle_vals:
                created = VehicleLine.create([vals for _card_id, vals in create_vehicle_vals])
                for (card_id, _vals), line in zip(create_vehicle_vals, created):
                    assignment_by_card[card_id] = line

            stale = Card.search([("tid", "not in", tids)]) if tids else Card.search([])
            removed = len(stale)
            if stale:
                stale.unlink()

        Record = self.env["nsp.sync.record"].sudo()
        for info in normalized:
            card = info["card"]
            assignment_type = info["assignment_type"]
            record = assignment_by_card.get(card.id, card)
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
                response=info["source"],
                operation="pull",
            )
        return counts, removed

    @api.model
    def _apply_vehicle_borrow(self, item, cache=None):
        code = str(item.get("borrow_uid") or "").strip()
        if not code:
            raise UserError(_("Borrow UID is required."))
        cache = cache or self._prepare_apply_cache("vehicle_borrow", [item])
        Borrow = self.env["nsp.vehicle.borrow"].sudo()
        borrow = cache.get("borrow_by_code", {}).get(code)
        vehicle_code = str(item.get("vehicle_code") or "").strip().upper()
        vehicle = cache.get("vehicle_by_code", {}).get(vehicle_code)
        borrower_code = str(item.get("borrower_user_code") or "").strip().upper()
        borrower = cache.get("user_by_code", {}).get(borrower_code)
        if not vehicle or not borrower:
            raise UserError(_("Vehicle and borrower must exist before Vehicle Borrow sync."))
        valid_from = self._remote_datetime(item.get("valid_from")) or fields.Datetime.now()
        valid_to = self._remote_datetime(item.get("valid_to")) or fields.Datetime.to_string(
            fields.Datetime.to_datetime(valid_from) + timedelta(days=1)
        )
        if fields.Datetime.to_datetime(valid_to) <= fields.Datetime.to_datetime(valid_from):
            raise UserError(_("Vehicle Borrow valid_to must be later than valid_from."))
        state = str(item.get("state") or "active").strip().lower()
        if state not in ("active", "returned", "cancelled"):
            raise UserError(_("Invalid Vehicle Borrow state: %s") % state)
        returned_at = (
            self._remote_datetime(item.get("returned_at"))
            if "returned_at" in item and item.get("returned_at")
            else (borrow.returned_at if borrow else False)
        )
        if state in ("active", "cancelled"):
            returned_at = False
        vals = {
            "vehicle_id": vehicle.id,
            "borrower_id": borrower.id,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "state": state,
            "returned_at": returned_at,
        }
        if borrow:
            self._write_changed(borrow.with_context(vehicle_borrow_sync=True), vals)
            return borrow
        vals["borrow_code"] = code
        borrow = Borrow.with_context(vehicle_borrow_sync=True).create(vals)
        cache.setdefault("borrow_by_code", {})[code] = borrow
        return borrow

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

        keys = set()
        for device_item in item.get("measurement_antennas") or []:
            if not isinstance(device_item, dict):
                raise UserError(_("Measurement Antennas must contain objects."))
            serial = str(device_item.get("serial_number") or "").strip().upper()
            numbers = device_item.get("antennas")
            if not serial or not isinstance(numbers, list) or not numbers:
                raise UserError(_("Each Measurement Reader requires serial_number and antennas."))
            for raw_number in numbers:
                try:
                    antenna_no = int(raw_number)
                except Exception as exc:
                    raise UserError(_("Invalid Measurement Antenna number.")) from exc
                key = (serial, antenna_no)
                if antenna_no <= 0 or key in keys:
                    raise UserError(
                        _("Invalid or duplicate Measurement Antenna %s/%s.")
                        % (serial, raw_number)
                    )
                keys.add(key)
        if not keys:
            raise UserError(_("Measurement Configuration has no antennas."))

        Device = self.env["nsp.device"].sudo()
        serials = {serial for serial, _antenna_no in keys}
        existing_devices = Device.search([("serial_number", "in", list(serials))])
        device_by_serial = {device.serial_number: device for device in existing_devices}
        missing_serials = sorted(serials - set(device_by_serial))
        if missing_serials:
            created_devices = Device.create([
                {"serial_number": serial, "controller_id": controller.id}
                for serial in missing_serials
            ])
            device_by_serial.update({device.serial_number: device for device in created_devices})
        for device in device_by_serial.values():
            self._write_changed(device, {"controller_id": controller.id})

        Antenna = self.env["nsp.device.antenna"].sudo()
        device_ids = [device.id for device in device_by_serial.values()]
        antenna_numbers = {antenna_no for _serial, antenna_no in keys}
        existing_antennas = Antenna.search([
            ("device_id", "in", device_ids),
            ("antenna_no", "in", list(antenna_numbers)),
        ])
        antenna_by_key = {
            (antenna.device_id.serial_number, int(antenna.antenna_no or 0)): antenna
            for antenna in existing_antennas
        }
        missing_keys = sorted(keys - set(antenna_by_key))
        if missing_keys:
            created_antennas = Antenna.create([
                {
                    "device_id": device_by_serial[serial].id,
                    "antenna_no": antenna_no,
                }
                for serial, antenna_no in missing_keys
            ])
            antenna_by_key.update({
                (antenna.device_id.serial_number, int(antenna.antenna_no or 0)): antenna
                for antenna in created_antennas
            })
        antenna_refs = Antenna.browse([antenna_by_key[key].id for key in sorted(keys)])

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
        if session:
            self._write_changed(session, vals)
            if set(session.antenna_ids.ids) != set(antenna_refs.ids):
                session.write({"antenna_ids": [(6, 0, antenna_refs.ids)]})
        else:
            vals["antenna_ids"] = [(6, 0, antenna_refs.ids)]
            session = Session.create(vals)
        return session

    def _apply_parking_config(self, item):
        """Apply one full Parking Area snapshot without per-row lookup queries."""
        self.ensure_one()
        if not isinstance(item, dict):
            raise UserError(_("Parking Configuration item must be an object."))

        branch_code = str(item.get("branch_code") or "").strip().upper()
        area_code = str(item.get("parking_area_code") or "").strip().upper()
        if not branch_code or not area_code:
            raise UserError(_("Branch Code and Parking Area Code are required."))
        state = str(item.get("state") or "draft").strip().lower()
        if state not in ("draft", "operational", "maintenance", "blocked"):
            raise UserError(_("Invalid Parking Area state: %s") % state)

        branch = self.env["nsp.branch"].sudo().with_context(active_test=False).search(
            [("code", "=", branch_code)], limit=1
        )
        if not branch:
            raise UserError(
                _("Branch %(code)s was not found. Run branches/sync before parking-config/sync.")
                % {"code": branch_code}
            )

        Parking = self.env["nsp.parking.area"].sudo()
        parking = Parking.search([("code", "=", area_code)], limit=1)
        try:
            motorbike_capacity = int(item.get("motorbike_capacity") or 0)
        except (TypeError, ValueError) as exc:
            raise UserError(_("Motorbike Capacity must be an integer.")) from exc
        if motorbike_capacity < 0:
            raise UserError(_("Motorbike Capacity cannot be negative."))

        parking_vals = {
            "code": area_code,
            "name": str(item.get("parking_area_name") or area_code).strip(),
            "branch_id": branch.id,
            "state": state,
            "motorbike_capacity": motorbike_capacity,
        }
        if parking:
            self._write_changed(parking, parking_vals)
        else:
            parking = Parking.create(parking_vals)

        controllers_data = item.get("controllers") or []
        if not isinstance(controllers_data, list):
            raise UserError(_("Parking controllers must be an array."))
        controller_specs = {}
        reader_specs = {}
        antenna_specs = {}
        allowed_connections = {
            key for key, _label in self.env["nsp.device"]._fields["connection_type"].selection
        }
        for controller_item in controllers_data:
            if not isinstance(controller_item, dict):
                raise UserError(_("Parking controllers must contain objects."))
            unsupported = set(controller_item) - {"controller_code", "controller_name", "devices"}
            if unsupported:
                raise UserError(
                    _("Unsupported Parking Controller field(s): %s") % ", ".join(sorted(unsupported))
                )
            controller_code = str(controller_item.get("controller_code") or "").strip().upper()
            if not controller_code or controller_code in controller_specs:
                raise UserError(_("Parking Controller Code is missing or duplicated."))
            controller_specs[controller_code] = {
                "name": str(controller_item.get("controller_name") or controller_code).strip(),
            }
            devices_data = controller_item.get("devices") or []
            if not isinstance(devices_data, list):
                raise UserError(_("Controller devices must be an array."))
            for device_item in devices_data:
                if not isinstance(device_item, dict):
                    raise UserError(_("Controller devices must contain objects."))
                unsupported_device = set(device_item) - {
                    "serial_number", "reader_name", "model_number", "vendor",
                    "physical_connection", "reader_parameters", "antennas",
                }
                if unsupported_device:
                    raise UserError(
                        _("Unsupported Reader field(s): %s") % ", ".join(sorted(unsupported_device))
                    )
                serial = str(device_item.get("serial_number") or "").strip().upper()
                if not serial or serial in reader_specs:
                    raise UserError(_("Reader Serial Number is missing or duplicated in Parking Configuration."))
                reader_parameters = device_item.get("reader_parameters") or {}
                if not isinstance(reader_parameters, dict):
                    raise UserError(_("reader_parameters must be an object."))
                unsupported_params = set(reader_parameters) - {
                    "power_dbm", "read_interval_ms", "tid_start_address", "tid_length",
                }
                if unsupported_params:
                    raise UserError(
                        _("Unsupported Reader parameter(s): %s") % ", ".join(sorted(unsupported_params))
                    )
                connection_type = device_item.get("physical_connection") or False
                if connection_type and connection_type not in allowed_connections:
                    raise UserError(_("Invalid Physical Connection for Reader %s.") % serial)
                try:
                    reader_specs[serial] = {
                        "controller_code": controller_code,
                        "name": str(device_item.get("reader_name") or serial).strip(),
                        "model_number": str(device_item.get("model_number") or "").strip() or False,
                        "device_vendor": str(device_item.get("vendor") or "").strip() or False,
                        "connection_type": connection_type,
                        "power_dbm": int(
                            reader_parameters.get("power_dbm")
                            if reader_parameters.get("power_dbm") is not None else 30
                        ),
                        "read_interval_ms": int(reader_parameters.get("read_interval_ms") or 200),
                        "tid_addr": int(reader_parameters.get("tid_start_address") or 0),
                        "tid_len": int(reader_parameters.get("tid_length") or 4),
                    }
                except (TypeError, ValueError) as exc:
                    raise UserError(_("Invalid Reader technical parameter for %s.") % serial) from exc

                antennas_data = device_item.get("antennas") or []
                if not isinstance(antennas_data, list):
                    raise UserError(_("Reader antennas must be an array."))
                for antenna_item in antennas_data:
                    if not isinstance(antenna_item, dict):
                        raise UserError(_("Reader antennas must contain objects."))
                    unsupported_antenna = set(antenna_item) - {"antenna_no", "minimum_rssi_dbm"}
                    if unsupported_antenna:
                        raise UserError(
                            _("Unsupported Reader Antenna field(s): %s")
                            % ", ".join(sorted(unsupported_antenna))
                        )
                    try:
                        antenna_no = int(antenna_item.get("antenna_no") or 0)
                        minimum_rssi = float(
                            antenna_item.get("minimum_rssi_dbm")
                            if antenna_item.get("minimum_rssi_dbm") is not None else -65.0
                        )
                    except (TypeError, ValueError) as exc:
                        raise UserError(_("Invalid Antenna configuration for Reader %s.") % serial) from exc
                    key = (serial, antenna_no)
                    if antenna_no <= 0 or key in antenna_specs:
                        raise UserError(_("Invalid or duplicate Antenna No for Reader %s.") % serial)
                    antenna_specs[key] = {"minimum_rssi_dbm": minimum_rssi}

        edge = self._require_edge_server_record()
        Controller = self.env["nsp.controller"].sudo().with_context(active_test=False)
        existing_controllers = Controller.search([
            ("controller_id", "in", list(controller_specs)),
        ]) if controller_specs else Controller.browse()
        controller_by_code = {controller.controller_id: controller for controller in existing_controllers}
        missing_controller_codes = sorted(set(controller_specs) - set(controller_by_code))
        if missing_controller_codes:
            created_controllers = Controller.create([
                {
                    "controller_id": code,
                    "controller_name": controller_specs[code]["name"],
                    "edge_server_id": edge.id,
                    "active": True,
                }
                for code in missing_controller_codes
            ])
            controller_by_code.update({controller.controller_id: controller for controller in created_controllers})
        for code, spec in controller_specs.items():
            self._write_changed(controller_by_code[code], {
                "controller_name": spec["name"],
                "edge_server_id": edge.id,
                "active": True,
            })

        Device = self.env["nsp.device"].sudo()
        existing_devices = Device.search([
            ("serial_number", "in", list(reader_specs)),
        ]) if reader_specs else Device.browse()
        device_by_serial = {device.serial_number: device for device in existing_devices}
        missing_serials = sorted(set(reader_specs) - set(device_by_serial))
        if missing_serials:
            created_devices = Device.create([
                {
                    "serial_number": serial,
                    "name": reader_specs[serial]["name"],
                    "controller_id": controller_by_code[reader_specs[serial]["controller_code"]].id,
                }
                for serial in missing_serials
            ])
            device_by_serial.update({device.serial_number: device for device in created_devices})
        for serial, spec in reader_specs.items():
            values = dict(spec)
            controller_code = values.pop("controller_code")
            values["controller_id"] = controller_by_code[controller_code].id
            self._write_changed(device_by_serial[serial], values)

        Antenna = self.env["nsp.device.antenna"].sudo()
        antenna_numbers = {number for _serial, number in antenna_specs}
        existing_antennas = Antenna.search([
            ("device_id", "in", [device.id for device in device_by_serial.values()]),
            ("antenna_no", "in", list(antenna_numbers)),
        ]) if device_by_serial and antenna_numbers else Antenna.browse()
        antenna_by_key = {
            (antenna.device_id.serial_number, int(antenna.antenna_no or 0)): antenna
            for antenna in existing_antennas
        }
        missing_antenna_keys = sorted(set(antenna_specs) - set(antenna_by_key))
        if missing_antenna_keys:
            created_antennas = Antenna.create([
                {
                    "device_id": device_by_serial[serial].id,
                    "antenna_no": antenna_no,
                    "minimum_rssi_dbm": antenna_specs[(serial, antenna_no)]["minimum_rssi_dbm"],
                }
                for serial, antenna_no in missing_antenna_keys
            ])
            antenna_by_key.update({
                (antenna.device_id.serial_number, int(antenna.antenna_no or 0)): antenna
                for antenna in created_antennas
            })
        for key, spec in antenna_specs.items():
            self._write_changed(antenna_by_key[key], spec)

        lanes_data = item.get("lanes") or []
        if not isinstance(lanes_data, list):
            raise UserError(_("Parking lanes must be an array."))
        lane_specs = {}
        desired_mapping_specs = []
        desired_antenna_ids = set()
        for lane_index, lane_item in enumerate(lanes_data, start=1):
            if not isinstance(lane_item, dict):
                raise UserError(_("Parking lanes must contain objects."))
            unsupported_lane = set(lane_item) - {
                "lane_code", "lane_name", "lane_no", "controller_code", "direction",
                "transition_window_seconds", "grouping_window_seconds",
                "repeat_suppression_seconds", "antenna_mappings",
            }
            if unsupported_lane:
                raise UserError(
                    _("Unsupported Parking Lane field(s): %s") % ", ".join(sorted(unsupported_lane))
                )
            lane_code = str(lane_item.get("lane_code") or "").strip().upper()
            controller_code = str(lane_item.get("controller_code") or "").strip().upper()
            direction = str(lane_item.get("direction") or "").strip().lower()
            if not lane_code or lane_code in lane_specs or not controller_code:
                raise UserError(_("Parking Lane Code is missing or duplicated."))
            if controller_code not in controller_by_code:
                raise UserError(
                    _("Controller %s is missing from Parking controller configuration.") % controller_code
                )
            if direction not in ("entry", "exit", "both"):
                raise UserError(_("Parking Lane direction must be entry, exit or both."))
            try:
                transition_window = int(lane_item.get("transition_window_seconds") or 10)
                grouping_window = int(lane_item.get("grouping_window_seconds") or 3)
                repeat_suppression = int(lane_item.get("repeat_suppression_seconds") or 1)
                lane_no = int(lane_item.get("lane_no") or lane_index)
            except (TypeError, ValueError) as exc:
                raise UserError(_("Parking Lane timing and lane number values must be integers.")) from exc
            if min(transition_window, grouping_window, repeat_suppression, lane_no) < 1:
                raise UserError(_("Parking Lane timing values and Lane No. must be at least one."))
            lane_specs[lane_code] = {
                "parking_area_id": parking.id,
                "code": lane_code,
                "name": str(lane_item.get("lane_name") or lane_code).strip(),
                "controller_id": controller_by_code[controller_code].id,
                "lane_no": lane_no,
                "direction": direction,
                "transition_window_seconds": transition_window,
                "grouping_window_seconds": grouping_window,
                "repeat_suppression_seconds": repeat_suppression,
                "active": True,
            }

            mappings_data = lane_item.get("antenna_mappings") or []
            if not isinstance(mappings_data, list):
                raise UserError(_("Antenna mappings must be an array."))
            for mapping_item in mappings_data:
                if not isinstance(mapping_item, dict):
                    raise UserError(_("Antenna mappings must contain objects."))
                unsupported_mapping = set(mapping_item) - {"serial_number", "antenna_no", "zone"}
                if unsupported_mapping:
                    raise UserError(
                        _("Unsupported Antenna Mapping field(s): %s")
                        % ", ".join(sorted(unsupported_mapping))
                    )
                serial = str(mapping_item.get("serial_number") or "").strip().upper()
                try:
                    antenna_no = int(mapping_item.get("antenna_no") or 0)
                except (TypeError, ValueError) as exc:
                    raise UserError(_("Invalid antenna_no in Parking Lane mapping.")) from exc
                mapping_zone = str(mapping_item.get("zone") or "").strip().lower()
                antenna = antenna_by_key.get((serial, antenna_no))
                if not antenna:
                    raise UserError(
                        _("Antenna %s/%s is missing from Reader configuration.") % (serial, antenna_no)
                    )
                if antenna.device_id.controller_id.id != controller_by_code[controller_code].id:
                    raise UserError(_("Antenna must belong to the Controller assigned to this Lane."))
                if direction == "both" and mapping_zone not in ("outside", "inside"):
                    raise UserError(_("A Two-way Lane antenna mapping requires zone outside or inside."))
                if direction != "both" and mapping_zone:
                    raise UserError(_("A one-way Lane antenna mapping must not define zone."))
                if antenna.id in desired_antenna_ids:
                    raise UserError(_("An antenna can be mapped to only one Parking Lane."))
                desired_antenna_ids.add(antenna.id)
                desired_mapping_specs.append((lane_code, antenna.id, mapping_zone or False))

        Lane = self.env["nsp.parking.lane"].sudo().with_context(active_test=False)
        existing_lanes = Lane.search([
            ("parking_area_id", "=", parking.id),
            ("code", "in", list(lane_specs)),
        ]) if lane_specs else Lane.browse()
        lane_by_code = {lane.code: lane for lane in existing_lanes}
        for lane_code, lane_vals in lane_specs.items():
            lane = lane_by_code.get(lane_code)
            if lane:
                self._write_changed(lane, lane_vals)
            else:
                lane = Lane.create(lane_vals)
                lane_by_code[lane_code] = lane

        Mapping = self.env["nsp.parking.lane.antenna.mapping"].sudo()
        desired_mappings = Mapping.search([
            ("antenna_ref_id", "in", list(desired_antenna_ids)),
        ]) if desired_antenna_ids else Mapping.browse()
        mapping_by_antenna = {mapping.antenna_ref_id.id: mapping for mapping in desired_mappings}
        create_mapping_vals = []
        for lane_code, antenna_id, zone in desired_mapping_specs:
            mapping_vals = {
                "lane_id": lane_by_code[lane_code].id,
                "antenna_ref_id": antenna_id,
                "zone": zone,
            }
            mapping = mapping_by_antenna.get(antenna_id)
            if mapping:
                self._write_changed(mapping, mapping_vals)
            else:
                create_mapping_vals.append(mapping_vals)
        if create_mapping_vals:
            Mapping.create(create_mapping_vals)

        area_lanes = Lane.search([("parking_area_id", "=", parking.id)])
        current_area_mappings = Mapping.search([("lane_id", "in", area_lanes.ids)]) if area_lanes else Mapping.browse()
        stale_mappings = current_area_mappings.filtered(
            lambda mapping: mapping.antenna_ref_id.id not in desired_antenna_ids
        )
        if stale_mappings:
            stale_mappings.unlink()

        incoming_codes = set(lane_specs)
        stale_lanes = area_lanes.filtered(lambda lane: lane.code not in incoming_codes and lane.active)
        if stale_lanes:
            stale_lanes.mapped("antenna_mapping_ids").unlink()
            stale_lanes.write({"active": False})

        if parking.state == "operational":
            issues = parking._operational_issues()
            if issues:
                raise UserError("; ".join(str(issue) for issue in issues))
        return parking

    def _reconcile_parking_config_snapshot(self, items):
        self.ensure_one()
        incoming_codes = {
            str(item.get("parking_area_code") or "").strip().upper()
            for item in (items or [])
            if isinstance(item, dict) and item.get("parking_area_code")
        }
        Parking = self.env["nsp.parking.area"].sudo()
        stale = Parking.search([("code", "not in", list(incoming_codes))]) if incoming_codes else Parking.search([])
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
        normalized_items = items if isinstance(items, list) else []
        cached_kinds = {"device_whitelist", "branch", "user", "vehicle", "vehicle_borrow"}
        apply_cache = self._prepare_apply_cache(kind, normalized_items) if kind in cached_kinds else None
        for index, item in enumerate(normalized_items):
            key = self._record_key_from_item(item)
            try:
                with self.env.cr.savepoint():
                    record = handler(item, cache=apply_cache) if kind in cached_kinds else handler(item)
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
