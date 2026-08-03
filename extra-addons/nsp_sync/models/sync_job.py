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
    "users/sync": "pull",
    "vehicle-config/sync": "pull",
    "vehicles/sync": "pull",
    "rfid-tags/sync": "pull",
    "vehicle-borrow/sync": "pull",
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
    "users/sync": {"schedule_interval_minutes": 5, "batch_size": 500},
    "vehicle-config/sync": {"schedule_interval_minutes": 5, "batch_size": 1000},
    "vehicles/sync": {"schedule_interval_minutes": 5, "batch_size": 500},
    "rfid-tags/sync": {"schedule_interval_minutes": 5, "batch_size": 1000},
    "vehicle-borrow/sync": {"schedule_interval_minutes": 5, "batch_size": 500},
    "edge/lane-calibrations/snapshot": {"schedule_interval_minutes": 1, "batch_size": 100},
    "edge/lane-calibrations/events": {"schedule_interval_minutes": 1, "batch_size": 100},
    "edge/lane-calibrations/status": {"schedule_interval_minutes": 1, "batch_size": 100},
    "edge/parking-transactions": {"schedule_interval_minutes": 1, "batch_size": 200},
}
ACTION_KINDS = {
    "edge/status": "edge_server_status",
    "edge/parking-runtime/snapshot": "parking_runtime",
    "users/sync": "user",
    "vehicle-config/sync": "vehicle_config",
    "vehicles/sync": "vehicle",
    "rfid-tags/sync": "rfid_tag",
    "vehicle-borrow/sync": "vehicle_borrow",
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
        controllers = []
        Controller = self.env["nsp.controller"].sudo()
        for controller in Controller.search([("active", "=", True)], order="controller_id,id"):
            devices = []
            for device in controller.device_ids.filtered("active").sorted(
                key=lambda record: (record.serial_number or "", record.id)
            ):
                status = str(device.status or "offline").lower()
                item = {
                    "serial_number": device.serial_number or "",
                    "antennas": sorted(
                        int(number)
                        for number in device.antennas_ids.filtered("active").mapped("antenna_no")
                    ),
                    "device_status": (
                        status if status in ("online", "offline", "degraded") else "offline"
                    ),
                    "last_seen_at": self._dt(device.last_seen) if device.last_seen else False,
                }
                if device.firmware_version:
                    item["firmware_version"] = device.firmware_version

                # Runtime settings are unknown until the Controller reports them.
                # Omit both fields rather than sending a synthetic zero interval
                # that Cloud must reject as invalid.
                runtime_interval = int(device.runtime_read_interval_ms or 0)
                if runtime_interval > 0:
                    item.update({
                        "power_dbm": int(device.runtime_power_dbm or 0),
                        "read_interval_ms": runtime_interval,
                    })
                devices.append(item)

            controllers.append({
                "controller_code": controller.controller_id or "",
                "current_status": str(controller.status or "offline").lower(),
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
            technical_codes = {str(item.get("technical_code") or "").strip().upper() for item in rows}
            type_codes = {str(item.get("device_type_code") or "").strip().upper() for item in rows}
            technical_codes.discard("")
            type_codes.discard("")
            records = self.env["nsp.device.whitelist"].sudo().with_context(active_test=False).search([
                ("technical_code", "in", list(technical_codes)),
            ]) if technical_codes else self.env["nsp.device.whitelist"].browse()
            device_types = self.env["nsp.device.type"].sudo().with_context(active_test=False).search([
                ("code", "in", list(type_codes)),
            ]) if type_codes else self.env["nsp.device.type"].browse()
            return {
                "records": {record.technical_code: record for record in records},
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
        """Apply one identity referenced by the released assembly snapshot."""
        if not isinstance(item, dict):
            raise UserError(_("Device Whitelist item must be an object."))
        unsupported = set(item) - {
            "technical_code", "name", "device_type_code", "device_type_name",
            "serial_number", "active",
        }
        if unsupported:
            raise UserError(
                _("Unsupported Device Whitelist field(s): %s")
                % ", ".join(sorted(unsupported))
            )
        technical_code = str(item.get("technical_code") or "").strip().upper()
        if not technical_code:
            raise UserError(_("Device Whitelist Management Code is required."))

        type_code = str(item.get("device_type_code") or "").strip().upper()
        type_name = str(item.get("device_type_name") or type_code).strip()
        if type_code not in {"SERVER", "CONTROLLER", "RFID_READER", "ANTENNA"}:
            raise UserError(_("Unsupported Device Type Code: %s") % type_code)

        cache = cache or self._prepare_apply_cache("device_whitelist", [item])
        device_type = cache.get("device_types", {}).get(type_code)
        if not device_type:
            raise UserError(_("Device Type %(type)s is not installed on Edge.") % {"type": type_code})
        if type_name and device_type.name != type_name:
            device_type.write({"name": type_name})

        serial = str(item.get("serial_number") or "").strip().upper() or False
        if type_code == "RFID_READER" and not serial:
            raise UserError(_("Serial Number is required for RFID Reader %(code)s.") % {"code": technical_code})
        vals = {
            "technical_code": technical_code,
            "name": str(item.get("name") or serial or technical_code).strip(),
            "device_type_id": device_type.id,
            "serial_number": serial,
            "active": bool(item.get("active", True)),
        }
        Whitelist = self.env["nsp.device.whitelist"].sudo().with_context(active_test=False)
        record = cache.get("records", {}).get(technical_code)
        if record:
            self._write_changed(record, vals)
            return record
        record = Whitelist.create(vals)
        cache.setdefault("records", {})[technical_code] = record
        return record

    @api.model
    def _reconcile_device_whitelist_snapshot(self, items):
        technical_codes = {
            str(item.get("technical_code") or "").strip().upper()
            for item in (items or [])
            if isinstance(item, dict) and str(item.get("technical_code") or "").strip()
        }
        Whitelist = self.env["nsp.device.whitelist"].sudo().with_context(active_test=False)
        stale = Whitelist.search([
            ("technical_code", "not in", list(technical_codes))
        ]) if technical_codes else Whitelist.search([])
        if stale:
            stale.write({"active": False})
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
    def _normalize_rfid_tag_snapshot_item(self, item):
        if not isinstance(item, dict):
            raise UserError(_("RFID Tag snapshot items must be objects."))
        unsupported = sorted(set(item) - {"tid", "assignment"})
        if unsupported:
            raise UserError(_("Unsupported RFID Tag field(s): %s") % ", ".join(unsupported))
        tid = self.env["nsp.rfid.tag"]._normalize_tid(item.get("tid"))
        if not tid:
            raise UserError(_("RFID Tag TID is required."))

        assignment = item.get("assignment") or False
        target = "unassigned"
        code = ""
        assigned_at = False
        if assignment:
            if not isinstance(assignment, dict):
                raise UserError(_("RFID Tag assignment must be an object."))
            unsupported_assignment = sorted(set(assignment) - {"target", "code", "assigned_at"})
            if unsupported_assignment:
                raise UserError(
                    _("Unsupported RFID Tag assignment field(s): %s")
                    % ", ".join(unsupported_assignment)
                )
            target = str(assignment.get("target") or "").strip().lower()
            code = str(assignment.get("code") or "").strip().upper()
            assigned_at = (
                self._remote_datetime(assignment.get("assigned_at"))
                if assignment.get("assigned_at") else False
            )
            if target not in ("user", "vehicle"):
                raise UserError(_("RFID Tag assignment target must be user or vehicle."))
            if not code:
                raise UserError(_("Assigned RFID Tag requires assignment.code."))
        return {
            "tid": tid,
            "target": target,
            "code": code,
            "assigned_at": assigned_at,
            "source": item,
        }

    def _apply_rfid_tag_snapshot(self, data, request_payload=False):
        """Apply the complete Cloud RFID whitelist and active assignment snapshot."""
        self.ensure_one()
        items = self._items_from_response(data)
        if not isinstance(items, list):
            raise UserError(_("RFID Tag snapshot must contain an items array."))

        normalized = []
        seen = set()
        for item in items:
            info = self._normalize_rfid_tag_snapshot_item(item)
            if info["tid"] in seen:
                raise UserError(_("Duplicate RFID TID in snapshot: %s") % info["tid"])
            seen.add(info["tid"])
            normalized.append(info)

        Tag = self.env["nsp.rfid.tag"].sudo()
        Assignment = self.env["nsp.rfid.tag.assignment"].sudo()
        User = self.env["nsp.user"].sudo().with_context(active_test=False)
        Vehicle = self.env["nsp.vehicle"].sudo().with_context(active_test=False)

        user_codes = {info["code"] for info in normalized if info["target"] == "user"}
        vehicle_codes = {info["code"] for info in normalized if info["target"] == "vehicle"}
        users = User.search([("user_code", "in", list(user_codes))]) if user_codes else User.browse()
        vehicles = Vehicle.search([("vehicle_code", "in", list(vehicle_codes))]) if vehicle_codes else Vehicle.browse()
        user_by_code = {user.user_code: user for user in users}
        vehicle_by_code = {vehicle.vehicle_code: vehicle for vehicle in vehicles}
        for info in normalized:
            if info["target"] == "user" and info["code"] not in user_by_code:
                raise UserError(
                    _("RFID Tag %(tid)s User %(code)s was not found. Run users/sync first.")
                    % info
                )
            if info["target"] == "vehicle" and info["code"] not in vehicle_by_code:
                raise UserError(
                    _("RFID Tag %(tid)s Vehicle %(code)s was not found. Run vehicles/sync first.")
                    % info
                )

        tids = [info["tid"] for info in normalized]
        existing_tags = Tag.search([("tid", "in", tids)]) if tids else Tag.browse()
        tag_by_tid = {tag.tid: tag for tag in existing_tags}
        missing = [info for info in normalized if info["tid"] not in tag_by_tid]
        if missing:
            created = Tag.create([{"tid": info["tid"]} for info in missing])
            tag_by_tid.update({tag.tid: tag for tag in created})

        all_snapshot_tags = Tag.browse([tag_by_tid[info["tid"]].id for info in normalized])
        active_assignments = Assignment.search([
            ("tag_id", "in", all_snapshot_tags.ids), ("state", "=", "active"),
        ]) if all_snapshot_tags else Assignment.browse()
        active_by_tag = {assignment.tag_id.id: assignment for assignment in active_assignments}

        counts = {
            "whitelisted_tags": len(normalized),
            "employee_assignments": 0,
            "vehicle_assignments": 0,
            "unassigned_tags": 0,
        }
        synced_records = {}
        for info in normalized:
            tag = tag_by_tid[info["tid"]]
            current = active_by_tag.get(tag.id, Assignment.browse())
            target = info["target"]
            desired_user = user_by_code.get(info["code"]) if target == "user" else User.browse()
            desired_vehicle = vehicle_by_code.get(info["code"]) if target == "vehicle" else Vehicle.browse()

            same = bool(
                current
                and current.user_id == desired_user
                and current.vehicle_id == desired_vehicle
                and target != "unassigned"
            )
            if current and not same:
                current.action_revoke()
                current = Assignment.browse()

            if target == "unassigned":
                counts["unassigned_tags"] += 1
                synced_records[tag.id] = tag
                continue

            if target == "user":
                counts["employee_assignments"] += 1
            else:
                counts["vehicle_assignments"] += 1

            if not current:
                # Reconcile stale target ownership before creating the desired assignment.
                stale_target = Assignment.search([
                    ("state", "=", "active"),
                    ("user_id" if target == "user" else "vehicle_id", "=",
                     desired_user.id if target == "user" else desired_vehicle.id),
                ], limit=1)
                if stale_target:
                    stale_target.action_revoke()
                vals = {
                    "tag_id": tag.id,
                    "user_id": desired_user.id if desired_user else False,
                    "vehicle_id": desired_vehicle.id if desired_vehicle else False,
                }
                if info["assigned_at"]:
                    vals["assigned_at"] = info["assigned_at"]
                current = Assignment.with_context(rfid_assignment_sync=True).create(vals)
            synced_records[tag.id] = current

        # Whitelist TIDs are audit identities and are never deleted. If a TID is
        # absent from the authoritative snapshot, revoke only its active local
        # assignment. Cloud normally keeps every historical TID, so this branch
        # is a defensive reconciliation path.
        stale_tags = Tag.search([("tid", "not in", tids)]) if tids else Tag.search([])
        stale_active = Assignment.search([
            ("tag_id", "in", stale_tags.ids),
            ("state", "=", "active"),
        ]) if stale_tags else Assignment.browse()
        stale_assignment_count = len(stale_active)
        if stale_active:
            stale_active.with_context(rfid_assignment_sync=True).action_revoke()

        Record = self.env["nsp.sync.record"].sudo()
        for info in normalized:
            tag = tag_by_tid[info["tid"]]
            Record.mark_result(
                sync_job=self,
                action_code=self.sync_action_code,
                action_name=self.sync_action_name,
                route_suffix=self.route_suffix,
                record=synced_records.get(tag.id, tag),
                record_key=tag.tid,
                status="synced",
                message="RFID Tag whitelist and active assignment synchronized.",
                payload=request_payload,
                response=info["source"],
                operation="pull",
            )
        return counts, stale_assignment_count

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

    def _apply_lane_calibration(self, item):
        """Apply one released Lane Calibration assembly from Cloud."""
        self.ensure_one()
        if not isinstance(item, dict):
            raise UserError(_("Lane Calibration item must be an object."))
        unsupported_outer = set(item) - {
            "measurement_code", "status", "desired_state", "revision",
            "vehicles", "readers",
        }
        if unsupported_outer:
            raise UserError(
                _("Unsupported Lane Calibration field(s): %s")
                % ", ".join(sorted(unsupported_outer))
            )

        code = str(item.get("measurement_code") or "").strip().upper()
        vehicle_payloads = item.get("vehicles")
        reader_payloads = item.get("readers")
        if not code:
            raise UserError(_("Calibration Code is required."))
        if not isinstance(vehicle_payloads, list) or not vehicle_payloads:
            raise UserError(_("Lane Calibration must contain at least one Vehicle."))
        if not isinstance(reader_payloads, list) or not reader_payloads:
            raise UserError(_("Lane Calibration must contain at least one RFID Reader assembly."))

        status = str(item.get("status") or "ready").strip().lower()
        if status not in ("ready", "running", "completed", "applied", "failed", "cancelled"):
            raise UserError(_("Invalid Lane Calibration status: %s") % status)
        try:
            revision = max(int(item.get("revision") or 1), 1)
        except (TypeError, ValueError) as exc:
            raise UserError(_("Invalid Lane Calibration revision.")) from exc

        # Vehicles and RFID assignments are authoritative master snapshots applied
        # by their dedicated sync jobs before this configuration is activated.
        Tag = self.env["nsp.rfid.tag"].sudo()
        normalized_vehicles = []
        all_tids = set()
        vehicle_codes = set()
        for payload in vehicle_payloads:
            if not isinstance(payload, dict):
                raise UserError(_("Lane Calibration Vehicle must be an object."))
            unsupported = set(payload) - {
                "vehicle_tid", "vehicle_code", "license_plate", "owner_user_code",
            }
            if unsupported:
                raise UserError(
                    _("Unsupported Lane Calibration Vehicle field(s): %s")
                    % ", ".join(sorted(unsupported))
                )
            tid = Tag._normalize_tid(payload.get("vehicle_tid"))
            vehicle_code = str(payload.get("vehicle_code") or "").strip().upper()
            plate = str(payload.get("license_plate") or "").strip().upper()
            owner_code = str(payload.get("owner_user_code") or "").strip().upper()
            if not tid or not vehicle_code or not plate:
                raise UserError(_(
                    "Each Lane Calibration Vehicle requires RFID Tag, Vehicle Code and License Plate."
                ))
            if tid in all_tids:
                raise UserError(_("Duplicate Vehicle RFID Tag: %s") % tid)
            if vehicle_code in vehicle_codes:
                raise UserError(_("Duplicate Vehicle: %s") % vehicle_code)
            all_tids.add(tid)
            vehicle_codes.add(vehicle_code)
            normalized_vehicles.append({
                "vehicle_tid": tid,
                "vehicle_code": vehicle_code,
                "license_plate": plate,
                "owner_user_code": owner_code,
            })

        assignments = self.env["nsp.rfid.tag.assignment"].sudo().search([
            ("tid", "in", list(all_tids)), ("state", "=", "active"),
        ])
        assignment_by_tid = {assignment.tid: assignment for assignment in assignments}
        missing_tids = sorted(all_tids - set(assignment_by_tid))
        if missing_tids:
            raise UserError(
                _("Vehicle RFID assignment(s) are not synchronized: %s")
                % ", ".join(missing_tids[:20])
            )

        Vehicle = self.env["nsp.vehicle"].sudo().with_context(active_test=False)
        vehicles = Vehicle.search([("vehicle_code", "in", list(vehicle_codes))])
        vehicle_by_code = {vehicle.vehicle_code: vehicle for vehicle in vehicles}
        missing_vehicle_codes = sorted(vehicle_codes - set(vehicle_by_code))
        if missing_vehicle_codes:
            raise UserError(
                _("Vehicle(s) are not synchronized: %s")
                % ", ".join(missing_vehicle_codes[:20])
            )

        target_commands = []
        Target = self.env["nsp.measurement.target.line"].sudo()
        for payload in normalized_vehicles:
            assignment = assignment_by_tid[payload["vehicle_tid"]]
            vehicle = vehicle_by_code[payload["vehicle_code"]]
            if assignment.vehicle_id != vehicle:
                raise UserError(_(
                    "RFID Tag %(tid)s is not assigned to Vehicle %(vehicle)s."
                ) % {"tid": payload["vehicle_tid"], "vehicle": payload["vehicle_code"]})
            actual_plate = str(vehicle.license_plate or "").strip().upper()
            if actual_plate != payload["license_plate"]:
                raise UserError(_(
                    "Vehicle %(vehicle)s License Plate does not match the released calibration."
                ) % {"vehicle": payload["vehicle_code"]})
            actual_owner = str(vehicle.owner_id.user_code or "").strip().upper() if vehicle.owner_id else ""
            if actual_owner != payload["owner_user_code"]:
                raise UserError(_(
                    "Vehicle %(vehicle)s Owner does not match the released calibration."
                ) % {"vehicle": payload["vehicle_code"]})
            values = Target._prepare_scanned_values({
                "tag_id": assignment.tag_id.id,
                "vehicle_id": vehicle.id,
            })
            target_commands.append((0, 0, values))

        # Parse and apply only identities referenced by this released calibration.
        normalized_readers = []
        identity_rows = {}
        seen_readers = set()
        used_antennas = set()
        for payload in reader_payloads:
            if not isinstance(payload, dict):
                raise UserError(_("Reader assembly must be an object."))
            unsupported = set(payload) - {
                "server_code", "controller_code", "controller_name",
                "technical_code", "serial_number", "reader_name",
                "physical_connection", "reader_parameters", "antennas",
            }
            if unsupported:
                raise UserError(
                    _("Unsupported Reader assembly field(s): %s")
                    % ", ".join(sorted(unsupported))
                )
            server_code = str(payload.get("server_code") or "").strip().upper()
            controller_code = str(payload.get("controller_code") or "").strip().upper()
            reader_code = str(payload.get("technical_code") or "").strip().upper()
            serial = str(payload.get("serial_number") or "").strip().upper()
            if not server_code or not controller_code or not reader_code or not serial:
                raise UserError(_(
                    "Every Reader assembly requires Server, Controller, Reader Management Code and Reader Serial Number."
                ))
            if reader_code in seen_readers or serial in {row["serial_number"] for row in normalized_readers}:
                raise UserError(_("RFID Reader is duplicated in Lane Calibration."))
            seen_readers.add(reader_code)
            parameters = payload.get("reader_parameters") or {}
            if not isinstance(parameters, dict) or set(parameters) - {
                "power_dbm", "read_interval_ms", "tid_start_address", "tid_length",
            }:
                raise UserError(_("Invalid Reader Parameters payload."))
            try:
                power = int(parameters.get("power_dbm") if parameters.get("power_dbm") is not None else 30)
                interval = int(parameters.get("read_interval_ms") or 200)
                tid_addr = int(parameters.get("tid_start_address") or 0)
                tid_len = int(parameters.get("tid_length") or 0)
            except (TypeError, ValueError) as exc:
                raise UserError(_("Invalid Reader Parameters.")) from exc
            if power < 0 or power > 40 or interval <= 0 or interval > 60000 or tid_addr < 0 or tid_len <= 0:
                raise UserError(_("Reader Parameters are outside the supported range."))

            antennas = payload.get("antennas") or []
            if not isinstance(antennas, list) or not antennas:
                raise UserError(_("RFID Reader %s has no Antenna Port Mapping.") % serial)
            antenna_rows = []
            used_ports = set()
            for antenna in antennas:
                if not isinstance(antenna, dict) or set(antenna) - {
                    "antenna_no", "technical_code", "serial_number", "name",
                }:
                    raise UserError(_("Invalid Antenna Port Mapping payload."))
                try:
                    port_no = int(antenna.get("antenna_no") or 0)
                except (TypeError, ValueError) as exc:
                    raise UserError(_("Antenna Port No. must be an integer.")) from exc
                antenna_code = str(antenna.get("technical_code") or "").strip().upper()
                if port_no <= 0 or port_no in used_ports or not antenna_code:
                    raise UserError(_("Antenna Port Mapping is missing or duplicated."))
                if antenna_code in used_antennas:
                    raise UserError(_("An Antenna can be assembled only once in one Lane Calibration."))
                used_ports.add(port_no)
                used_antennas.add(antenna_code)
                antenna_serial = str(antenna.get("serial_number") or "").strip().upper() or False
                antenna_name = str(antenna.get("name") or antenna_code).strip()
                antenna_rows.append({
                    "port_no": port_no,
                    "technical_code": antenna_code,
                    "serial_number": antenna_serial,
                    "name": antenna_name,
                })
                identity_rows[antenna_code] = {
                    "technical_code": antenna_code,
                    "name": antenna_name,
                    "device_type_code": "ANTENNA",
                    "device_type_name": "Antenna",
                    "serial_number": antenna_serial,
                    "active": True,
                }
            identity_rows[server_code] = {
                "technical_code": server_code, "name": server_code,
                "device_type_code": "SERVER", "device_type_name": "Server",
                "serial_number": False, "active": True,
            }
            identity_rows[controller_code] = {
                "technical_code": controller_code,
                "name": str(payload.get("controller_name") or controller_code).strip(),
                "device_type_code": "CONTROLLER", "device_type_name": "Controller",
                "serial_number": False, "active": True,
            }
            identity_rows[reader_code] = {
                "technical_code": reader_code,
                "name": str(payload.get("reader_name") or serial).strip(),
                "device_type_code": "RFID_READER", "device_type_name": "RFID Reader",
                "serial_number": serial, "active": True,
            }
            normalized_readers.append({
                "server_code": server_code,
                "controller_code": controller_code,
                "controller_name": str(payload.get("controller_name") or controller_code).strip(),
                "reader_code": reader_code,
                "reader_name": str(payload.get("reader_name") or serial).strip(),
                "serial_number": serial,
                "physical_connection": payload.get("physical_connection") or False,
                "power_dbm": power,
                "read_interval_ms": interval,
                "tid_start_address": tid_addr,
                "tid_length": tid_len,
                "antennas": antenna_rows,
            })

        identity_list = list(identity_rows.values())
        identity_cache = self._prepare_apply_cache("device_whitelist", identity_list)
        for row in identity_list:
            self._apply_device_whitelist(row, cache=identity_cache)
        whitelist_by_code = {
            record.technical_code: record
            for record in self.env["nsp.device.whitelist"].sudo().with_context(active_test=False).search([
                ("technical_code", "in", list(identity_rows)),
            ])
        }

        Edge = self.env["nsp.edge.server"].sudo().with_context(active_test=False)
        Controller = self.env["nsp.controller"].sudo().with_context(active_test=False)
        Device = self.env["nsp.device"].sudo().with_context(active_test=False)
        Antenna = self.env["nsp.device.antenna"].sudo().with_context(active_test=False)
        edge_by_code = {row.edge_server_code: row for row in Edge.search([])}
        controller_by_code = {row.controller_id: row for row in Controller.search([])}
        reader_by_code = {row.device_code: row for row in Device.search([])}
        antenna_by_code = {row.technical_code: row for row in Antenna.search([]) if row.technical_code}

        line_commands = []
        for row in normalized_readers:
            edge = edge_by_code.get(row["server_code"])
            edge_vals = {
                "name": whitelist_by_code[row["server_code"]].name or row["server_code"],
                "whitelist_id": whitelist_by_code[row["server_code"]].id,
                "active": True, "cloud_removed": False,
            }
            if edge:
                self._write_changed(edge, edge_vals)
            else:
                edge = Edge.create({"edge_server_code": row["server_code"], **edge_vals})
                edge_by_code[row["server_code"]] = edge

            controller = controller_by_code.get(row["controller_code"])
            controller_vals = {
                "controller_name": row["controller_name"],
                "edge_server_id": edge.id,
                "whitelist_id": whitelist_by_code[row["controller_code"]].id,
                "active": True, "cloud_removed": False,
            }
            if controller:
                self._write_changed(controller, controller_vals)
            else:
                controller = Controller.create({
                    "controller_id": row["controller_code"], **controller_vals,
                })
                controller_by_code[row["controller_code"]] = controller

            reader = reader_by_code.get(row["reader_code"])
            if not reader:
                reader = Device.search([("serial_number", "=", row["serial_number"])], limit=1)
            reader_vals = {
                "name": row["reader_name"],
                "serial_number": row["serial_number"],
                "device_code": row["reader_code"],
                "controller_id": controller.id,
                "connection_type": row["physical_connection"],
                "power_dbm": row["power_dbm"],
                "read_interval_ms": row["read_interval_ms"],
                "tid_addr": row["tid_start_address"],
                "tid_len": row["tid_length"],
                "whitelist_id": whitelist_by_code[row["reader_code"]].id,
                "active": True, "cloud_removed": False,
            }
            if reader:
                self._write_changed(reader, reader_vals)
            else:
                reader = Device.create(reader_vals)
            reader_by_code[row["reader_code"]] = reader

            port_commands = []
            for antenna_row in row["antennas"]:
                identity = whitelist_by_code[antenna_row["technical_code"]]
                antenna = Antenna.search([
                    ("device_id", "=", reader.id),
                    ("antenna_no", "=", antenna_row["port_no"]),
                ], limit=1)
                if antenna and antenna.whitelist_id != identity:
                    raise UserError(_(
                        "Reader %(reader)s port %(port)s is already mapped to another Antenna in the active runtime assembly."
                    ) % {
                        "reader": row["serial_number"],
                        "port": antenna_row["port_no"],
                    })
                antenna_vals = {
                    "serial_number": antenna_row["serial_number"],
                    "device_id": reader.id,
                    "antenna_no": antenna_row["port_no"],
                    "whitelist_id": identity.id,
                    "active": True,
                    "cloud_removed": False,
                }
                if antenna:
                    # Keep the existing technical code when this endpoint is also
                    # used by Parking. The whitelist relation is the identity source.
                    self._write_changed(antenna, antenna_vals)
                else:
                    # A calibration mapping is contextual. Do not move another
                    # runtime row that may belong to a published Parking assembly.
                    # PostgreSQL unique constraints permit multiple NULL technical
                    # codes while whitelist_id preserves the physical identity.
                    antenna = Antenna.create({"technical_code": False, **antenna_vals})
                port_commands.append((0, 0, {
                    "port_no": antenna_row["port_no"],
                    "antenna_id": antenna.id,
                }))

            line_commands.append((0, 0, {
                "edge_server_id": edge.id,
                "controller_id": controller.id,
                "reader_id": reader.id,
                "reader_power_dbm": row["power_dbm"],
                "read_interval_ms": row["read_interval_ms"],
                "antenna_port_ids": port_commands,
            }))

        Session = self.env["nsp.measurement.session"].sudo().with_context(measurement_sync=True)
        session = Session.search([("measurement_code", "=", code)], limit=1)
        vals = {
            "measurement_code": code,
            "revision": revision,
            "status": status,
            "target_line_ids": [(5, 0, 0)] + target_commands,
            "reader_line_ids": [(5, 0, 0)] + line_commands,
        }
        if session:
            session.write(vals)
        else:
            session = Session.create(vals)
        return session

    def _apply_parking_runtime_snapshot(self, data, request_payload=False):
        """Apply only the device projection referenced by published Parking Layouts."""
        self.ensure_one()
        if not isinstance(data, dict):
            raise UserError(_("Parking Runtime response must be an object."))
        revision = int(data.get("revision") or 0)
        if revision <= 0:
            raise UserError(_("Parking Runtime revision is required."))
        if revision < int(self.snapshot_revision or 0):
            return {"applied": 0, "removed": 0, "revision": revision, "stale": True}

        controllers = data.get("controllers") or []
        areas = data.get("parking_areas") or []
        branches = data.get("branches") or []
        whitelist = data.get("device_whitelist") or []
        for name, value in (
            ("controllers", controllers),
            ("parking_areas", areas),
            ("branches", branches),
            ("device_whitelist", whitelist),
        ):
            if not isinstance(value, list):
                raise UserError(_("Parking Runtime %s must be an array.") % name)

        with self.env.cr.savepoint():
            Branch = self.env["nsp.branch"].sudo().with_context(active_test=False)
            existing_branches = {record.code: record for record in Branch.search([])}
            incoming_branch_codes = set()
            for item in branches:
                if not isinstance(item, dict):
                    raise UserError(_("Branch payload must contain objects."))
                code = str(item.get("branch_code") or "").strip().upper()
                if not code:
                    raise UserError(_("Branch Code is required."))
                incoming_branch_codes.add(code)
                values = {
                    "name": item.get("branch_name") or code,
                    "code": code,
                    "timezone": item.get("timezone") or "Asia/Ho_Chi_Minh",
                    "status": "active" if item.get("active", True) else "inactive",
                }
                record = existing_branches.get(code)
                if record:
                    self._write_changed(record, values)
                else:
                    existing_branches[code] = Branch.create(values)
            stale_branches = Branch.search([
                ("code", "not in", list(incoming_branch_codes)),
            ]) if incoming_branch_codes else Branch.search([])
            if stale_branches:
                stale_branches.write({"status": "inactive"})

            # Upsert only identities referenced by this published projection.
            # Do not globally reconcile the identity cache here because active
            # Lane Calibration snapshots may reference additional identities.
            identity_cache = self._prepare_apply_cache("device_whitelist", whitelist)
            for item in whitelist:
                self._apply_device_whitelist(item, cache=identity_cache)
            Whitelist = self.env["nsp.device.whitelist"].sudo().with_context(active_test=False)
            whitelist_by_code = {
                record.technical_code: record
                for record in Whitelist.search([])
                if record.technical_code
            }

            Edge = self.env["nsp.edge.server"].sudo().with_context(active_test=False)
            edge_by_code = {record.edge_server_code: record for record in Edge.search([])}
            for identity in whitelist:
                if str(identity.get("device_type_code") or "").strip().upper() != "SERVER":
                    continue
                code = str(identity.get("technical_code") or "").strip().upper()
                whitelist_record = whitelist_by_code.get(code)
                values = {
                    "name": str(identity.get("name") or code).strip(),
                    "whitelist_id": whitelist_record.id if whitelist_record else False,
                    "active": bool(identity.get("active", True)),
                    "cloud_removed": False,
                }
                edge = edge_by_code.get(code)
                if edge:
                    self._write_changed(edge, values)
                else:
                    edge = Edge.create({"edge_server_code": code, **values})
                    edge_by_code[code] = edge

            Controller = self.env["nsp.controller"].sudo().with_context(active_test=False)
            Device = self.env["nsp.device"].sudo().with_context(active_test=False)
            Antenna = self.env["nsp.device.antenna"].sudo().with_context(active_test=False)
            controller_by_code = {record.controller_id: record for record in Controller.search([])}
            reader_by_serial = {record.serial_number: record for record in Device.search([])}
            antenna_by_code = {
                record.technical_code: record
                for record in Antenna.search([("technical_code", "!=", False)])
                if record.technical_code
            }
            incoming_parking_antenna_codes = set()

            for controller_item in controllers:
                if not isinstance(controller_item, dict):
                    raise UserError(_("Controller payload must contain objects."))
                unsupported = set(controller_item) - {
                    "controller_code", "controller_name", "server_code", "active", "devices",
                }
                if unsupported:
                    raise UserError(
                        _("Unsupported Controller field(s): %s")
                        % ", ".join(sorted(unsupported))
                    )
                controller_code = str(controller_item.get("controller_code") or "").strip().upper()
                server_code = str(controller_item.get("server_code") or "").strip().upper()
                if not controller_code or not server_code:
                    raise UserError(_("Controller Code and Server Code are required."))
                edge = edge_by_code.get(server_code)
                if not edge:
                    raise UserError(_("Published Server %s is missing from the identity projection.") % server_code)
                controller_whitelist = whitelist_by_code.get(controller_code)
                if not controller_whitelist:
                    raise UserError(_("Published Controller %s is missing from the identity projection.") % controller_code)
                controller_values = {
                    "controller_name": controller_item.get("controller_name") or controller_code,
                    "edge_server_id": edge.id,
                    "active": bool(controller_item.get("active", True)),
                    "cloud_removed": False,
                    "whitelist_id": controller_whitelist.id,
                }
                controller = controller_by_code.get(controller_code)
                if controller:
                    self._write_changed(controller, controller_values)
                else:
                    controller = Controller.create({
                        "controller_id": controller_code,
                        **controller_values,
                    })
                    controller_by_code[controller_code] = controller

                devices = controller_item.get("devices") or []
                if not isinstance(devices, list):
                    raise UserError(_("Controller devices must be an array."))
                for reader_item in devices:
                    if not isinstance(reader_item, dict):
                        raise UserError(_("RFID Reader payload must contain objects."))
                    unsupported_reader = set(reader_item) - {
                        "technical_code", "serial_number", "reader_name",
                        "physical_connection", "reader_parameters", "antennas",
                    }
                    if unsupported_reader:
                        raise UserError(
                            _("Unsupported RFID Reader field(s): %s")
                            % ", ".join(sorted(unsupported_reader))
                        )
                    serial = str(reader_item.get("serial_number") or "").strip().upper()
                    reader_code = str(reader_item.get("technical_code") or "").strip().upper()
                    if not serial or not reader_code:
                        raise UserError(_("RFID Reader Management Code and Serial Number are required."))
                    reader_whitelist = whitelist_by_code.get(reader_code)
                    if not reader_whitelist:
                        raise UserError(_("Published RFID Reader %s is missing from the identity projection.") % reader_code)
                    parameters = reader_item.get("reader_parameters") or {}
                    reader_values = {
                        "name": reader_item.get("reader_name") or serial,
                        "controller_id": controller.id,
                        "connection_type": reader_item.get("physical_connection") or False,
                        "power_dbm": int(parameters.get("power_dbm") if parameters.get("power_dbm") is not None else 30),
                        "read_interval_ms": int(parameters.get("read_interval_ms") or 200),
                        "tid_addr": int(parameters.get("tid_start_address") or 0),
                        "tid_len": int(parameters.get("tid_length") or 4),
                        "device_code": reader_code,
                        "whitelist_id": reader_whitelist.id,
                        "active": True,
                        "cloud_removed": False,
                    }
                    reader = reader_by_serial.get(serial)
                    if reader:
                        self._write_changed(reader, reader_values)
                    else:
                        reader = Device.create({"serial_number": serial, **reader_values})
                        reader_by_serial[serial] = reader

                    antenna_items = reader_item.get("antennas") or []
                    if not isinstance(antenna_items, list) or not antenna_items:
                        raise UserError(_("Published RFID Reader %s has no Antenna Port Mapping.") % serial)
                    desired_codes = []
                    desired_ports = []
                    normalized_antennas = []
                    for antenna_item in antenna_items:
                        if not isinstance(antenna_item, dict) or set(antenna_item) - {
                            "antenna_no", "technical_code", "serial_number", "name",
                        }:
                            raise UserError(_("Reader Antenna payload contains unsupported fields."))
                        port_no = int(antenna_item.get("antenna_no") or 0)
                        antenna_code = str(antenna_item.get("technical_code") or "").strip().upper()
                        if port_no <= 0 or not antenna_code:
                            raise UserError(_("Antenna Port No. and Management Code are required."))
                        if port_no in desired_ports or antenna_code in desired_codes:
                            raise UserError(_("Antenna Port Mapping is duplicated for Reader %s.") % serial)
                        desired_ports.append(port_no)
                        desired_codes.append(antenna_code)
                        normalized_antennas.append((antenna_item, port_no, antenna_code))

                    # Move canonical Parking rows to temporary positive ports so
                    # Reader-port swaps can be applied without violating SQL uniqueness.
                    movable = Antenna.search([
                        "|",
                        ("technical_code", "in", desired_codes),
                        "&", ("device_id", "=", reader.id), ("antenna_no", "in", desired_ports),
                    ])
                    for antenna in movable:
                        temporary_port = 1000000 + antenna.id
                        if antenna.antenna_no != temporary_port:
                            antenna.write({"antenna_no": temporary_port})

                    for antenna_item, port_no, antenna_code in normalized_antennas:
                        antenna_whitelist = whitelist_by_code.get(antenna_code)
                        if not antenna_whitelist:
                            raise UserError(_("Published Antenna %s is missing from the identity projection.") % antenna_code)
                        incoming_parking_antenna_codes.add(antenna_code)
                        antenna_values = {
                            "technical_code": antenna_code,
                            "serial_number": str(antenna_item.get("serial_number") or "").strip().upper() or False,
                            "device_id": reader.id,
                            "antenna_no": port_no,
                            "whitelist_id": antenna_whitelist.id,
                            "active": True,
                            "cloud_removed": False,
                        }
                        antenna = antenna_by_code.get(antenna_code)
                        if antenna:
                            self._write_changed(antenna, antenna_values)
                        else:
                            antenna = Antenna.create(antenna_values)
                            antenna_by_code[antenna_code] = antenna

            # Only canonical Parking port rows carry technical_code. Contextual
            # calibration rows use NULL technical_code and are not archived here.
            stale_parking_antennas = Antenna.search([
                ("technical_code", "!=", False),
                ("technical_code", "not in", list(incoming_parking_antenna_codes)),
                ("active", "=", True),
            ]) if incoming_parking_antenna_codes else Antenna.search([
                ("technical_code", "!=", False), ("active", "=", True),
            ])
            if stale_parking_antennas:
                stale_parking_antennas.write({"active": False, "cloud_removed": True})

            for area in areas:
                self._apply_parking_config(area)
            removed_area = self._reconcile_parking_config_snapshot(areas)
            self._validate_operational_parking_topology()
            self.write({
                "snapshot_revision": revision,
                "last_pull_at": fields.Datetime.now(),
                "sync_cursor": False,
            })

        return {
            "applied": len(controllers) + len(areas) + len(whitelist),
            "removed": len(stale_parking_antennas) + removed_area,
            "revision": revision,
            "stale": False,
        }

    def _apply_items(self, kind, items, request_payload=False):
        self.ensure_one()
        results, failed = [], []
        Record = self.env["nsp.sync.record"].sudo()
        handlers = {
            "user": self._apply_user,
            "vehicle": self._apply_vehicle,
            "vehicle_borrow": self._apply_vehicle_borrow,
            "lane_calibration": self._apply_lane_calibration,
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
        if self._action_kind() in ("parking_runtime", "vehicle_config", "rfid_tag", "user", "vehicle", "vehicle_borrow", "lane_calibration"):
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
            "record_key", "tid", "borrow_uid", "branch_code", "user_code",
            "vehicle_code", "license_plate", "parking_area_code", "transaction_uid",
            "measurement_code", "event_uid", "serial_number", "code",
            "controller_code", "edge_server_code",
        ):
            if item.get(field_name):
                return str(item[field_name])
        return False

    # ------------------------ lane calibration push -------------------
    @api.model
    def _lane_calibration_event_payload(self, event):
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
                else event.session_id._reader_power_for_serial(event.serial_number)
            ),
            "read_interval_ms": int(
                event.read_interval_ms
                if event.read_interval_ms is not None
                else event.session_id._reader_interval_for_serial(event.serial_number)
            ),
        }
        if event.rssi_dbm not in (False, None):
            payload["rssi_dbm"] = float(event.rssi_dbm)
        return payload

    def _pending_lane_calibration_events(self, limit):
        self.ensure_one(); Record=self.env["nsp.sync.record"].sudo(); Event=self.env["nsp.measurement.event"].sudo(); action_code=str(self.sync_action_code or "").strip(); source_code=str(self.edge_server_code or "NSP").strip() or "NSP"
        synced=Record.search([("source_code","=",source_code),("sync_action_code","=",action_code),("operation","=","push"),("status","=","synced")]).mapped("record_key")
        domain=[("event_uid","not in",synced)] if synced else []
        return Event.search(domain,order="read_at,id",limit=max(1,int(limit or 1)))

    def _push_lane_calibration_event_records(self, events, timeout=120):
        self.ensure_one()
        events = events.sudo().exists().sorted(key=lambda event: event.id)
        if not events:
            return {"pushed": 0, "failed": 0, "has_more": False, "message": "No Lane Calibration Events to push."}
        session = events[0].session_id
        events = events.filtered(lambda event: event.session_id == session)
        Record = self.env["nsp.sync.record"].sudo()
        payload = {
            "edge_server_code": self.edge_server_code,
            "measurement_code": session.measurement_code,
            "events": [self._lane_calibration_event_payload(event) for event in events],
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
                payload=self._lane_calibration_event_payload(event),
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
                    payload=self._lane_calibration_event_payload(event),
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
                payload=self._lane_calibration_event_payload(event),
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
            message = _("Cloud rejected %s Lane Calibration Event(s).") % failed
            if reason_text:
                message += " " + _("Reasons: %s") % reason_text
            raise UserError(message)
        self.last_push_at = fields.Datetime.now()
        return {
            "pushed": len(events),
            "failed": 0,
            "has_more": bool(self._pending_lane_calibration_events(1)),
            "message": "Pushed %s Lane Calibration Event(s)." % len(events),
        }

    def _run_lane_calibration_event_push_once(self):
        self.ensure_one()
        events = self._pending_lane_calibration_events(
            max(1, min(int(self.batch_size or 100), 100))
        )
        return self._push_lane_calibration_event_records(events)

    @api.model
    def _ensure_edge_sync_jobs(self):
        """Repair missing default Sync Jobs for existing Edge connections.

        New route types may be introduced after an Edge connection already
        exists.  Do not require an operator to re-authenticate merely to create
        those jobs: the scheduler and immediate Lane Calibration forwarding can
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
    def _lane_calibration_push_job(self, route_suffix):
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
    def push_lane_calibration_events_now(self, events):
        job = self._lane_calibration_push_job("edge/lane-calibrations/events")
        if not job:
            _logger.warning(
                "Lane Calibration Event forwarding deferred: no edge/lane-calibrations/events job is available."
            )
            return False
        try:
            job._push_lane_calibration_event_records(events, timeout=3)
            return True
        except Exception:
            _logger.exception("Immediate Lane Calibration Event forwarding failed; fallback retry remains pending.")
            return False

    @api.model
    def _lane_calibration_status_payload(self, session):
        occurred_at = session.ended_at or session.started_at or session.write_date or fields.Datetime.now()
        return {
            "edge_server_code": self.edge_server_code,
            "measurement_code": session.measurement_code,
            "status": session.status,
            "occurred_at": self._iso_utc(occurred_at),
        }

    def _pending_lane_calibration_status_sessions(self, limit):
        self.ensure_one(); Session=self.env["nsp.measurement.session"].sudo(); Record=self.env["nsp.sync.record"].sudo(); result=Session.search([("status","!=","draft")],order="write_date,id")
        pending=[]
        for session in result:
            synced=Record.search([("sync_action_code","=",self.sync_action_code),("operation","=","push"),("record_key","=",session.measurement_code),("status","=","synced"),("last_synced_at",">=",session.write_date)],limit=1)
            if not synced: pending.append(session.id)
            if len(pending)>=max(1,int(limit or 1)): break
        return Session.browse(pending)

    def _push_lane_calibration_status_records(self, sessions, timeout=120):
        self.ensure_one()
        sessions = sessions.sudo().exists().sorted(key=lambda session: (session.write_date, session.id))
        if not sessions:
            return {"pushed": 0, "failed": 0, "has_more": False, "message": "No Lane Calibration Status to push."}
        Record = self.env["nsp.sync.record"].sudo()
        pushed = 0
        for session in sessions:
            payload = self._lane_calibration_status_payload(session)
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
                message="Lane Calibration Status accepted by Cloud.",
                payload=payload,
                response=data,
                operation="push",
            )
            pushed += 1
        self.last_push_at = fields.Datetime.now()
        return {
            "pushed": pushed,
            "failed": 0,
            "has_more": bool(self._pending_lane_calibration_status_sessions(1)),
            "message": "Pushed %s Lane Calibration Status record(s)." % pushed,
        }

    def _run_lane_calibration_status_push_once(self):
        self.ensure_one()
        sessions = self._pending_lane_calibration_status_sessions(
            max(1, min(int(self.batch_size or 100), 1000))
        )
        return self._push_lane_calibration_status_records(sessions)

    @api.model
    def push_lane_calibration_status_now(self, session):
        job = self._lane_calibration_push_job("edge/lane-calibrations/status")
        if not job:
            _logger.warning(
                "Lane Calibration Status forwarding deferred: no edge/lane-calibrations/status job is available."
            )
            return False
        try:
            job._push_lane_calibration_status_records(session, timeout=3)
            return True
        except Exception:
            _logger.exception("Immediate Lane Calibration Status forwarding failed; fallback retry remains pending.")
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

        if kind == "rfid_tag":
            counts, stale_assignment_count = self._apply_rfid_tag_snapshot(data, request_payload=request_payload)
            self.write({"last_pull_at": fields.Datetime.now(), "sync_cursor": False})
            return {
                "pulled": counts["whitelisted_tags"],
                "failed": 0,
                "has_more": False,
                "message": (
                    "RFID Tag Whitelist applied: %(tags)s tag(s), %(employees)s employee assignment(s), "
                    "%(vehicles)s vehicle assignment(s), %(unassigned)s unassigned; revoked %(stale)s stale local assignment(s)."
                ) % {
                    "tags": counts["whitelisted_tags"],
                    "employees": counts["employee_assignments"],
                    "vehicles": counts["vehicle_assignments"],
                    "unassigned": counts["unassigned_tags"],
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
