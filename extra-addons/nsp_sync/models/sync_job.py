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
    "devices-status/sync": "push",
    "branches/sync": "pull",
    "cards/sync": "pull",
    "employees/sync": "pull",
    "vehicles/sync": "pull",
    "vehicle-borrow/sync": "pull",
    "parking-config/sync": "pull",
    "measurement-config/sync": "pull",
    "measurement-data/sync": "push",
    "measurement-session-status/sync": "push",
    "parking-transactions/sync": "push",
}
NSP_SYNC_ALLOWED_ROUTES = tuple(SYNC_ROUTE_DIRECTIONS)
DEFAULT_JOB_SETTINGS = {
    "edge-server/status": {"interval_seconds": 60, "batch_size": 1},
    "devices-status/sync": {"interval_seconds": 60, "batch_size": 200},
    "branches/sync": {"interval_seconds": 300, "batch_size": 500},
    "cards/sync": {"interval_seconds": 300, "batch_size": 500},
    "employees/sync": {"interval_seconds": 300, "batch_size": 500},
    "vehicles/sync": {"interval_seconds": 300, "batch_size": 500},
    "vehicle-borrow/sync": {"interval_seconds": 300, "batch_size": 500},
    "parking-config/sync": {"interval_seconds": 300, "batch_size": 100},
    "measurement-config/sync": {"interval_seconds": 60, "batch_size": 100},
    "measurement-data/sync": {"interval_seconds": 30, "batch_size": 200},
    "measurement-session-status/sync": {"interval_seconds": 30, "batch_size": 100},
    "parking-transactions/sync": {"interval_seconds": 30, "batch_size": 200},
}
ACTION_KINDS = {
    "edge-server/status": "edge_server_status",
    "devices-status/sync": "device_status",
    "branches/sync": "branch",
    "cards/sync": "card",
    "employees/sync": "user",
    "vehicles/sync": "vehicle",
    "vehicle-borrow/sync": "vehicle_borrow",
    "parking-config/sync": "parking_config",
    "measurement-config/sync": "measurement_config",
    "measurement-data/sync": "measurement_event",
    "measurement-session-status/sync": "measurement_status",
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
    interval_seconds = fields.Integer(default=60, required=True)
    batch_size = fields.Integer(default=100, required=True)
    sync_cursor = fields.Char(string="Next Sync Cursor", readonly=True, copy=False)
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
    nsp_remote_service_code = fields.Char(related="auth_id.remote_service_code", readonly=True)
    nsp_connected = fields.Boolean(related="auth_id.connected", readonly=True)
    nsp_last_error = fields.Text(related="auth_id.last_error", readonly=True)

    _sql_constraints = [
        ("interval_positive", "CHECK(interval_seconds >= 1)", "Interval Seconds must be at least 1."),
        ("batch_positive", "CHECK(batch_size >= 1)", "Batch Size must be at least 1."),
        (
            "job_unique",
            "unique(sync_action_id, auth_id, direction)",
            "Only one Sync Job is allowed per API, Cloud Connection, and direction.",
        ),
    ]

    @api.depends("sync_action_name", "direction", "interval_seconds", "auth_id", "auth_id.display_name")
    def _compute_display_name(self):
        labels = dict(self._fields["direction"].selection)
        for rec in self:
            rec.display_name = "%s / %s / %s / %ss" % (
                rec.auth_id.display_name or "Cloud",
                rec.sync_action_name or rec.route_suffix or "-",
                labels.get(rec.direction, rec.direction or "-"),
                rec.interval_seconds or 0,
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
            for sequence, route in enumerate(NSP_SYNC_ALLOWED_ROUTES, start=1):
                if route in existing_routes:
                    continue
                settings = DEFAULT_JOB_SETTINGS[route]
                vals_list.append({
                    "sequence": sequence * 10,
                    "auth_id": auth.id,
                    "sync_action_id": action_by_route[route].id,
                    "version_id": version.id,
                    "direction": SYNC_ROUTE_DIRECTIONS[route],
                    "interval_seconds": settings["interval_seconds"],
                    "batch_size": settings["batch_size"],
                    "next_run_at": now,
                    "active": True,
                })
            if vals_list:
                created |= self.create(vals_list)
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
            vals["interval_seconds"] = max(1, int(vals.get("interval_seconds") or 60))
            vals["batch_size"] = max(1, min(int(vals.get("batch_size") or 100), 1000))
            vals.setdefault("next_run_at", fields.Datetime.now())
            prepared.append(vals)
        return super().create(prepared)

    def write(self, vals):
        if {"auth_id", "sync_action_id", "direction", "active", "interval_seconds", "batch_size"}.intersection(vals):
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
        if "interval_seconds" in values:
            values["interval_seconds"] = max(1, int(values.get("interval_seconds") or 1))
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
                else now + timedelta(seconds=max(1, rec.interval_seconds or 1)) if rec.active
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
        self.ensure_one()
        edge = self._require_edge_server_record()
        return {
            "record_key": self.edge_server_code,
            "edge_server_code": self.edge_server_code,
            "current_status": "online",
            "last_seen_at": self._dt(fields.Datetime.now()),
        }

    @api.model
    def _serialize_device_status(self, device):
        controller = device.controller_id
        status = str(device.status or "offline").lower()
        device_status = status if status in ("online", "offline", "degraded") else "offline"
        antennas = []
        for antenna in device.antennas_ids.sorted(key=lambda rec: (rec.antenna_id, rec.id)):
            antenna_status = str(antenna.status or "offline").strip().lower()
            if antenna_status not in ("online", "offline", "degraded"):
                antenna_status = "online" if antenna.is_active else "offline"
            antennas.append({
                "antenna_no": int(antenna.antenna_id or 0),
                "antenna_status": antenna_status,
                "enabled": bool(antenna.is_active),
                **({"power_dbm": int(antenna.power_dbm)} if antenna.power_dbm not in (False, None) else {}),
                **({"return_loss_db": int(antenna.return_loss_db)} if antenna.return_loss_db not in (False, None) else {}),
                **({"last_seen_at": self._dt(device.last_seen)} if device.last_seen else {}),
            })
        return {
            "record_key": device.serial_number or device.device_code,
            "controller_code": controller.controller_id if controller else "",
            "serial_number": device.serial_number or "",
            **({"device_code": device.device_code} if device.device_code else {}),
            "device_status": device_status,
            "last_seen_at": self._dt(device.last_seen or fields.Datetime.now()),
            **({"firmware_version": device.firmware_version} if device.firmware_version else {}),
            "connection": {
                **({"ip_address": device.device_ip} if device.device_ip else {}),
                **({"port": int(device.device_port)} if device.device_port else {}),
            },
            "antennas": antennas,
        }

    @api.model
    def _serialize_parking_transaction(self, record):
        decision = record.status if record.status in ("allowed", "denied") else "denied"
        return {
            "record_key": record.transaction_uid,
            "transaction_uid": record.transaction_uid,
            "controller_code": record.controller_id.controller_id if record.controller_id else "",
            "parking_area_code": record.parking_area_code or (record.parking_area_id.code if record.parking_area_id else ""),
            "lane_code": record.lane_code or (record.lane_id.code if record.lane_id else ""),
            "direction": record.direction,
            "check_time": self._dt(record.time_entered),
            "vehicle_tid": record.vehicle_tid or "",
            "user_tid": record.user_tid or "",
            "vehicle_code": record.vehicle_code or "",
            "user_code": record.user_code or "",
            "decision": decision,
            **({"decision_reason_code": record.error_code or "unknown"} if decision == "denied" else {}),
        }

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
        if kind == "device_status":
            records = self.env["nsp.device"].sudo().search(
                domain + [("controller_id.edge_server_id", "=", edge.id)],
                order="write_date asc, id asc",
                limit=limit + 1,
            )
            serializer = self._serialize_device_status
        elif kind == "parking_transaction":
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
        owner_code = str(item.get("owner_user_code") or "").strip()
        owner = self.env["nsp.user"].sudo().search([("user_code", "=", owner_code)], limit=1) if owner_code else self.env["nsp.user"].browse()
        if not owner:
            owner = self._apply_user({"user_code": owner_code or ("OWNER-%s" % code), "name": owner_code or code, "active": True})
        vals = {
            "license_plate": plate,
            "owner_id": owner.id,
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
    def _assign_card(self, card, owner_type, owner_code, active=True):
        UserLine = self.env["nsp.user.card"].sudo()
        VehicleLine = self.env["nsp.vehicle.card"].sudo()
        user_lines = UserLine.search([("card_id", "=", card.id)])
        vehicle_lines = VehicleLine.search([("card_id", "=", card.id)])
        user_lines.filtered(lambda line: line.state == "active").action_revoke()
        vehicle_lines.filtered(lambda line: line.state == "active").action_revoke()
        if not active or owner_type == "unassigned":
            return
        if owner_type == "user":
            owner = self.env["nsp.user"].sudo().search([("user_code", "=", owner_code)], limit=1)
            if not owner:
                raise UserError(_("Card owner user %s was not found.") % (owner_code or "-"))
            line = user_lines.filtered(lambda value: value.user_id == owner)[:1]
            vals = {"user_id": owner.id, "card_id": card.id, "state": "active", "revoked_at": False}
            line.write(vals) if line else UserLine.create(vals)
            return
        if owner_type == "vehicle":
            owner = self._find_vehicle(owner_code)
            if not owner:
                raise UserError(_("Card owner vehicle %s was not found.") % (owner_code or "-"))
            line = vehicle_lines.filtered(lambda value: value.vehicle_id == owner)[:1]
            vals = {"vehicle_id": owner.id, "card_id": card.id, "state": "active", "revoked_at": False}
            line.write(vals) if line else VehicleLine.create(vals)
            return
        raise UserError(_("Invalid card owner type: %s") % (owner_type or "-"))

    @api.model
    def _apply_card(self, item):
        tid = str(item.get("card_uid") or "").strip().upper().replace(" ", "")
        card_type = item.get("card_type")
        if not tid:
            raise UserError(_("Card UID is required."))
        if card_type not in ("vehicle_card", "user_card"):
            raise UserError(_("Invalid Card Type."))
        card = self._card(tid, card_type)
        self._assign_card(
            card,
            str(item.get("owner_type") or "unassigned").strip().lower(),
            str(item.get("owner_code") or "").strip(),
            active=bool(item.get("active", True)),
        )
        return card

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
        Device = self.env["nsp.device"].sudo().with_context(active_test=False)
        device = Device.search([("serial_number", "=", serial)], limit=1)
        reader_type = self.env.ref("nsp_gatekeeper.nsp_device_type_rfid_reader", raise_if_not_found=False)
        if not reader_type:
            reader_type = self.env["nsp.device.type"].sudo().search([("code", "=", "rfid_reader")], limit=1)
        vals = {
            "controller_id": controller.id,
            "managed": True,
            "device_code": serial,
            "device_name": device.device_name if device and device.device_name else serial,
            "device_type_id": reader_type.id if reader_type else False,
        }
        if device:
            device.write(vals)
            return device
        vals["serial_number"] = serial
        return Device.create(vals)

    def _apply_measurement_config(self, item):
        self.ensure_one()
        uid = str(item.get("measurement_session_uid") or "").strip().upper()
        code = str(item.get("measurement_code") or uid).strip().upper()
        controller_code = str(item.get("controller_code") or "").strip().upper()
        if not uid or not controller_code:
            raise UserError(_("Measurement Session UID and Controller Code are required."))
        status = str(item.get("measurement_status") or "ready").strip().lower()
        if status not in ("ready", "measuring", "completed", "cancelled"):
            raise UserError(_("Invalid Measurement Session status: %s") % status)
        controller = self._find_or_create_controller(controller_code)
        Session = self.env["nsp.measurement.session"].sudo().with_context(measurement_sync=True)
        session = Session.search([("measurement_session_uid", "=", uid)], limit=1)
        vals = {
            "measurement_session_uid": uid,
            "measurement_code": code,
            "controller_id": controller.id,
            "measurement_status": status,
            "planned_start_at": self._remote_datetime(item.get("planned_start_at")),
            "planned_end_at": self._remote_datetime(item.get("planned_end_at")),
            "note": str(item.get("note") or "").strip() or False,
        }
        if session:
            session.write(vals)
        else:
            session = Session.create(vals)

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
                antenna = Antenna.search([("device_id", "=", device.id), ("antenna_id", "=", antenna_no)], limit=1)
                if not antenna:
                    antenna = Antenna.create({
                        "device_id": device.id,
                        "antenna_id": antenna_no,
                        "status": "offline",
                        "is_active": True,
                    })
                antenna_refs |= antenna
        if not antenna_refs:
            raise UserError(_("Measurement Configuration has no antennas."))
        session.antenna_ids.with_context(measurement_sync=True).unlink()
        self.env["nsp.measurement.antenna"].sudo().with_context(measurement_sync=True).create([
            {"session_id": session.id, "antenna_ref_id": antenna.id}
            for antenna in antenna_refs
        ])
        return session

    def _apply_parking_config(self, item):
        self.ensure_one()
        branch_code = str(item.get("branch_code") or "").strip().upper()
        area_code = str(item.get("parking_area_code") or "").strip().upper()
        if not branch_code or not area_code:
            raise UserError(_("Branch Code and Parking Area Code are required."))
        branch = self.env["nsp.branch"].sudo().search([("code", "=", branch_code)], limit=1)
        if not branch:
            branch = self._apply_branch({"branch_code": branch_code, "branch_name": branch_code, "active": True})

        Parking = self.env["nsp.parking.area"].sudo().with_context(active_test=False)
        parking = Parking.search([("code", "=", area_code)], limit=1)
        vals = {
            "code": area_code,
            "name": item.get("parking_area_name") or area_code,
            "branch_id": branch.id,
            "status": "active" if bool(item.get("active", True)) else "blocked",
            "operation_state": "operational" if bool(item.get("operational")) else "draft",
        }
        parking.write(vals) if parking else None
        if not parking:
            parking = Parking.create(vals)

        Lane = self.env["nsp.parking.lane"].sudo().with_context(active_test=False)
        Group = self.env["nsp.parking.lane.antenna.group"].sudo()
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
            if not lane_code or not controller_code:
                raise UserError(_("Each Parking Lane requires lane_code and controller_code."))
            incoming_codes.append(lane_code)
            controller = self._find_or_create_controller(controller_code)
            lane = Lane.search([("parking_area_id", "=", parking.id), ("code", "=", lane_code)], limit=1)
            lane_type = lane_item.get("lane_type") if lane_item.get("lane_type") in ("one_way", "two_way") else "one_way"
            direction = lane_item.get("direction")
            if lane_type == "two_way":
                direction = "both"
            elif direction not in ("entry", "exit"):
                direction = "entry"
            lane_vals = {
                "parking_area_id": parking.id,
                "code": lane_code,
                "name": lane_item.get("lane_name") or lane_code,
                "controller_id": controller.id,
                "lane_no": int(lane_item.get("lane_no") or lane_index),
                "sequence": int(lane_item.get("sequence") or lane_index * 10),
                "lane_type": lane_type,
                "direction": direction,
                "detection_window_ms": int(lane_item.get("detection_window_ms") or parking.detection_window_ms or 1500),
                "required_vehicle_tid": bool(lane_item.get("required_vehicle_tid", True)),
                "required_user_tid": bool(lane_item.get("required_user_tid", False)),
                "active": True,
            }
            lane.write(lane_vals) if lane else None
            if not lane:
                lane = Lane.create(lane_vals)
            lane.antenna_group_ids.unlink()

            groups_data = lane_item.get("antenna_groups") or []
            if not isinstance(groups_data, list):
                raise UserError(_("Antenna groups must be an array."))
            for group_index, group_item in enumerate(groups_data, start=1):
                if not isinstance(group_item, dict):
                    raise UserError(_("Antenna groups must contain objects."))
                group_direction = str(group_item.get("direction") or "").strip().lower()
                if group_direction not in ("entry", "exit"):
                    raise UserError(_("Antenna Group direction must be entry or exit."))
                detection_mode = group_item.get("detection_mode")
                if detection_mode not in ("any", "sequential"):
                    detection_mode = "any"
                group = Group.create({
                    "lane_id": lane.id,
                    "direction": group_direction,
                    "detection_mode": detection_mode,
                    "sequence": int(group_item.get("sequence") or group_index * 10),
                    "active": True,
                })
                antennas_data = group_item.get("antennas") or []
                if not isinstance(antennas_data, list):
                    raise UserError(_("Antenna mappings must be an array."))
                for antenna_index, antenna_item in enumerate(antennas_data, start=1):
                    if not isinstance(antenna_item, dict):
                        raise UserError(_("Antenna mappings must contain objects."))
                    serial = str(antenna_item.get("serial_number") or "").strip().upper()
                    antenna_no = int(antenna_item.get("antenna_no") or 0)
                    if not serial or antenna_no <= 0:
                        raise UserError(_("Each antenna mapping requires serial_number and antenna_no."))
                    device = self._find_or_create_device(controller, serial)
                    Antenna = self.env["nsp.device.antenna"].sudo()
                    antenna = Antenna.search([("device_id", "=", device.id), ("antenna_id", "=", antenna_no)], limit=1)
                    if not antenna:
                        antenna = Antenna.create({
                            "device_id": device.id,
                            "antenna_id": antenna_no,
                            "status": "offline",
                            "is_active": True,
                        })
                    sequence_no = int(antenna_item.get("sequence_no") or antenna_index) if detection_mode == "sequential" else 0
                    tag_role = antenna_item.get("tag_role")
                    if tag_role not in ("vehicle_tid", "user_tid", "both"):
                        tag_role = "vehicle_tid"
                    Mapping.create({
                        "antenna_group_id": group.id,
                        "antenna_ref_id": antenna.id,
                        "tag_role": tag_role,
                        "sequence_no": sequence_no,
                        "is_active": True,
                    })
        stale_domain = [("parking_area_id", "=", parking.id)]
        if incoming_codes:
            stale_domain.append(("code", "not in", incoming_codes))
        Lane.search(stale_domain).write({"active": False})
        return parking

    def _apply_items(self, kind, items):
        self.ensure_one()
        results, failed = [], []
        Record = self.env["nsp.sync.record"].sudo()
        handlers = {
            "branch": self._apply_branch,
            "card": self._apply_card,
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
                    payload=item,
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
                    payload=item,
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
            "measurement_session_uid", "measurement_uid", "serial_number",
            "controller_code", "edge_server_code",
        ):
            if item.get(field_name):
                return str(item[field_name])
        return False

    # --------------------------- measurement push ---------------------
    @api.model
    def _measurement_event_payload(self, event):
        payload = {
            "measurement_uid": event.measurement_uid,
            "controller_code": event.session_id.controller_id.controller_id,
            "serial_number": event.serial_number,
            "antenna_no": int(event.antenna_no),
            "tid": event.tid,
            "read_at": self._iso_utc(event.read_at),
        }
        if event.rssi_dbm not in (False, None):
            payload["rssi_dbm"] = float(event.rssi_dbm)
        return payload

    def _run_measurement_event_push_once(self):
        self.ensure_one()
        edge = self._require_edge_server_record()
        Event = self.env["nsp.measurement.event"].sudo()
        now = fields.Datetime.now()
        scope = [("session_id.controller_id.edge_server_id", "=", edge.id)]
        retry_domain = [
            ("sync_state", "in", ["pending", "failed"]),
            "|", ("next_retry_at", "=", False), ("next_retry_at", "<=", now),
        ]
        first = Event.search(retry_domain + scope, order="id asc", limit=1)
        if not first:
            return {"pushed": 0, "failed": 0, "has_more": False, "message": "No Measurement Events to push."}
        events = Event.search(
            retry_domain + [("session_id", "=", first.session_id.id), ("run_id", "=", first.run_id.id)],
            order="id asc",
            limit=max(1, min(int(self.batch_size or 100), 1000)),
        )
        payload = {
            "edge_server_code": self.edge_server_code,
            "measurement_session_uid": first.session_id.measurement_session_uid,
            "measurement_run_uid": first.run_id.measurement_run_uid,
            "measurements": [self._measurement_event_payload(event) for event in events],
        }
        try:
            data = self._json_or_error(self._post_remote(self.sync_action_id, payload, timeout=120))
        except Exception:
            for event in events:
                retry = int(event.retry_count or 0) + 1
                event.write({
                    "sync_state": "failed",
                    "retry_count": retry,
                    "next_retry_at": now + timedelta(seconds=min(60 * (2 ** min(retry, 6)), 3600)),
                })
            raise
        rejected_keys = {
            str(result.get("record_key") or "")
            for result in (data.get("results") or [])
            if isinstance(result, dict) and result.get("status") in ("rejected", "failed", "error")
        }
        reported_failed = int(data.get("failed") or 0)
        failed_events = events if reported_failed and not rejected_keys else events.filtered(
            lambda event: event.measurement_uid in rejected_keys
        )
        accepted_events = events - failed_events
        if accepted_events:
            accepted_events.write({
                "sync_state": "synced",
                "retry_count": 0,
                "last_sync_at": now,
                "next_retry_at": False,
            })
        for event in failed_events:
            retry = int(event.retry_count or 0) + 1
            event.write({
                "sync_state": "failed",
                "retry_count": retry,
                "next_retry_at": now + timedelta(seconds=min(60 * (2 ** min(retry, 6)), 3600)),
            })
        if failed_events or reported_failed:
            raise UserError(_("Cloud rejected %s Measurement Event(s).") % max(len(failed_events), reported_failed))
        has_more = bool(Event.search_count(retry_domain + scope))
        self.last_push_at = now
        return {
            "pushed": len(accepted_events),
            "failed": 0,
            "has_more": has_more,
            "message": "Pushed %s Measurement Event(s)." % len(accepted_events),
        }

    @api.model
    def _measurement_status_payload(self, session):
        runs = []
        for run in session.run_ids.sorted(key=lambda value: (value.id,)):
            item = {
                "measurement_run_uid": run.measurement_run_uid,
                "run_status": run.run_status,
                "measurement_count": int(run.measurement_count or 0),
            }
            if run.started_at:
                item["started_at"] = self._iso_utc(run.started_at)
            if run.stopped_at:
                item["stopped_at"] = self._iso_utc(run.stopped_at)
            runs.append(item)
        return {
            "edge_server_code": self.edge_server_code,
            "measurement_session_uid": session.measurement_session_uid,
            "measurement_status": session.measurement_status,
            "runs": runs,
            "reported_at": self._iso_utc(fields.Datetime.now()),
        }

    def _run_measurement_status_push_once(self):
        self.ensure_one()
        edge = self._require_edge_server_record()
        limit = max(1, min(int(self.batch_size or 100), 1000))
        Session = self.env["nsp.measurement.session"].sudo()
        sessions = Session.search(
            self._push_cursor_domain() + [
                ("controller_id.edge_server_id", "=", edge.id),
                ("measurement_status", "!=", "draft"),
            ],
            order="write_date asc, id asc",
            limit=limit + 1,
        )
        has_more = len(sessions) > limit
        selected = sessions[:limit]
        if not selected:
            return {"pushed": 0, "failed": 0, "has_more": False, "message": "No Measurement Session status to push."}
        Record = self.env["nsp.sync.record"].sudo()
        for session in selected:
            payload = self._measurement_status_payload(session)
            try:
                data = self._json_or_error(self._post_remote(self.sync_action_id, payload, timeout=120))
            except Exception as exc:
                Record.mark_result(
                    sync_job=self,
                    action_code=self.sync_action_code,
                    action_name=self.sync_action_name,
                    route_suffix=self.route_suffix,
                    record=session,
                    record_key=session.measurement_session_uid,
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
                record_key=session.measurement_session_uid,
                status="synced",
                message="Measurement Session status accepted by Cloud.",
                payload=payload,
                response=data,
                operation="push",
            )
        last = selected[-1]
        self.write({"last_push_at": last.write_date, "last_push_record_id": last.id})
        return {
            "pushed": len(selected),
            "failed": 0,
            "has_more": has_more,
            "message": "Pushed %s Measurement Session status record(s)." % len(selected),
        }

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
        data = self._json_or_error(self._post_remote(self.sync_action_id, self._build_pull_payload(), timeout=120))
        items = self._items_from_response(data)
        next_cursor = data.get("next_sync_cursor") or False
        has_more = bool(data.get("has_more"))
        if not items:
            self.write({
                "last_pull_at": fields.Datetime.now(),
                "sync_cursor": next_cursor if has_more else False,
            })
            return {"pulled": 0, "failed": 0, "has_more": has_more, "message": "No changed records to pull."}
        results, failed = self._apply_items(self._action_kind(), items)
        if failed:
            raise UserError(json.dumps(failed, ensure_ascii=False))
        self.write({
            "last_pull_at": fields.Datetime.now(),
            "sync_cursor": next_cursor if has_more else False,
        })
        return {
            "pulled": len(results),
            "failed": 0,
            "has_more": has_more,
            "message": "Pulled %s record(s)." % len(results),
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
