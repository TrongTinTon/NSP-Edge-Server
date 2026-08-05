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
    "edge/status": "push",
    "edge/parking-runtime/snapshot": "pull",
    "edge/users/snapshot": "pull",
    "edge/vehicle-reference/snapshot": "pull",
    "edge/vehicles/snapshot": "pull",
    "edge/rfid-assignments/snapshot": "pull",
    "edge/vehicle-borrows/snapshot": "pull",
    "edge/lane-calibrations/snapshot": "pull",
    "edge/lane-calibrations/events": "push",
    "edge/lane-calibrations/status": "push",
    "edge/parking-transactions": "push",
}
NSP_SYNC_ALLOWED_ROUTES = tuple(SYNC_ROUTE_DIRECTIONS)
JOB_SEQUENCE = {route: sequence * 10 for sequence, route in enumerate(NSP_SYNC_ALLOWED_ROUTES, start=1)}
DEFAULT_JOB_SETTINGS = {
    "edge/status": {"schedule_interval_minutes": 1, "batch_size": 1},
    "edge/parking-runtime/snapshot": {"schedule_interval_minutes": 1, "batch_size": 1},
    "edge/users/snapshot": {"schedule_interval_minutes": 5, "batch_size": 500},
    "edge/vehicle-reference/snapshot": {"schedule_interval_minutes": 5, "batch_size": 1000},
    "edge/vehicles/snapshot": {"schedule_interval_minutes": 5, "batch_size": 500},
    "edge/rfid-assignments/snapshot": {"schedule_interval_minutes": 5, "batch_size": 1000},
    "edge/vehicle-borrows/snapshot": {"schedule_interval_minutes": 5, "batch_size": 500},
    "edge/lane-calibrations/snapshot": {"schedule_interval_minutes": 1, "batch_size": 100},
    "edge/lane-calibrations/events": {"schedule_interval_minutes": 1, "batch_size": 100},
    "edge/lane-calibrations/status": {"schedule_interval_minutes": 1, "batch_size": 100},
    "edge/parking-transactions": {"schedule_interval_minutes": 1, "batch_size": 200},
}
ACTION_KINDS = {
    "edge/status": "edge_server_status",
    "edge/parking-runtime/snapshot": "parking_runtime",
    "edge/users/snapshot": "user",
    "edge/vehicle-reference/snapshot": "vehicle_config",
    "edge/vehicles/snapshot": "vehicle",
    "edge/rfid-assignments/snapshot": "rfid_runtime_assignment",
    "edge/vehicle-borrows/snapshot": "vehicle_borrow",
    "edge/lane-calibrations/snapshot": "lane_calibration",
    "edge/lane-calibrations/events": "lane_calibration_event",
    "edge/lane-calibrations/status": "lane_calibration_status",
    "edge/parking-transactions": "parking_transaction",
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
    schedule_interval_minutes = fields.Integer(default=1, required=True, string="Schedule Interval (Minutes)", help="Fallback retry interval. Lane Calibration Events and Status are forwarded immediately; this schedule is used only when immediate forwarding fails.")
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

    def _business_adapter_required(self):
        raise UserError(_(
            "NSP Business Gatekeeper is required to serialize or apply business runtime data."
        ))

    def _serialize_edge_server_status(self):
        return self._business_adapter_required()

    def _serialize_push_batch(self, kind):
        return self._business_adapter_required()

    def _record_key_from_item(self, item):
        return self._business_adapter_required()

    def _run_lane_calibration_event_push_once(self):
        return self._business_adapter_required()

    def _run_lane_calibration_status_push_once(self):
        return self._business_adapter_required()

    def _apply_parking_runtime_snapshot(self, data, request_payload=False):
        return self._business_adapter_required()

    def _apply_vehicle_config_snapshot(self, data, request_payload=False):
        return self._business_adapter_required()

    def _apply_rfid_runtime_assignment_snapshot(self, data, request_payload=False):
        return self._business_adapter_required()

    def _apply_items(self, kind, items, request_payload=False):
        return self._business_adapter_required()

    def _reconcile_business_snapshot(self, kind, items):
        return self._business_adapter_required()

    def _reconcile_measurement_snapshot(self, items):
        return self._business_adapter_required()

    @api.model
    def _remote_push_item(self, item):
        payload = dict(item or {})
        payload.pop("record_key", None)
        return payload

    def _build_push_payload(self, items):
        self.ensure_one()
        route = (self.route_suffix or "").strip().strip("/")
        base = {"edge_server_code": self.edge_server_code}
        if route == "edge/status":
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



















    @api.model
    def _items_from_response(self, data):
        items = data.get("items") if isinstance(data, dict) else []
        if isinstance(items, dict):
            return [items]
        return items if isinstance(items, list) else []

    def _build_pull_payload(self):
        self.ensure_one()
        if self._action_kind() in ("parking_runtime", "vehicle_config", "rfid_runtime_assignment", "user", "vehicle", "vehicle_borrow", "lane_calibration"):
            return {"edge_server_code": self.edge_server_code}
        payload = {"edge_server_code": self.edge_server_code, "limit": self.batch_size}
        if self.sync_cursor:
            payload["sync_cursor"] = self.sync_cursor
        return payload


    # ------------------------ lane calibration push -------------------












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
        if kind == "lane_calibration_event":
            return self._run_lane_calibration_event_push_once()
        if kind == "lane_calibration_status":
            return self._run_lane_calibration_status_push_once()
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

        if kind == "parking_runtime":
            result = self._apply_parking_runtime_snapshot(data, request_payload=request_payload)
            return {"pulled": result["applied"], "failed": 0, "has_more": False, "message": "Parking Runtime snapshot revision %s applied; %s stale record(s) removed/archived." % (result["revision"], result["removed"])}

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

        if kind == "rfid_runtime_assignment":
            counts, stale_assignment_count = self._apply_rfid_runtime_assignment_snapshot(data, request_payload=request_payload)
            self.write({"last_pull_at": fields.Datetime.now(), "sync_cursor": False})
            return {
                "pulled": counts["active_assignments"],
                "failed": 0,
                "has_more": False,
                "message": (
                    "RFID runtime assignments applied: %(total)s active assignment(s), "
                    "%(users)s user assignment(s), %(vehicles)s vehicle assignment(s); "
                    "revoked %(stale)s stale local assignment(s)."
                ) % {
                    "total": counts["active_assignments"],
                    "users": counts["user_assignments"],
                    "vehicles": counts["vehicle_assignments"],
                    "stale": stale_assignment_count,
                },
            }

        items = self._items_from_response(data)
        next_cursor = data.get("next_sync_cursor") or False
        has_more = bool(data.get("has_more"))
        full_snapshot = kind in ("user", "vehicle", "vehicle_borrow", "lane_calibration")
        if not items:
            removed = 0
            if kind in ("user", "vehicle", "vehicle_borrow"):
                removed = self._reconcile_business_snapshot(kind, [])
            elif kind == "lane_calibration":
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
        elif kind == "lane_calibration":
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
        # Lane Calibration Events/Status). Repair the default job set before every
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
