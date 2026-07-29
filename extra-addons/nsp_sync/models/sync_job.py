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
    "gatekeeper-config/sync": "pull",
    "users/sync": "pull",
    "vehicle-config/sync": "pull",
    "vehicles/sync": "pull",
    "cards/sync": "pull",
    "vehicle-borrow/sync": "pull",
    "measurement-config/sync": "pull",
    "measurement-events/sync": "push",
    "measurement-status/sync": "push",
    "parking-transactions/sync": "push",
}
NSP_SYNC_ALLOWED_ROUTES = tuple(SYNC_ROUTE_DIRECTIONS)
JOB_SEQUENCE = {route: sequence * 10 for sequence, route in enumerate(NSP_SYNC_ALLOWED_ROUTES, start=1)}
DEFAULT_JOB_SETTINGS = {
    "edge-server/status": {"schedule_interval_minutes": 1, "batch_size": 1},
    "gatekeeper-config/sync": {"schedule_interval_minutes": 1, "batch_size": 1},
    "users/sync": {"schedule_interval_minutes": 5, "batch_size": 500},
    "vehicle-config/sync": {"schedule_interval_minutes": 5, "batch_size": 1000},
    "vehicles/sync": {"schedule_interval_minutes": 5, "batch_size": 500},
    "cards/sync": {"schedule_interval_minutes": 5, "batch_size": 1000},
    "vehicle-borrow/sync": {"schedule_interval_minutes": 5, "batch_size": 500},
    "measurement-config/sync": {"schedule_interval_minutes": 1, "batch_size": 100},
    "measurement-events/sync": {"schedule_interval_minutes": 1, "batch_size": 100},
    "measurement-status/sync": {"schedule_interval_minutes": 1, "batch_size": 100},
    "parking-transactions/sync": {"schedule_interval_minutes": 1, "batch_size": 200},
}
ACTION_KINDS = {
    "edge-server/status": "edge_server_status",
    "gatekeeper-config/sync": "gatekeeper_config",
    "users/sync": "user",
    "vehicle-config/sync": "vehicle_config",
    "vehicles/sync": "vehicle",
    "cards/sync": "card",
    "vehicle-borrow/sync": "vehicle_borrow",
    "measurement-config/sync": "measurement_config",
    "measurement-events/sync": "measurement_event",
    "measurement-status/sync": "measurement_status",
    "parking-transactions/sync": "parking_transaction",
}

class NspSyncJob(models.Model):
    _name = "nsp.sync.job"
    _description = "NSP Sync Job"

    @api.model
    def api_client_route_not_available(self):
        """Reject inbound calls on Edge-only remote-route metadata.

        The records in ``sync_route_definitions.xml`` are local descriptors used
        by outbound jobs to build Cloud URLs. Cloud implementations live in
        ``nsp_master_gatekeeper`` and are not provided by this Edge transport.
        """
        return {
            "status_code": 403,
            "message": "NSP Sync routes are outbound-only on Edge",
            "data": {
                "success": False,
                "error_code": "route_not_allowed",
                "message": "NSP Sync routes are outbound-only on Edge",
                "details": {},
            },
        }
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
    snapshot_revision = fields.Integer(string="Applied Snapshot Revision", readonly=True, copy=False, default=0)
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
    def _require_edge_server_record(self):
        """One Edge database represents exactly one Edge node.

        Edge Server is a Cloud master object and is deliberately not stored on Edge.
        Runtime scope is the local database plus auth_id.edge_server_code.
        """
        self.ensure_one()
        return False

    # --------------------------- push payloads ------------------------
    def _serialize_edge_server_status(self):
        self.ensure_one()
        controllers=[]
        for controller in self.env["nsp.controller"].sudo().search([("active","=",True)], order="controller_id,id"):
            devices=[]
            for device in controller.device_ids.filtered("active").sorted(key=lambda r:(r.serial_number or "",r.id)):
                status=str(device.status or "offline").lower()
                devices.append({"serial_number":device.serial_number or "","antennas":sorted(int(n) for n in device.antennas_ids.filtered("active").mapped("antenna_no")),"device_status":status if status in ("online","offline","degraded") else "offline","last_seen_at":self._dt(device.last_seen) if device.last_seen else False,**({"firmware_version":device.firmware_version} if device.firmware_version else {})})
            controllers.append({"controller_code":controller.controller_id or "","current_status":str(controller.status or "offline").lower(),"last_seen_at":self._dt(controller.timestamp) if controller.timestamp else False,"devices":devices})
        return {"record_key":self.edge_server_code,"edge_server_code":self.edge_server_code,"current_status":"online","last_seen_at":self._dt(fields.Datetime.now()),"controllers":controllers}

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
            "license_plate": record.vehicle_id.license_plate if record.vehicle_id else "",
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
        domain = self._push_cursor_domain()
        if kind == "parking_transaction":
            records = self.env["nsp.parking.transaction"].sudo().search(
                domain,
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
        self.ensure_one(); code=str(code or "").strip().upper(); Controller=self.env["nsp.controller"].sudo().with_context(active_test=False)
        if not code: return Controller.browse()
        controller=Controller.search([("controller_id","=",code)],limit=1)
        vals={"controller_name":name or (controller.controller_name if controller else code),"active":True,"cloud_removed":False}
        if controller: self._write_changed(controller,vals); return controller
        vals["controller_id"]=code; return Controller.create(vals)

    @api.model
    def _prepare_apply_cache(self, kind, items):
        """Preload master records used by high-volume pull snapshots."""
        rows = [item for item in (items or []) if isinstance(item, dict)]
        if kind == "device_whitelist":
            serials = {str(item.get("serial_number") or "").strip().upper() for item in rows}
            type_codes = {str(item.get("device_type_code") or "").strip().upper() for item in rows}
            serials.discard("")
            type_codes.discard("")
            records = self.env["nsp.device.whitelist"].sudo().search([
                ("serial_number", "in", list(serials)),
            ]) if serials else self.env["nsp.device.whitelist"].browse()
            device_types = self.env["nsp.device.type"].sudo().with_context(active_test=False).search([
                ("code", "in", list(type_codes)),
            ]) if type_codes else self.env["nsp.device.type"].browse()
            return {
                "records": {record.serial_number: record for record in records},
                "device_types": {record.code: record for record in device_types},
            }

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
                ("brand_by_code", "nsp.reference.brand", "brand_code"),
                ("model_by_code", "nsp.reference.model", "model_code"),
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

        type_code = str(item.get("device_type_code") or "").strip().upper()
        type_name = str(item.get("device_type_name") or type_code).strip()
        if not type_code:
            raise UserError(_("Device Type Code is required for Device Whitelist %(serial)s.") % {"serial": serial})

        cache = cache or self._prepare_apply_cache("device_whitelist", [item])
        DeviceType = self.env["nsp.device.type"].sudo().with_context(active_test=False)
        device_type = cache.get("device_types", {}).get(type_code)
        if not device_type:
            device_type = DeviceType.create({
                "code": type_code,
                "name": type_name or type_code,
                "active": True,
            })
            cache.setdefault("device_types", {})[type_code] = device_type
        elif type_name and device_type.name != type_name:
            device_type.write({"name": type_name})

        Whitelist = self.env["nsp.device.whitelist"].sudo()
        record = cache.get("records", {}).get(serial)
        vals = {
            "serial_number": serial,
            "device_type_id": device_type.id,
        }
        if record:
            changed = {}
            for name, value in vals.items():
                current = record[name].id if record._fields[name].type == "many2one" else record[name]
                if current != value:
                    changed[name] = value
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
                "nsp.reference.brand", groups["brands"]
            )
            applied.extend(("brand", item, record) for item, record in brand_rows)

            Brand = self.env["nsp.reference.brand"].sudo().with_context(active_test=False)
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
                "nsp.reference.model", groups["models"], extra_values=model_extra
            )
            applied.extend(("model", item, record) for item, record in model_rows)

            color_rows, codes["colors"] = self._vehicle_master_snapshot_group(
                "nsp.vehicle.color", groups["colors"]
            )
            applied.extend(("color", item, record) for item, record in color_rows)

            removed = {
                "vehicle_types": self._reconcile_vehicle_master_snapshot("nsp.vehicle.type", codes["vehicle_types"]),
                "brands": self._reconcile_vehicle_master_snapshot("nsp.reference.brand", codes["brands"]),
                "models": self._reconcile_vehicle_master_snapshot("nsp.reference.model", codes["models"]),
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
        target_tid = self.env["nsp.rfid.card"].sudo()._normalize_tid(item.get("target_tid"))
        reader_payloads = item.get("readers")
        if not code or not controller_code or not target_tid:
            raise UserError(
                _("Measurement Code, Controller Code and Target TID are required.")
            )
        if not isinstance(reader_payloads, list) or not reader_payloads:
            raise UserError(_("Measurement Configuration must contain at least one Reader."))

        status = str(item.get("status") or "ready").strip().lower()
        if status not in ("ready", "running", "completed", "applied", "failed", "cancelled"):
            raise UserError(_("Invalid Measurement Session status: %s") % status)
        try:
            revision = max(int(item.get("revision") or 1), 1)
        except (TypeError, ValueError) as exc:
            raise UserError(_("Invalid Measurement revision.")) from exc

        controller = self._find_or_create_controller(controller_code)
        Card = self.env["nsp.rfid.card"].sudo()
        target_card = Card.search([("tid", "=", target_tid)], limit=1)
        if not target_card:
            raise UserError(_("Target RFID Tag %s has not been synchronized to this Edge Server.") % target_tid)

        Device = self.env["nsp.device"].sudo()
        Antenna = self.env["nsp.device.antenna"].sudo()
        line_commands = []
        seen_serials = set()
        for reader_payload in reader_payloads:
            if not isinstance(reader_payload, dict):
                raise UserError(_("Measurement Reader must be an object."))
            reader_serial = str(reader_payload.get("serial_number") or "").strip().upper()
            if not reader_serial or reader_serial in seen_serials:
                raise UserError(_("Measurement Reader Serial is missing or duplicated."))
            seen_serials.add(reader_serial)
            try:
                measurement_power = int(
                    reader_payload.get("power_dbm")
                    if reader_payload.get("power_dbm") is not None
                    else 30
                )
            except (TypeError, ValueError) as exc:
                raise UserError(_("Invalid Measurement Reader power.")) from exc
            if measurement_power < 0 or measurement_power > 40:
                raise UserError(_("Measurement Reader power must be between 0 and 40 dBm."))

            reader = Device.search([
                ("serial_number", "=", reader_serial),
                ("controller_id", "=", controller.id),
            ], limit=1)
            if not reader:
                reader = Device.create({
                    "serial_number": reader_serial,
                    "name": reader_serial,
                    "controller_id": controller.id,
                })

            numbers = reader_payload.get("antennas")
            if not isinstance(numbers, list) or not numbers:
                raise UserError(_("Measurement Reader %s has no antennas.") % reader_serial)
            keys = set()
            for raw_number in numbers:
                try:
                    antenna_no = int(raw_number)
                except Exception as exc:
                    raise UserError(_("Invalid Measurement Antenna number.")) from exc
                if antenna_no <= 0 or antenna_no in keys:
                    raise UserError(_("Invalid or duplicate Measurement Antenna %s.") % raw_number)
                keys.add(antenna_no)

            existing = Antenna.search([
                ("device_id", "=", reader.id),
                ("antenna_no", "in", list(keys)),
            ])
            antenna_by_no = {int(antenna.antenna_no): antenna for antenna in existing}
            missing = sorted(keys - set(antenna_by_no))
            if missing:
                created = Antenna.create([
                    {"device_id": reader.id, "antenna_no": antenna_no}
                    for antenna_no in missing
                ])
                antenna_by_no.update({int(antenna.antenna_no): antenna for antenna in created})
            antenna_refs = Antenna.browse([antenna_by_no[number].id for number in sorted(keys)])
            line_commands.append((0, 0, {
                "reader_id": reader.id,
                "measurement_power_dbm": measurement_power,
                "antenna_ids": [(6, 0, antenna_refs.ids)],
            }))

        Session = self.env["nsp.measurement.session"].sudo().with_context(measurement_sync=True)
        session = Session.search([("measurement_code", "=", code)], limit=1)
        vals = {
            "measurement_code": code,
            "controller_id": controller.id,
            "target_card_id": target_card.id,
            "revision": revision,
            "status": status,
            "planned_start_at": self._remote_datetime(item.get("planned_start_at")),
            "planned_end_at": self._remote_datetime(item.get("planned_end_at")),
            "note": str(item.get("note") or "").strip() or False,
            "reader_line_ids": [(5, 0, 0)] + line_commands,
        }
        if session:
            session.write(vals)
        else:
            session = Session.create(vals)
        return session

    def _apply_parking_config(self, item):
        """Apply one Parking Area topology snapshot against existing physical cache.

        Controller/Reader/Antenna declarations are synchronized once at the top-level
        Gatekeeper snapshot. Parking areas carry only business topology, avoiding the
        previous duplicate physical configuration inside every Parking Area payload.
        """
        self.ensure_one()
        if not isinstance(item, dict):
            raise UserError(_("Parking Configuration item must be an object."))

        unsupported = set(item) - {
            "parking_area_code", "parking_area_name", "branch_code", "state",
            "motorbike_capacity", "live_monitor_columns", "lanes",
        }
        if unsupported:
            raise UserError(
                _("Unsupported Parking Configuration field(s): %s")
                % ", ".join(sorted(unsupported))
            )

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
                _("Branch %(code)s was not found in the current Gatekeeper configuration snapshot.")
                % {"code": branch_code}
            )

        try:
            motorbike_capacity = int(item.get("motorbike_capacity") or 0)
            live_monitor_columns = int(item.get("live_monitor_columns") or 2)
        except (TypeError, ValueError) as exc:
            raise UserError(_("Parking capacity and monitor columns must be integers.")) from exc
        if motorbike_capacity < 0:
            raise UserError(_("Motorbike Capacity cannot be negative."))
        if live_monitor_columns < 1 or live_monitor_columns > 4:
            raise UserError(_("Live Monitor Columns must be between 1 and 4."))

        Parking = self.env["nsp.parking.area"].sudo()
        parking = Parking.search([("code", "=", area_code)], limit=1)
        parking_vals = {
            "code": area_code,
            "name": str(item.get("parking_area_name") or area_code).strip(),
            "branch_id": branch.id,
            "state": state,
            "motorbike_capacity": motorbike_capacity,
            "live_monitor_columns": live_monitor_columns,
        }
        if parking:
            self._write_changed(parking, parking_vals)
        else:
            parking = Parking.create(parking_vals)

        Controller = self.env["nsp.controller"].sudo().with_context(active_test=False)
        controllers = Controller.search([])
        controller_by_code = {record.controller_id: record for record in controllers}

        Antenna = self.env["nsp.device.antenna"].sudo().with_context(active_test=False)
        antennas = Antenna.search([])
        antenna_by_key = {
            (record.device_id.serial_number, int(record.antenna_no or 0)): record
            for record in antennas
        }

        lanes_data = item.get("lanes") or []
        if not isinstance(lanes_data, list):
            raise UserError(_("Parking lanes must be an array."))

        lane_specs = {}
        transition_specs = []
        for lane_index, lane_item in enumerate(lanes_data, start=1):
            if not isinstance(lane_item, dict):
                raise UserError(_("Parking lanes must contain objects."))
            unsupported_lane = set(lane_item) - {
                "lane_code", "lane_name", "lane_no", "controller_code",
                "antenna_transitions",
            }
            if unsupported_lane:
                raise UserError(
                    _("Unsupported Parking Lane field(s): %s")
                    % ", ".join(sorted(unsupported_lane))
                )

            lane_code = str(lane_item.get("lane_code") or "").strip().upper()
            controller_code = str(lane_item.get("controller_code") or "").strip().upper()
            if not lane_code or lane_code in lane_specs or not controller_code:
                raise UserError(_("Parking Lane Code is missing or duplicated."))
            controller = controller_by_code.get(controller_code)
            if not controller or not controller.active or controller.cloud_removed:
                raise UserError(
                    _("Controller %s is missing or inactive in Gatekeeper configuration.")
                    % controller_code
                )
            try:
                lane_no = int(lane_item.get("lane_no") or lane_index)
            except (TypeError, ValueError) as exc:
                raise UserError(_("Parking Lane No. must be an integer.")) from exc
            if lane_no < 1:
                raise UserError(_("Parking Lane No. must be at least one."))

            lane_specs[lane_code] = {
                "parking_area_id": parking.id,
                "code": lane_code,
                "name": str(lane_item.get("lane_name") or lane_code).strip(),
                "controller_id": controller.id,
                "lane_no": lane_no,
                "active": True,
            }

            transitions_data = lane_item.get("antenna_transitions") or []
            if not isinstance(transitions_data, list):
                raise UserError(_("Antenna transitions must be an array."))
            if state == "operational" and not transitions_data:
                raise UserError(
                    _("Operational Lane %s requires at least one Antenna Transition.")
                    % lane_code
                )

            seen_paths = set()
            for transition_item in transitions_data:
                if not isinstance(transition_item, dict):
                    raise UserError(_("Antenna transitions must contain objects."))
                unsupported_transition = set(transition_item) - {
                    "from_serial_number", "from_antenna_no",
                    "to_serial_number", "to_antenna_no",
                    "event_type", "duration_seconds",
                }
                if unsupported_transition:
                    raise UserError(
                        _("Unsupported Antenna Transition field(s): %s")
                        % ", ".join(sorted(unsupported_transition))
                    )

                from_serial = str(transition_item.get("from_serial_number") or "").strip().upper()
                to_serial = str(transition_item.get("to_serial_number") or "").strip().upper()
                event_type = str(transition_item.get("event_type") or "").strip().lower()
                try:
                    from_no = int(transition_item.get("from_antenna_no") or 0)
                    to_no = int(transition_item.get("to_antenna_no") or 0)
                    duration = float(transition_item.get("duration_seconds") or 0.0)
                except (TypeError, ValueError) as exc:
                    raise UserError(_("Invalid Antenna Transition number or Duration.")) from exc
                if not from_serial or not to_serial or from_no <= 0 or to_no <= 0:
                    raise UserError(_("Antenna Transition requires valid From/To antennas."))
                if event_type not in ("check_in", "check_out"):
                    raise UserError(_("Antenna Transition Event Type must be check_in or check_out."))
                if duration <= 0:
                    raise UserError(_("Antenna Transition Duration must be greater than zero."))

                from_antenna = antenna_by_key.get((from_serial, from_no))
                to_antenna = antenna_by_key.get((to_serial, to_no))
                if not from_antenna or not to_antenna:
                    raise UserError(
                        _("Antenna Transition references an antenna missing from Reader configuration.")
                    )
                if (
                    not from_antenna.active
                    or from_antenna.cloud_removed
                    or not from_antenna.device_id.active
                    or from_antenna.device_id.cloud_removed
                    or not to_antenna.active
                    or to_antenna.cloud_removed
                    or not to_antenna.device_id.active
                    or to_antenna.device_id.cloud_removed
                ):
                    raise UserError(
                        _("Antenna Transition references an inactive or removed Reader/Antenna.")
                    )
                if from_antenna == to_antenna:
                    raise UserError(_("From Antenna and To Antenna must be different."))
                if (
                    from_antenna.device_id.controller_id != controller
                    or to_antenna.device_id.controller_id != controller
                ):
                    raise UserError(
                        _("Both transition antennas must belong to the Lane Controller.")
                    )

                path_key = (from_antenna.id, to_antenna.id)
                if path_key in seen_paths:
                    raise UserError(_("Duplicate directed Antenna Transition in Lane %s.") % lane_code)
                seen_paths.add(path_key)
                transition_specs.append({
                    "lane_code": lane_code,
                    "from_antenna_id": from_antenna.id,
                    "to_antenna_id": to_antenna.id,
                    "event_type": event_type,
                    "duration_seconds": duration,
                })

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

        Transition = self.env["nsp.parking.antenna.transition"].sudo()
        area_lanes = Lane.search([("parking_area_id", "=", parking.id)])
        existing_transitions = Transition.search([
            ("lane_id", "in", area_lanes.ids)
        ]) if area_lanes else Transition.browse()
        transition_by_key = {
            (rule.lane_id.code, rule.from_antenna_id.id, rule.to_antenna_id.id): rule
            for rule in existing_transitions
        }
        desired_keys = set()
        create_vals = []
        for spec in transition_specs:
            key = (
                spec["lane_code"], spec["from_antenna_id"], spec["to_antenna_id"]
            )
            desired_keys.add(key)
            vals = {
                "lane_id": lane_by_code[spec["lane_code"]].id,
                "from_antenna_id": spec["from_antenna_id"],
                "to_antenna_id": spec["to_antenna_id"],
                "event_type": spec["event_type"],
                "duration_seconds": spec["duration_seconds"],
            }
            rule = transition_by_key.get(key)
            if rule:
                self._write_changed(rule, vals)
            else:
                create_vals.append(vals)
        if create_vals:
            Transition.create(create_vals)

        stale_transitions = existing_transitions.filtered(
            lambda rule: (
                rule.lane_id.code,
                rule.from_antenna_id.id,
                rule.to_antenna_id.id,
            ) not in desired_keys
        )
        if stale_transitions:
            stale_transitions.unlink()

        incoming_codes = set(lane_specs)
        stale_lanes = area_lanes.filtered(lambda lane: lane.code not in incoming_codes and lane.active)
        if stale_lanes:
            stale_lanes.mapped("antenna_transition_ids").unlink()
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
            stale.mapped("lane_ids.antenna_transition_ids").unlink()
            stale.mapped("lane_ids").write({"active": False})
            stale.write({"state": "blocked"})
        return len(stale)

    def _apply_gatekeeper_config_snapshot(self, data, request_payload=False):
        """Atomically apply the Cloud-authoritative Gatekeeper snapshot.

        Absence from the snapshot is the delete signal. Historical Controller/Reader
        rows are archived (active=False/cloud_removed=True); transient topology rows
        are removed/blocked in dependency-safe order.
        """
        self.ensure_one()
        if not isinstance(data, dict): raise UserError(_("Gatekeeper Configuration response must be an object."))
        revision=int(data.get("revision") or 0)
        if revision <= 0: raise UserError(_("Gatekeeper Configuration revision is required."))
        if revision < int(self.snapshot_revision or 0):
            return {"applied":0,"removed":0,"revision":revision,"stale":True}
        controllers=data.get("controllers") or []; areas=data.get("parking_areas") or []; branches=data.get("branches") or []; whitelist=data.get("device_whitelist") or []
        for name,val in (("controllers",controllers),("parking_areas",areas),("branches",branches),("device_whitelist",whitelist)):
            if not isinstance(val,list): raise UserError(_("Gatekeeper Configuration %s must be an array.") % name)
        with self.env.cr.savepoint():
            # Branch cache first.
            Branch=self.env["nsp.branch"].sudo().with_context(active_test=False); incoming_branch=set()
            existing={r.code:r for r in Branch.search([])}
            for item in branches:
                code=str(item.get("branch_code") or "").strip().upper();
                if not code: raise UserError(_("Branch Code is required."))
                incoming_branch.add(code); vals={"name":item.get("branch_name") or code,"code":code,"timezone":item.get("timezone") or "Asia/Ho_Chi_Minh","status":"active" if item.get("active",True) else "inactive"}
                rec=existing.get(code); self._write_changed(rec,vals) if rec else Branch.create(vals)
            stale_branch=Branch.search([("code","not in",list(incoming_branch))]) if incoming_branch else Branch.search([])
            if stale_branch: stale_branch.write({"status":"inactive"})
            # Device whitelist is a small pure cache: hard-delete stale.
            cache=self._prepare_apply_cache("device_whitelist",whitelist)
            for item in whitelist: self._apply_device_whitelist(item,cache=cache)
            self._reconcile_device_whitelist_snapshot(whitelist)
            # Controllers/readers/antennas independent of Parking Area assignment.
            Controller=self.env["nsp.controller"].sudo().with_context(active_test=False); Device=self.env["nsp.device"].sudo().with_context(active_test=False); Antenna=self.env["nsp.device.antenna"].sudo().with_context(active_test=False)
            incoming_ctrl=set(); incoming_serial=set(); incoming_ant=set()
            ctrl_by={r.controller_id:r for r in Controller.search([])}; dev_by={r.serial_number:r for r in Device.search([])}
            for c in controllers:
                code=str(c.get("controller_code") or "").strip().upper();
                if not code: raise UserError(_("Controller Code is required."))
                incoming_ctrl.add(code); ctrl=ctrl_by.get(code)
                vals={"controller_name":c.get("controller_name") or code,"active":bool(c.get("active",True)),"cloud_removed":False}
                if ctrl: self._write_changed(ctrl,vals)
                else: vals["controller_id"]=code; ctrl=Controller.create(vals); ctrl_by[code]=ctrl
                for d in c.get("devices") or []:
                    serial=str(d.get("serial_number") or "").strip().upper();
                    if not serial: raise UserError(_("Reader Serial Number is required."))
                    incoming_serial.add(serial); rp=d.get("reader_parameters") or {}; conn=d.get("physical_connection") or False
                    vals={"name":d.get("reader_name") or serial,"controller_id":ctrl.id,"connection_type":conn,"power_dbm":int(rp.get("power_dbm") if rp.get("power_dbm") is not None else 30),"read_interval_ms":int(rp.get("read_interval_ms") or 200),"tid_addr":int(rp.get("tid_start_address") or 0),"tid_len":int(rp.get("tid_length") or 4),"active":True,"cloud_removed":False}
                    dev=dev_by.get(serial)
                    if dev: self._write_changed(dev,vals)
                    else: vals["serial_number"]=serial; dev=Device.create(vals); dev_by[serial]=dev
                    existing_ant = {
                        int(a.antenna_no): a
                        for a in Antenna.search([("device_id", "=", dev.id)])
                    }
                    for a in d.get("antennas") or []:
                        if not isinstance(a, dict) or set(a) - {"antenna_no"}:
                            raise UserError(_("Reader antenna supports only antenna_no."))
                        no=int(a.get("antenna_no") or 0)
                        if no <= 0:
                            raise UserError(_("Antenna No must be greater than zero."))
                        incoming_ant.add((serial,no)); av={"active":True,"cloud_removed":False}
                        ant=existing_ant.get(no)
                        if ant: self._write_changed(ant,av)
                        else: av.update({"device_id":dev.id,"antenna_no":no}); Antenna.create(av)
            stale_ant=Antenna.search([]).filtered(lambda a:(a.device_id.serial_number,int(a.antenna_no or 0)) not in incoming_ant)
            if stale_ant: stale_ant.write({"active":False,"cloud_removed":True})
            stale_dev=Device.search([]).filtered(lambda d:d.serial_number not in incoming_serial)
            if stale_dev: stale_dev.write({"active":False,"cloud_removed":True,"status":"offline"})
            stale_ctrl=Controller.search([]).filtered(lambda c:c.controller_id not in incoming_ctrl)
            if stale_ctrl: stale_ctrl.write({"active":False,"cloud_removed":True,"status":"revoked"})
            # Apply parking topology after physical cache exists.
            for area in areas: self._apply_parking_config(area)
            removed_area=self._reconcile_parking_config_snapshot(areas)
            self.write({"snapshot_revision":revision,"last_pull_at":fields.Datetime.now(),"sync_cursor":False})
        return {"applied":len(controllers)+len(areas)+len(whitelist),"removed":len(stale_ctrl)+len(stale_dev)+len(stale_ant)+removed_area,"revision":revision,"stale":False}

    def _apply_items(self, kind, items, request_payload=False):
        self.ensure_one()
        results, failed = [], []
        Record = self.env["nsp.sync.record"].sudo()
        handlers = {
            "user": self._apply_user,
            "vehicle": self._apply_vehicle,
            "vehicle_borrow": self._apply_vehicle_borrow,
            "measurement_config": self._apply_measurement_config,
        }
        handler = handlers.get(kind)
        if not handler:
            raise UserError(_("Unsupported pull route: %s") % self.route_suffix)
        normalized_items = items if isinstance(items, list) else []
        cached_kinds = {"user", "vehicle", "vehicle_borrow"}
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

    def _reconcile_measurement_snapshot(self, items):
        """Stop/remove Edge Measurement runtime records absent from Cloud snapshot.

        A deleted Cloud Measurement must stop physical execution on Edge. Sessions
        that still own observations are kept only as short-lived history and are
        removed by the normal retention cleanup after their observations expire.
        """
        self.ensure_one()
        rows = [item for item in (items or []) if isinstance(item, dict)]
        incoming = {
            str(item.get("measurement_code") or "").strip().upper()
            for item in rows
            if item.get("measurement_code")
        }
        Session = self.env["nsp.measurement.session"].sudo()
        stale = Session.search([("measurement_code", "not in", list(incoming))]) if incoming else Session.search([])
        if not stale:
            return 0
        now = fields.Datetime.now()
        running = stale.filtered(lambda rec: rec.status in ("ready", "running"))
        if running:
            running.with_context(measurement_sync=True).write({"status": "cancelled", "ended_at": now})
        disposable = stale.filtered(
            lambda rec: rec.status in ("completed", "applied", "failed", "cancelled") and not rec.event_ids
        )
        if disposable:
            disposable.with_context(measurement_sync=True).unlink()
        return len(stale)

    def _reconcile_business_snapshot(self, kind, items):
        """Archive/remove records absent from full Cloud master snapshots."""
        rows=[i for i in (items or []) if isinstance(i,dict)]
        if kind=="user":
            keys={str(i.get("user_code") or "").strip().upper() for i in rows}; Model=self.env["nsp.user"].sudo().with_context(active_test=False); stale=Model.search([("user_code","not in",list(keys)),("active","=",True)]) if keys else Model.search([("active","=",True)]); stale.write({"active":False}) if stale else None; return len(stale)
        if kind=="vehicle":
            keys={str(i.get("vehicle_code") or "").strip().upper() for i in rows}; Model=self.env["nsp.vehicle"].sudo().with_context(active_test=False); stale=Model.search([("vehicle_code","not in",list(keys)),("active","=",True)]) if keys else Model.search([("active","=",True)]); stale.write({"active":False}) if stale else None; return len(stale)
        if kind=="vehicle_borrow":
            keys={str(i.get("borrow_uid") or "").strip() for i in rows}; Model=self.env["nsp.vehicle.borrow"].sudo(); stale=Model.search([("borrow_code","not in",list(keys))]) if keys else Model.search([]); stale.unlink() if stale else None; return len(stale)
        return 0

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
        if self._action_kind() in ("gatekeeper_config", "vehicle_config", "card", "user", "vehicle", "vehicle_borrow", "measurement_config"):
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
        read_at = False
        if event.read_at:
            parsed = fields.Datetime.to_datetime(event.read_at)
            if parsed:
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                else:
                    parsed = parsed.astimezone(timezone.utc)
                millisecond = max(0, min(int(event.read_at_ms or 0), 999))
                parsed = parsed.replace(microsecond=millisecond * 1000)
                read_at = parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        payload = {
            "event_uid": event.event_uid,
            "revision": int(event.revision or 1),
            "serial_number": event.serial_number,
            "antenna_no": int(event.antenna_no),
            "tid": event.tid,
            "read_at": read_at,
            "power_dbm": int(
                event.power_dbm
                if event.power_dbm is not None
                else event.session_id._measurement_power_for_serial(event.serial_number)
            ),
        }
        if event.rssi_dbm not in (False, None):
            payload["rssi_dbm"] = float(event.rssi_dbm)
        return payload

    def _pending_measurement_events(self, limit):
        self.ensure_one(); Record=self.env["nsp.sync.record"].sudo(); Event=self.env["nsp.measurement.event"].sudo(); action_code=str(self.sync_action_code or "").strip(); source_code=str(self.edge_server_code or "NSP").strip() or "NSP"
        synced=Record.search([("source_code","=",source_code),("sync_action_code","=",action_code),("operation","=","push"),("status","=","synced")]).mapped("record_key")
        domain=[("event_uid","not in",synced)] if synced else []
        return Event.search(domain,order="read_at,id",limit=max(1,int(limit or 1)))

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
            reasons = {}
            for result in result_by_key.values():
                if result.get("status") not in ("rejected", "failed", "error"):
                    continue
                code = str(result.get("error_code") or result.get("message") or "rejected").strip()
                reasons[code] = reasons.get(code, 0) + 1
            reason_text = ", ".join(
                "%s: %s" % (code, count)
                for code, count in sorted(reasons.items())
            )
            message = _("Cloud rejected %s Measurement Event(s).") % failed
            if reason_text:
                message += " " + _("Reasons: %s") % reason_text
            raise UserError(message)
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
    def _ensure_edge_sync_jobs(self):
        """Repair missing default Sync Jobs for existing Edge connections.

        New route types may be introduced after an Edge connection already
        exists.  Do not require an operator to re-authenticate merely to create
        those jobs: the scheduler and immediate Measurement forwarding can
        self-heal the job set.
        """
        if self._deployment_role() != "edge_server":
            return self.browse()
        Auth = self.env["nsp.sync.auth"].sudo()
        auth_records = Auth.search([])
        if not auth_records:
            return self.browse()
        try:
            return self.sudo().ensure_default_jobs(auth_records)
        except Exception:
            _logger.exception("Unable to repair missing NSP Sync Jobs automatically.")
            return self.browse()

    @api.model
    def _measurement_push_job(self, route_suffix):
        domain = [
            ("active", "=", True),
            ("route_suffix", "=", route_suffix),
            ("direction", "=", "push"),
        ]
        job = self.sudo().search(domain, order="sequence, id", limit=1)
        if not job:
            self._ensure_edge_sync_jobs()
            job = self.sudo().search(domain, order="sequence, id", limit=1)
        return job

    @api.model
    def push_measurement_events_now(self, events):
        job = self._measurement_push_job("measurement-events/sync")
        if not job:
            _logger.warning(
                "Measurement Event forwarding deferred: no measurement-events/sync job is available."
            )
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
        self.ensure_one(); Session=self.env["nsp.measurement.session"].sudo(); Record=self.env["nsp.sync.record"].sudo(); result=Session.search([("status","!=","draft")],order="write_date,id")
        pending=[]
        for session in result:
            synced=Record.search([("sync_action_code","=",self.sync_action_code),("operation","=","push"),("record_key","=",session.measurement_code),("status","=","synced"),("last_synced_at",">=",session.write_date)],limit=1)
            if not synced: pending.append(session.id)
            if len(pending)>=max(1,int(limit or 1)): break
        return Session.browse(pending)

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
        job = self._measurement_push_job("measurement-status/sync")
        if not job:
            _logger.warning(
                "Measurement status forwarding deferred: no measurement-status/sync job is available."
            )
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

        if kind == "gatekeeper_config":
            result = self._apply_gatekeeper_config_snapshot(data, request_payload=request_payload)
            return {"pulled": result["applied"], "failed": 0, "has_more": False, "message": "Gatekeeper snapshot revision %s applied; %s stale record(s) removed/archived." % (result["revision"], result["removed"])}

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
        full_snapshot = kind in ("user", "vehicle", "vehicle_borrow", "measurement_config")
        if not items:
            removed = 0
            if kind in ("user", "vehicle", "vehicle_borrow"):
                removed = self._reconcile_business_snapshot(kind, [])
            elif kind == "measurement_config":
                removed = self._reconcile_measurement_snapshot([])
            self.write({
                "last_pull_at": fields.Datetime.now(),
                "sync_cursor": False if full_snapshot else (next_cursor or self.sync_cursor),
            })
            if full_snapshot:
                message = "%s snapshot is empty; removed/archived %s stale record(s)." % (self.sync_action_name or kind, removed)
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
        if kind in ("user", "vehicle", "vehicle_borrow"):
            removed = self._reconcile_business_snapshot(kind, items)
        elif kind == "measurement_config":
            removed = self._reconcile_measurement_snapshot(items)
        else:
            removed = 0
        self.write({
            "last_pull_at": fields.Datetime.now(),
            "sync_cursor": False if full_snapshot else (next_cursor or self.sync_cursor),
        })
        if full_snapshot:
            message = "Pulled %s record(s); removed/archived %s stale record(s)." % (len(results), removed)
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
        # Existing Cloud Connections may predate newer Sync routes (notably
        # Measurement Events/Status).  Repair the default job set before every
        # scheduler pass so pending Edge records always gain a durable retry path.
        self._ensure_edge_sync_jobs()
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
