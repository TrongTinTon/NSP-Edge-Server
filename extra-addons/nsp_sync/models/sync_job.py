# -*- coding: utf-8 -*-
import json
import logging
import time
from datetime import timedelta

import requests


from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)


NSP_SYNC_ALLOWED_ROUTES = (
    "edge-server/status",
    "devices-status/sync",
    "branches/sync",
    "cards/sync",
    "employees/sync",
    "vehicles/sync",
    "vehicle-borrow/sync",
    "gate-config/sync",
    "gate-measurement/sync",
    "parking-transactions/sync",
    "controller-pairing-requests/sync",
    "controller-pairing-decisions/sync",
)


ACTION_KINDS = {
    "nsp_gatekeeper_edge_server_status": "edge_server_status",
    "nsp_gatekeeper_devices_status_sync": "device_status",
    "nsp_gatekeeper_branches_sync": "branch",
    "nsp_gatekeeper_cards_sync": "card",
    "nsp_gatekeeper_employees_sync": "user",
    "nsp_gatekeeper_vehicles_sync": "vehicle",
    "nsp_gatekeeper_gate_config_sync": "gate_config",
    "nsp_gatekeeper_gate_measurement_sync": "measurement",
    "nsp_gatekeeper_parking_transactions_sync": "parking_transaction",
    "nsp_gatekeeper_vehicle_borrow_sync": "vehicle_borrow",
    "nsp_controller_pairing_requests_sync": "pairing_request_push",
    "nsp_controller_pairing_decisions_sync": "pairing_decision",
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
        "nsp.sync.auth",
        string="Authentication",
        required=True,
        index=True,
        ondelete="restrict",
        help="Shared Remote Core API Authentication. Multiple Sync Jobs can reuse one authentication record.",
    )
    sync_action_id = fields.Many2one(
        "ir.actions.core_api",
        string="Sync Action",
        required=True,
        domain=[("endpoint_manager_id", "!=", False), ("endpoint_code", "!=", False), ("route_suffix", "in", list(NSP_SYNC_ALLOWED_ROUTES))],
        ondelete="restrict",
        help="Select the action definition from Action Endpoints Management. This is not tied to a fixed Core API Application.",
    )
    endpoint_manager_id = fields.Many2one(
        "action.endpoint.manager",
        string="Endpoint Manager",
        related="sync_action_id.endpoint_manager_id",
        store=True,
        readonly=True,
        index=True,
    )
    version_id = fields.Many2one(
        "core.api.version",
        string="API Version",
        default=lambda self: self.env["core.api.version"].get_default_version(),
        required=True,
        help="Remote API version segment used when building /<service_code>/<version>/<route>.",
    )
    sync_action_code = fields.Char(string="Action Code", compute="_compute_action_meta", store=True, index=True)
    sync_action_name = fields.Char(string="Action Name", compute="_compute_action_meta", store=True, index=True)
    route_suffix = fields.Char(string="Route", compute="_compute_action_meta", store=True)
    direction = fields.Selection([
        ("pull", "Pull"),
        ("push", "Push"),
    ], string="Direction", default="pull", required=True, index=True)
    interval_seconds = fields.Integer(string="Interval Seconds", default=10, required=True)
    batch_size = fields.Integer(string="Batch Size", default=100, required=True)
    pull_cursor = fields.Char(string="Opaque Pull Cursor", readonly=True, copy=False)
    last_push_at = fields.Datetime(string="Last Push At", readonly=True)
    last_pull_at = fields.Datetime(string="Last Pull At", readonly=True)
    next_run_at = fields.Datetime(string="Next Run At", readonly=True, index=True)
    status = fields.Selection([
        ("idle", "Idle"),
        ("running", "Running"),
        ("success", "Success"),
        ("failed", "Failed"),
        ("disabled", "Disabled"),
    ], string="Status", default="idle", readonly=True, index=True)
    last_message = fields.Text(string="Last Message", readonly=True)

    # Shared remote authentication. Tokens and credentials are stored once on
    # nsp.sync.auth, not duplicated per Sync Job. The related fields below are
    # read-only convenience fields used for display and existing sync-record tracing.
    edge_server_id = fields.Many2one(
        "nsp.controller",
        string="Edge Server Identity",
        related="auth_id.edge_server_id",
        readonly=True,
        store=True,
        index=True,
    )
    nsp_remote_server_url = fields.Char(string="Remote Server URL", related="auth_id.remote_server_url", readonly=True)
    nsp_remote_base_url = fields.Char(string="Resolved Remote URL", related="auth_id.remote_base_url", readonly=True)
    nsp_remote_service_code = fields.Char(string="Resolved Remote Server Code", related="auth_id.remote_service_code", readonly=True)
    nsp_remote_client_id = fields.Char(string="Remote Client ID", related="auth_id.client_id", readonly=True)
    nsp_connected = fields.Boolean(string="Connected", related="auth_id.connected", readonly=True)
    nsp_last_auth_at = fields.Datetime(string="Last Auth At", related="auth_id.last_auth_at", readonly=True)
    nsp_last_error = fields.Text(string="Last Auth Error", related="auth_id.last_error", readonly=True)

    _sql_constraints = [
        ("interval_positive", "CHECK(interval_seconds >= 1)", "Interval Seconds must be at least 1."),
        ("batch_positive", "CHECK(batch_size >= 1)", "Batch Size must be at least 1."),
        ("job_unique", "unique(sync_action_id, auth_id, direction)", "Only one sync job is allowed per Sync Action, Authentication, and Direction."),
    ]

    @api.depends("sync_action_name", "direction", "interval_seconds", "auth_id", "auth_id.display_name")
    def _compute_display_name(self):
        dir_labels = dict(self._fields["direction"].selection)
        for rec in self:
            remote = rec.auth_id.display_name or rec.nsp_remote_base_url or "remote"
            rec.display_name = "%s / %s / %s / %ss" % (
                remote,
                rec.sync_action_name or rec.sync_action_code or "-",
                dir_labels.get(rec.direction, rec.direction or "-"),
                rec.interval_seconds or 0,
            )

    @api.depends("sync_action_id", "sync_action_id.endpoint_code", "sync_action_id.name", "sync_action_id.route_suffix")
    def _compute_action_meta(self):
        for rec in self:
            action = rec.sync_action_id
            rec.sync_action_code = action.endpoint_code if action else False
            rec.sync_action_name = action.name if action else False
            rec.route_suffix = action.route_suffix if action else False

    @api.onchange("sync_action_id")
    def _onchange_sync_action(self):
        return

    @api.constrains("sync_action_id")
    def _check_sync_actions(self):
        for rec in self:
            if rec.sync_action_id and not rec.sync_action_id.endpoint_manager_id:
                raise ValidationError(_("Sync Action must come from Action Endpoints Management."))
            if rec.sync_action_id and not rec.sync_action_id.endpoint_code:
                raise ValidationError(_("Sync Action must have an Action Code. Click Generate API Actions Only on the Endpoint Manager."))
            if rec.sync_action_id and not rec.sync_action_id.route_suffix:
                raise ValidationError(_("Sync Action must have a Route Path. Click Generate API Actions Only on the Endpoint Manager."))
            if rec.sync_action_id and (rec.sync_action_id.route_suffix or "").strip().strip("/") not in NSP_SYNC_ALLOWED_ROUTES:
                raise ValidationError(_("This route is a runtime Controller API, not an NSP Sync route. Use only Edge Server Status/Devices Status/Branches/Cards/Users/Vehicles/Borrow/Gate Config/Parking Transactions/Measurement/Pairing sync routes."))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            vals.pop("sync_endpoint_id", None)
            vals.pop("nsp_remote_base_url", None)
            vals.pop("nsp_remote_server_url", None)
            vals.pop("nsp_remote_client_id", None)
            vals.pop("nsp_remote_client_secret", None)
            vals["interval_seconds"] = max(1, int(vals.get("interval_seconds") or 10))
            vals["batch_size"] = max(1, int(vals.get("batch_size") or 100))
            vals.setdefault("next_run_at", fields.Datetime.now())
        records = super().create(vals_list)
        records.action_auto_select_status_endpoint(silent=True)
        return records

    def write(self, vals):
        vals = dict(vals)
        vals.pop("sync_endpoint_id", None)
        vals.pop("nsp_remote_base_url", None)
        vals.pop("nsp_remote_server_url", None)
        vals.pop("nsp_remote_client_id", None)
        vals.pop("nsp_remote_client_secret", None)
        if "interval_seconds" in vals:
            vals["interval_seconds"] = max(1, int(vals.get("interval_seconds") or 1))
        if "batch_size" in vals:
            vals["batch_size"] = max(1, int(vals.get("batch_size") or 1))
        return super().write(vals)

    # --------------------------- shared remote auth helpers ----------------
    def _auth(self):
        self.ensure_one()
        if not self.auth_id:
            raise UserError(_("Select Authentication before running this Sync Job."))
        return self.auth_id

    def _nsp_normalize_remote_base_url(self):
        self.ensure_one()
        return self._auth()._normalize_remote_base_url()

    def _nsp_effective_remote_base_url(self):
        self.ensure_one()
        return self._auth()._effective_remote_base_url()

    def _nsp_effective_database_name(self):
        self.ensure_one()
        return self._auth()._effective_database_name()

    def _nsp_url(self, path):
        self.ensure_one()
        return self._auth()._url(path)

    def _nsp_remote_service_code(self):
        self.ensure_one()
        return self._auth()._remote_service_code()

    def _nsp_gateway_url(self, route_suffix, version_code="v1"):
        self.ensure_one()
        return self._auth().gateway_url(route_suffix, version_code=version_code)

    def _nsp_base_headers(self):
        self.ensure_one()
        return self._auth().base_headers()

    def nsp_sync_get_access_token(self, force=False):
        self.ensure_one()
        return self._auth().get_access_token(force=force)

    def nsp_sync_headers(self):
        self.ensure_one()
        return self._auth().sync_headers()

    def action_authenticate_application(self):
        for rec in self:
            if not rec.auth_id:
                raise UserError(_("Select Authentication before authenticating."))
            rec.auth_id.action_authenticate()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"title": _("NSP Sync"), "message": _("Remote Core API authentication completed."), "type": "success", "sticky": False},
        }

    def _schedule_next(self):
        """Schedule the next run for each job.

        Shared Authentication moved credentials/tokens out of the job model in v29,
        but the scheduler still belongs to nsp.sync.job. Keep this small and
        deterministic: no remote calls, no controller logic, only next_run_at.
        """
        now = fields.Datetime.now()
        for rec in self:
            if not rec.active:
                rec.write({"next_run_at": False})
                continue
            rec.write({"next_run_at": now + timedelta(seconds=max(1, rec.interval_seconds or 1))})

    def _post_remote(self, sync_action, payload=None, timeout=60):
        """Call a remote Core API route using the shared Authentication.

        This is server-to-server NSP Sync. It only uses Core API token + route
        permission. It must not resolve or validate nsp.controller records.
        """
        self.ensure_one()
        if not sync_action:
            raise UserError(_("Sync Action is required."))
        route_suffix = (sync_action.route_suffix or "").strip().strip("/")
        if not route_suffix:
            raise UserError(_("Sync Action route is required. Click Generate API Actions & Routes for this action."))
        if route_suffix not in NSP_SYNC_ALLOWED_ROUTES:
            raise UserError(_("Route %s is not an NSP Sync route.") % route_suffix)
        version_code = self.version_id.code if self.version_id else "v1"
        url = self._nsp_gateway_url(route_suffix, version_code=version_code)
        headers = self.nsp_sync_headers()
        try:
            return requests.post(url, json=payload or {}, headers=headers, timeout=timeout)
        except requests.exceptions.RequestException as exc:
            message = _("Cannot call remote NSP Sync API at %(url)s. Check Authentication Remote Server URL, resolved server code, Core API route permission, and database selector. Detail: %(detail)s") % {"url": url, "detail": str(exc)}
            self.write({"status": "failed", "last_message": message})
            raise UserError(message) from exc

    def _json_or_error(self, response):
        """Parse Core API JSON response and raise a user-safe error when failed."""
        try:
            data = response.json()
        except Exception:
            data = {"success": False, "error": response.text}
        if not isinstance(data, dict):
            raise UserError(_("Remote API returned an invalid response."))
        ok = data.get("success", data.get("ok", data.get("status") == "success"))
        if response.status_code >= 400 or not ok:
            raise UserError(data.get("error") or data.get("message") or ("HTTP %s" % response.status_code))
        if isinstance(data.get("data"), dict):
            merged = dict(data.get("data") or {})
            for key, value in data.items():
                if key not in merged:
                    merged[key] = value
            data = merged
        return data

    # --------------------------- action helpers ---------------------------
    def _action_kind(self):
        self.ensure_one()
        code = self.sync_action_code or ""
        if code in ACTION_KINDS:
            return ACTION_KINDS[code]
        route = (self.route_suffix or "").strip().strip("/")
        if route == "edge-server/status":
            return "edge_server_status"
        if route == "devices-status/sync":
            return "device_status"
        if route == "branches/sync":
            return "branch"
        if route == "cards/sync":
            return "card"
        if route in ("employees/sync", "users/sync"):
            return "user"
        if route == "vehicles/sync":
            return "vehicle"
        if route == "gate-config/sync":
            return "gate_config"
        if route == "gate-measurement/sync":
            return "measurement"
        if route == "parking-transactions/sync":
            return "parking_transaction"
        if route == "vehicle-borrow/sync":
            return "vehicle_borrow"
        if route == "controller-pairing-requests/sync":
            return "pairing_request_push"
        if route == "controller-pairing-decisions/sync":
            return "pairing_decision"
        return "generic"

    @api.model
    def _table_exists(self, table_name):
        self.env.cr.execute("SELECT to_regclass(%s)", (table_name,))
        return bool(self.env.cr.fetchone()[0])

    def _dt(self, value):
        return fields.Datetime.to_string(value) if value else False

    def _safe_dt(self, value):
        if not value:
            return False
        try:
            return fields.Datetime.to_string(fields.Datetime.to_datetime(value))
        except Exception:
            return fields.Datetime.now()

    @api.model
    def _user_key(self, user):
        return (getattr(user, "user_code", False) or user.name or str(user.id or "")).strip()

    @api.model
    def _serialize_branch(self, branch):
        return {
            "record_key": branch.code,
            "branch_code": branch.code,
            "branch_name": branch.name,
            "timezone": branch.timezone or "Asia/Ho_Chi_Minh",
            "status": branch.status,
            "note": branch.note,
            "write_date": self._dt(branch.write_date),
        }

    @api.model
    def _serialize_card(self, card):
        owner_type = "none"
        owner_code = ""
        owner_name = ""
        assignment_state = "available"
        if "nsp.vehicle.card" in self.env.registry.models:
            vehicle_line = self.env["nsp.vehicle.card"].sudo().search([("card_id", "=", card.id), ("state", "=", "active")], limit=1)
        else:
            vehicle_line = False
        if "nsp.user.card" in self.env.registry.models:
            user_line = self.env["nsp.user.card"].sudo().search([("card_id", "=", card.id), ("state", "=", "active")], limit=1)
        else:
            user_line = False
        if vehicle_line:
            owner_type = "vehicle"
            owner_code = vehicle_line.vehicle_id.license_plate or str(vehicle_line.vehicle_id.id)
            owner_name = vehicle_line.vehicle_id.display_name or vehicle_line.vehicle_id.license_plate or owner_code
            assignment_state = vehicle_line.state or "active"
        elif user_line:
            owner_type = "person"
            owner_code = getattr(user_line.user_id, "user_code", False) or str(user_line.user_id.id)
            owner_name = user_line.user_id.display_name or user_line.user_id.name or owner_code
            assignment_state = user_line.state or "active"
        return {
            "record_key": card.tid,
            "card_id": card.id,
            "tid": card.tid,
            "card_type": card.card_type,
            "active": True,
            "usage_state": "used" if owner_type != "none" else "available",
            "assignment_state": assignment_state,
            "owner_type": owner_type,
            "owner_code": owner_code,
            "owner_name": owner_name,
            "note": card.note,
            "write_date": self._dt(card.write_date),
        }

    @api.model
    def _serialize_user(self, user):
        cards = []
        for line in getattr(user, "user_card_ids", self.env["nsp.user.card"].browse()):
            cards.append({"tid": line.tid, "state": line.state, "assigned_at": self._dt(line.assigned_at), "revoked_at": self._dt(line.revoked_at), "note": line.note})
        user_code = self._user_key(user)
        return {"record_key": user_code, "user_code": user_code, "employee_id": user_code, "name": user.name, "pin": getattr(user, "pin", False), "active": getattr(user, "active", True), "email": getattr(user, "email", False), "phone": getattr(user, "phone", False), "user_tids": (user.user_rfid_tids.split(",") if getattr(user, "user_rfid_tids", False) else []), "cards": cards, "write_date": self._dt(user.write_date)}

    @api.model
    def _serialize_vehicle(self, vehicle):
        cards = []
        for line in getattr(vehicle, "vehicle_card_ids", self.env["nsp.vehicle.card"].browse()):
            cards.append({"tid": line.tid, "state": line.state, "assigned_at": self._dt(line.assigned_at), "revoked_at": self._dt(line.revoked_at), "note": line.note})
        owner = vehicle.owner_id
        return {"record_key": vehicle.license_plate, "license_plate": vehicle.license_plate, "state": vehicle.state, "owner_user_code": getattr(owner, "user_code", False), "owner_hr_code": getattr(owner, "user_code", False), "owner_name": owner.name if owner else False, "vehicle_type_name": vehicle.vehicle_type_id.name if vehicle.vehicle_type_id else False, "brand_name": vehicle.brand_id.name if vehicle.brand_id else False, "model_name": vehicle.model_id.name if vehicle.model_id else False, "color_name": vehicle.color_id.name if vehicle.color_id else False, "tid": vehicle.tid, "cards": cards, "write_date": self._dt(vehicle.write_date)}

    @api.model
    def _serialize_gate(self, gate):
        payload = gate.prepare_sync_payload() if hasattr(gate, "prepare_sync_payload") else {}
        return {
            "record_key": gate.code,
            "branch_code": gate.branch_id.code if gate.branch_id else False,
            "branch_name": gate.branch_id.name if gate.branch_id else False,
            "gate_code": gate.code,
            "gate_name": gate.name,
            "controller_codes": [c.controller_id for c in gate.controller_ids],
            "gate_status": gate.gate_status,
            "operation_state": gate.operation_state,
            "branch_timezone": gate.branch_id.timezone if gate.branch_id else "Asia/Ho_Chi_Minh",
            "timezone": gate.branch_id.timezone if gate.branch_id else "Asia/Ho_Chi_Minh",
            "detection_window_ms": gate.detection_window_ms,
            "sequence_required": gate.sequence_required,
            "entry_requires_user_tid": gate.entry_requires_user_tid,
            "exit_requires_user_tid": gate.exit_requires_user_tid,
            "config_revision": gate.config_revision,
            "config_hash": gate.config_hash,
            "config_state": gate.config_state,
            "applied_config_revision": gate.applied_config_revision,
            "applied_config_hash": gate.applied_config_hash,
            "lanes": payload.get("lanes", []),
            "write_date": self._dt(gate.write_date),
        }

    @api.model
    def _serialize_log(self, log):
        return {
            "record_key": log.transaction_uid,
            "local_id": log.controller_local_id or log.transaction_uid,
            "transaction_uid": log.transaction_uid,
            "controller_code": log.controller_id.controller_id if log.controller_id else False,
            "controller_name": log.controller_id.controller_name if log.controller_id else False,
            "branch_code": log.branch_id.code if getattr(log, "branch_id", False) else False,
            "gate_code": log.gate_code or (log.gate_id.code if log.gate_id else False),
            "gate_name": log.gate_id.name if log.gate_id else False,
            "lane_code": getattr(log, "lane_code", False),
            "time_entered": self._dt(log.time_entered),
            "direction": log.direction,
            "status": log.status,
            "error_message": log.error_message,
            "license_plate": log.license_plate,
            "vehicle_tid": log.vehicle_tid,
            "user_tid": log.user_tid,
            "device_serial": log.device_serial,
            "antenna_id": log.antenna_id,
            "antenna_sequence": log.antenna_sequence,
            "effective_direction": getattr(log, "effective_direction", False),
            "config_revision": log.config_revision,
            "write_date": self._dt(log.write_date),
        }

    @api.model
    def _serialize_vehicle_borrow(self, borrow):
        if hasattr(borrow, "_controller_payload"):
            return borrow._controller_payload()
        return {
            "record_key": borrow.borrow_code,
            "borrow_code": borrow.borrow_code,
            "vehicle_id": borrow.vehicle_id.id if borrow.vehicle_id else False,
            "license_plate": borrow.license_plate,
            "borrower_employee_id": borrow.borrower_code,
            "borrower_name": borrow.borrower_name,
            "state": borrow.state,
            "active": bool(getattr(borrow, "active_for_controller", False)),
            "valid_from": self._dt(borrow.valid_from),
            "valid_to": self._dt(borrow.valid_to),
            "returned_at": self._dt(borrow.returned_at),
            "write_date": self._dt(borrow.write_date),
        }


    @api.model
    def _serialize_edge_server_status(self, edge_server):
        if not edge_server:
            raise UserError(_("Select Edge Server Identity on NSP Sync Authentication before synchronizing Edge Server status."))
        controllers_count = self.env["nsp.controller"].sudo().with_context(active_test=False).search_count([
            ("parent_id", "=", edge_server.id),
            ("node_type", "=", "controller"),
        ])
        devices_count = 0
        if "nsp.device" in self.env.registry.models:
            devices_count = self.env["nsp.device"].sudo().search_count([("controller_id.parent_id", "=", edge_server.id)])
        pending_logs = 0
        failed_logs = 0
        if "nsp.parking.transaction" in self.env.registry.models:
            Log = self.env["nsp.parking.transaction"].sudo()
            if "push_status" in Log._fields:
                pending_logs = Log.search_count([("push_status", "=", "pending")])
                failed_logs = Log.search_count([("push_status", "=", "failed")])
        now = fields.Datetime.now()
        # This job is the Edge Server heartbeat. Report the caller as online
        # and use the current time as the heartbeat timestamp.
        heartbeat = now
        return {
            "record_key": edge_server.controller_id,
            "edge_server_code": edge_server.controller_id,
            "edge_server_name": edge_server.controller_name,
            "branch_code": edge_server.branch_id.code if edge_server.branch_id else False,
            "branch_name": edge_server.branch_id.name if edge_server.branch_id else False,
            "status": "online",
            "connected": True,
            "timestamp": self._dt(heartbeat),
            "last_heartbeat_at": self._dt(heartbeat),
            "last_device_report_at": self._dt(edge_server.last_device_report_at),
            "url": edge_server.url or False,
            "controllers_count": controllers_count,
            "devices_count": devices_count,
            "pending_logs": pending_logs,
            "failed_logs": failed_logs,
            "write_date": self._dt(edge_server.write_date),
        }

    @api.model
    def _serialize_device_status(self, device):
        antennas = []
        for ant in device.antennas_ids:
            antennas.append({
                "antenna_id": ant.antenna_id,
                "antenna_no": ant.antenna_id,
                "status": ant.status,
                "is_active": bool(ant.is_active),
                "power_dbm": ant.power_dbm,
                "return_loss_db": ant.return_loss_db,
                "scan_time": ant.scan_time,
                "q_value": ant.q_value,
                "session": ant.session,
                "last_seen_at": self._dt(getattr(ant, "last_seen", False)) if "last_seen" in ant._fields else self._dt(device.last_seen),
            })
        controller = device.controller_id
        device_type = False
        if "device_type" in device._fields and device.device_type:
            device_type = device.device_type
        elif "device_type_id" in device._fields and device.device_type_id:
            device_type = device.device_type_id.code
        return {
            "record_key": device.serial_number or device.device_code,
            "device_code": device.device_code or device.serial_number,
            "serial_number": device.serial_number,
            "device_name": device.device_name,
            "device_type": device_type,
            "model_number": device.model_number,
            "device_vendor": device.device_vendor,
            "ip_address": device.device_ip,
            "port": device.device_port,
            "status": device.status,
            "connection_status": device.status,
            "last_seen_at": self._dt(device.last_seen),
            "firmware_version": device.firmware_version,
            "controller_code": controller.controller_id if controller else False,
            "controller_name": controller.controller_name if controller else False,
            "controller_last_seen_at": self._dt(controller.timestamp) if controller else False,
            "edge_server_code": controller.parent_id.controller_id if controller and controller.parent_id else False,
            "antennas": antennas,
            "write_date": self._dt(device.write_date),
        }

    @api.model
    def _serialize_measurement(self, event):
        payload = {
            "measurement_uid": event.measurement_uid,
            "controller_code": event.session_id.controller_id.controller_id,
            "serial_number": event.serial_number,
            "antenna_no": int(event.antenna_no),
            "tid": event.tid,
            "read_at": self._dt(event.read_at),
            "_measurement_session_uid": event.session_id.measurement_session_uid,
            "_measurement_run_uid": event.run_id.measurement_run_uid,
        }
        if event.rssi_dbm not in (False, None):
            payload["rssi_dbm"] = event.rssi_dbm
        return payload


    @api.model
    def _serialize_pairing_request(self, pairing):
        if not hasattr(pairing, "sync_payload"):
            raise UserError(_("Controller Pairing model is not ready. Upgrade NSP Gatekeeper first."))
        return pairing.sync_payload()

    def _serialize_records_by_kind(self, kind, since=False, limit=100):
        self.ensure_one()
        limit = max(1, min(int(limit or 100), 1000))
        domain = []
        if since:
            domain.append(("write_date", ">", since))
        if kind == "pairing_request_push":
            edge_server = self.auth_id.edge_server_id
            if not edge_server:
                raise UserError(_("Select Edge Server Identity before synchronizing Controller Pairing Requests."))
            pairing_domain = [
                ("edge_server_id", "=", edge_server.id),
                ("pairing_status", "in", ["pending", "delivered", "cancelled", "expired"]),
            ]
            if since:
                pairing_domain.append(("write_date", ">", since))
            records = self.env["nsp.controller.pairing.request"].sudo().search(
                pairing_domain, order="write_date asc, id asc", limit=limit
            )
            return [self._serialize_pairing_request(record) for record in records]
        if kind == "edge_server_status":
            return [self._serialize_edge_server_status(self.auth_id.edge_server_id)]
        if kind == "device_status":
            edge_server = self.auth_id.edge_server_id
            if not edge_server:
                raise UserError(_("Select Edge Server Identity before synchronizing Device status."))
            controller_ids = self.env["nsp.controller"].sudo().with_context(active_test=False).search([
                ("parent_id", "=", edge_server.id),
                ("node_type", "=", "controller"),
            ]).ids
            device_domain = [("controller_id", "in", controller_ids)]
            if since:
                device_domain.append(("write_date", ">", since))
            records = self.env["nsp.device"].sudo().search(device_domain, order="write_date asc, id asc", limit=limit)
            return [self._serialize_device_status(record) for record in records]
        if kind == "branch":
            if not self._table_exists("nsp_branch") or "nsp.branch" not in self.env.registry.models:
                return []
            records = self.env["nsp.branch"].sudo().search(domain, order="write_date asc, id asc", limit=limit)
            return [self._serialize_branch(r) for r in records]
        if kind == "gate_config":
            records = self.env["nsp.gate"].sudo().search(domain, order="write_date asc, id asc", limit=limit)
            return [self._serialize_gate(r) for r in records]
        if kind == "measurement":
            if "nsp.gate.measurement.event" not in self.env.registry.models:
                return []
            now = fields.Datetime.now()
            event_domain = [
                ("sync_state", "in", ["pending", "failed"]),
                "|", ("next_retry_at", "=", False), ("next_retry_at", "<=", now),
            ]
            first = self.env["nsp.gate.measurement.event"].sudo().search(
                event_domain, order="read_at asc, id asc", limit=1
            )
            if not first:
                return []
            event_domain += [
                ("session_id", "=", first.session_id.id),
                ("run_id", "=", first.run_id.id),
            ]
            records = self.env["nsp.gate.measurement.event"].sudo().search(
                event_domain, order="read_at asc, id asc", limit=limit
            )
            return [self._serialize_measurement(record) for record in records]
        if kind == "user":
            records = self.env["nsp.user"].sudo().with_context(active_test=False).search(domain, order="write_date asc, id asc", limit=limit)
            return [self._serialize_user(r) for r in records]
        if kind == "vehicle":
            records = self.env["nsp.vehicle"].sudo().with_context(active_test=False).search(domain, order="write_date asc, id asc", limit=limit)
            return [self._serialize_vehicle(r) for r in records]
        if kind == "card":
            records = self.env["nsp.rfid.card"].sudo().search(domain, order="write_date asc, id asc", limit=limit)
            return [self._serialize_card(r) for r in records]
        if kind == "parking_transaction":
            records = self.env["nsp.parking.transaction"].sudo().search(domain, order="write_date asc, id asc", limit=limit)
            return [self._serialize_log(r) for r in records]
        if kind == "vehicle_borrow":
            if "nsp.vehicle.borrow.request" not in self.env.registry.models:
                return []
            records = self.env["nsp.vehicle.borrow.request"].sudo().search(domain, order="write_date asc, id asc", limit=limit)
            return [self._serialize_vehicle_borrow(r) for r in records]
        raise UserError(_("Unsupported NSP Sync action kind: %s") % kind)

    # ------------------------------ apply -------------------------------
    @api.model
    def _find_or_create_controller(self, code, name=False):
        code = str(code or "").strip() or "NSP-SYNC-CONTROLLER"
        Controller = self.env["nsp.controller"].sudo()
        rec = Controller.search([("controller_id", "=", code)], limit=1)
        if rec:
            if name and rec.controller_name != name:
                rec.write({"controller_name": name})
            return rec
        return Controller.create({"controller_id": code, "controller_name": name or code, "status": "online", "connected": True})

    @api.model
    def _find_or_create_device(self, controller, item):
        serial = str(item.get("device_serial") or "").strip()
        code = str(item.get("device_code") or serial or "RFID-READER").strip()
        Device = self.env["nsp.device"].sudo()
        device = Device.search([("serial_number", "=", serial)], limit=1) if serial else Device.browse()
        if not device:
            device = Device.search([("device_code", "=", code)], limit=1)
        vals = {"device_code": code, "device_name": item.get("device_name") or code, "device_type": False, "serial_number": serial or code, "controller_id": controller.id if controller else False, "status": "online", "managed": True}
        if item.get("device_type") and hasattr(Device, "_device_type_vals_from_report"):
            vals.update(Device._device_type_vals_from_report(item.get("device_type")))
        if device:
            device.write({k: v for k, v in vals.items() if v not in (False, None, "")})
            return device
        return Device.create(vals)

    @api.model
    def _card(self, tid, card_type):
        tid = str(tid or "").strip()
        if not tid:
            return self.env["nsp.rfid.card"].browse()
        Card = self.env["nsp.rfid.card"].sudo()
        card = Card.search([("tid", "=", tid)], limit=1)
        if card:
            if card.card_type != card_type:
                card.write({"card_type": card_type})
            return card
        return Card.create({"tid": tid, "card_type": card_type})


    @api.model
    def _apply_branch(self, item):
        if not self._table_exists("nsp_branch") or "nsp.branch" not in self.env.registry.models:
            raise UserError(_("Branch model is not ready. Upgrade NSP Gatekeeper before running Branch sync."))
        Branch = self.env["nsp.branch"].sudo()
        code = str(item.get("branch_code") or item.get("code") or item.get("record_key") or item.get("key") or "").strip().upper()
        if not code:
            raise UserError(_("Branch Code is required."))
        branch = Branch.search([("code", "=", code)], limit=1)
        vals = {
            "code": code,
            "name": item.get("branch_name") or item.get("name") or code,
            "timezone": item.get("timezone") or item.get("branch_timezone") or "Asia/Ho_Chi_Minh",
            "status": item.get("status") or "active",
            "note": item.get("note"),
        }
        vals = {k: v for k, v in vals.items() if v not in (None, "")}
        return branch.write(vals) and branch if branch else Branch.create(vals)

    @api.model
    def _apply_card(self, item):
        tid = str(item.get("tid") or item.get("record_key") or "").strip()
        card_type = item.get("card_type") or ("vehicle_card" if item.get("owner_type") == "vehicle" else "user_card" if item.get("owner_type") == "person" else False)
        if card_type not in ("vehicle_card", "user_card"):
            raise UserError(_("card_type must be vehicle_card or user_card."))
        if not tid:
            raise UserError(_("TID is required."))
        Card = self.env["nsp.rfid.card"].sudo()
        card = Card.search([("tid", "=", tid)], limit=1)
        vals = {"tid": tid, "card_type": card_type, "note": item.get("note")}
        vals = {k: v for k, v in vals.items() if v not in (None, "")}
        if card:
            card.write(vals)
            return card
        return Card.create(vals)

    @api.model
    def _apply_user(self, item):
        User = self.env["nsp.user"].sudo().with_context(active_test=False)
        user_code = str(item.get("user_code") or item.get("employee_id") or item.get("employee_code") or item.get("hr_code") or item.get("record_key") or item.get("key") or "").strip()
        name = item.get("name") or item.get("employee_name") or user_code or "User"
        user = User.search([("user_code", "=", user_code)], limit=1) if user_code else User.browse()
        if not user:
            user = User.search([("name", "=", name)], limit=1)
        vals = {"name": name}
        if user_code:
            vals["user_code"] = user_code
        if "active" in User._fields and item.get("active") is not None:
            vals["active"] = bool(item.get("active"))
        if item.get("email") or item.get("work_email"):
            vals["email"] = item.get("email") or item.get("work_email")
        if item.get("phone") or item.get("work_phone"):
            vals["phone"] = item.get("phone") or item.get("work_phone")
        if item.get("pin"):
            vals["pin"] = item.get("pin")
        user = user.write(vals) and user if user else User.create(vals)
        card_items = list(item.get("cards") or [])
        for tid in item.get("user_tids") or []:
            if tid and not any(c.get("tid") == tid for c in card_items if isinstance(c, dict)):
                card_items.append({"tid": tid, "state": "active"})
        for card_item in card_items:
            tid = card_item.get("tid") if isinstance(card_item, dict) else card_item
            card = self._card(tid, "user_card")
            if not card:
                continue
            Line = self.env["nsp.user.card"].sudo()
            line = Line.search([("user_id", "=", user.id), ("card_id", "=", card.id)], limit=1)
            vals = {"user_id": user.id, "card_id": card.id, "state": (card_item.get("state") if isinstance(card_item, dict) else "active") or "active", "note": card_item.get("note") if isinstance(card_item, dict) else False}
            if line:
                line.write(vals)
            else:
                Line.create(vals)
        return user

    @api.model
    def _get_or_create_named(self, model_name, name):
        name = str(name or "").strip()
        if not name:
            return self.env[model_name].browse()
        Model = self.env[model_name].sudo()
        rec = Model.search([("name", "=", name)], limit=1)
        return rec or Model.create({"name": name})

    @api.model
    def _apply_vehicle(self, item):
        Vehicle = self.env["nsp.vehicle"].sudo().with_context(active_test=False)
        plate = str(item.get("license_plate") or item.get("record_key") or item.get("key") or "").strip()
        if not plate:
            raise UserError(_("Vehicle license_plate is required."))
        owner_code = item.get("owner_user_code") or item.get("owner_hr_code") or item.get("owner_employee_id") or ("OWNER-%s" % plate)
        owner = self._apply_user({"user_code": owner_code, "employee_id": owner_code, "name": item.get("owner_name") or owner_code, "active": True, "cards": []})
        vehicle = Vehicle.search([("license_plate", "=", plate)], limit=1)
        vals = {"license_plate": plate, "owner_id": owner.id, "state": item.get("state") or "approved"}
        vehicle_type_name = item.get("vehicle_type_name") or item.get("vehicle_type")
        if vehicle_type_name:
            vals["vehicle_type_id"] = self._get_or_create_named("nsp.vehicle.type", vehicle_type_name).id
        if item.get("brand_name") or item.get("brand"):
            vals["brand_id"] = self._get_or_create_named("nsp.vehicle.brand", item.get("brand_name") or item.get("brand")).id
        if item.get("model_name") or item.get("model"):
            vals["model_id"] = self._get_or_create_named("nsp.vehicle.model", item.get("model_name") or item.get("model")).id
        if item.get("color_name") or item.get("color"):
            vals["color_id"] = self._get_or_create_named("nsp.vehicle.color", item.get("color_name") or item.get("color")).id
        vehicle = vehicle.write({k: v for k, v in vals.items() if v not in (None, "")}) and vehicle if vehicle else Vehicle.create(vals)
        card_items = list(item.get("cards") or [])
        for tid in item.get("vehicle_tids") or []:
            if tid and not any(c.get("tid") == tid for c in card_items if isinstance(c, dict)):
                card_items.append({"tid": tid, "state": "active"})
        if item.get("tid") and not any(c.get("tid") == item.get("tid") for c in card_items if isinstance(c, dict)):
            card_items.append({"tid": item.get("tid"), "state": "active"})
        for card_item in card_items:
            tid = card_item.get("tid") if isinstance(card_item, dict) else card_item
            card = self._card(tid, "vehicle_card")
            if not card:
                continue
            Line = self.env["nsp.vehicle.card"].sudo()
            line = Line.search([("vehicle_id", "=", vehicle.id), ("card_id", "=", card.id)], limit=1)
            vals = {"vehicle_id": vehicle.id, "card_id": card.id, "state": (card_item.get("state") if isinstance(card_item, dict) else "active") or "active", "note": card_item.get("note") if isinstance(card_item, dict) else False}
            if line:
                line.write(vals)
            else:
                Line.create(vals)
        return vehicle

    @api.model
    def _apply_gate(self, item):
        if not self._table_exists("nsp_branch") or "nsp.branch" not in self.env.registry.models:
            raise UserError(_("Branch model is not ready. Upgrade NSP Gatekeeper before syncing Gates."))
        Branch = self.env["nsp.branch"].sudo()
        branch_code = str(item.get("branch_code") or "DEFAULT").strip().upper()
        branch = Branch.search([("code", "=", branch_code)], limit=1)
        branch_tz = item.get("branch_timezone") or item.get("timezone")
        if not branch:
            branch = Branch.create({
                "code": branch_code,
                "name": item.get("branch_name") or branch_code,
                "status": "active",
                "timezone": branch_tz or "Asia/Ho_Chi_Minh",
            })
        else:
            branch_vals = {}
            if item.get("branch_name"):
                branch_vals["name"] = item.get("branch_name")
            if branch_tz:
                branch_vals["timezone"] = branch_tz
            if branch_vals:
                branch.write(branch_vals)

        Gate = self.env["nsp.gate"].sudo()
        code = str(item.get("gate_code") or item.get("record_key") or item.get("key") or "").strip().upper()
        if not code:
            raise UserError(_("Gate code is required."))
        gate = Gate.search([("code", "=", code)], limit=1)

        controller_codes = []
        controller_names = {}

        def _add_controller_ref(code, name=False):
            code = str(code or "").strip()
            if not code:
                return
            if code not in controller_codes:
                controller_codes.append(code)
            if name and not controller_names.get(code):
                controller_names[code] = name

        for code in item.get("controller_codes") or []:
            _add_controller_ref(code)
        for controller_item in item.get("controllers") or []:
            if isinstance(controller_item, dict):
                _add_controller_ref(
                    controller_item.get("controller_id") or controller_item.get("controller_code"),
                    controller_item.get("controller_name") or controller_item.get("name"),
                )
            else:
                _add_controller_ref(controller_item)
        _add_controller_ref(item.get("controller_code"), item.get("controller_name"))

        # Lane antenna rules may contain the only controller reference when the
        # source server allows Cloud-side Gate/Device/Antenna pre-configuration.
        for lane_item in item.get("lanes") or []:
            for rule in lane_item.get("antenna_rules") or []:
                if isinstance(rule, dict):
                    _add_controller_ref(
                        rule.get("controller_id") or rule.get("controller_code"),
                        rule.get("controller_name"),
                    )

        controllers = self.env["nsp.controller"].sudo().browse()
        for controller_code in controller_codes:
            controller = self._find_or_create_controller(controller_code, controller_names.get(controller_code) or item.get("controller_name"))
            controllers |= controller

        vals = {
            "code": code,
            "name": item.get("gate_name") or code,
            "branch_id": branch.id,
            "gate_status": item.get("gate_status") or "active",
            "operation_state": item.get("operation_state") or "draft",
            "detection_window_ms": int(item.get("detection_window_ms") or 1500),
            "sequence_required": bool(item.get("sequence_required", True)),
            "entry_requires_user_tid": bool(item.get("entry_requires_user_tid")),
            "exit_requires_user_tid": bool(item.get("exit_requires_user_tid", True)),
        }
        if controllers:
            vals["controller_ids"] = [(6, 0, controllers.ids)]
        gate = gate.write(vals) and gate if gate else Gate.create(vals)

        lane_items = list(item.get("lanes") or [])
        if lane_items:
            Lane = self.env["nsp.gate.lane"].sudo()
            Rule = self.env["nsp.gate.lane.antenna.mapping"].sudo()
            for lane_item in lane_items:
                lane_code = str(lane_item.get("lane_code") or lane_item.get("code") or "").strip().upper()
                if not lane_code:
                    continue
                lane = Lane.search([("gate_id", "=", gate.id), ("code", "=", lane_code)], limit=1)
                lane_vals = {
                    "gate_id": gate.id,
                    "code": lane_code,
                    "name": lane_item.get("lane_name") or lane_item.get("name") or lane_code,
                    "lane_no": int(lane_item.get("lane_no") or 1),
                    "direction": lane_item.get("direction") if lane_item.get("direction") in ("entry", "exit", "both") else "entry",
                    "sequence": int(lane_item.get("sequence") or 10),
                    "required_antenna_count": int(lane_item.get("required_antenna_count") or 1),
                    "active": bool(lane_item.get("active", True)),
                }
                lane = lane.write(lane_vals) and lane if lane else Lane.create(lane_vals)
                if lane_item.get("antenna_rules"):
                    lane.antenna_rule_ids.unlink()
                    for rule in lane_item.get("antenna_rules") or []:
                        controller_code = rule.get("controller_id") or rule.get("controller_code") or (controller_codes[0] if controller_codes else False)
                        controller = self._find_or_create_controller(controller_code, rule.get("controller_name") or controller_names.get(controller_code)) if controller_code else (controllers[:1] if controllers else self.env["nsp.controller"].browse())
                        if controller and controller not in gate.controller_ids:
                            # Keep Gate configuration consistent before creating the
                            # Lane/Antenna mapping. The mapping constraint requires
                            # the physical antenna controller to be one of the Gate
                            # Controllers; Cloud-side Gate Config Sync may infer this
                            # controller only from the antenna rule payload.
                            gate.write({"controller_ids": [(4, controller.id)]})
                            controllers |= controller
                        device = self._find_or_create_device(controller, rule) if controller else self.env["nsp.device"].browse()
                        ant_no = int(rule.get("antenna_id") or 1)
                        antenna = self.env["nsp.device.antenna"].sudo().search([("device_id", "=", device.id), ("antenna_id", "=", ant_no)], limit=1)
                        if not antenna and device:
                            antenna = self.env["nsp.device.antenna"].sudo().create({"device_id": device.id, "antenna_id": ant_no, "is_active": True})
                        if antenna:
                            Rule.create({
                                "gate_id": gate.id,
                                "lane_id": lane.id,
                                "antenna_ref_id": antenna.id,
                                "antenna_direction": rule.get("antenna_direction") or "auto",
                                "tag_role": rule.get("tag_role") if rule.get("tag_role") in ("vehicle_tid", "user_tid", "both") else "vehicle_tid",
                                "sequence_order": int(rule.get("sequence_order") or 10),
                                "required": bool(rule.get("required", True)),
                                "is_active": bool(rule.get("is_active", True)),
                            })
        preserve = {field: item.get(field) for field in ("config_revision", "config_hash", "config_state", "applied_config_revision", "applied_config_hash") if item.get(field) not in (None, "")}
        if preserve:
            gate.write(preserve)
        return gate

    @api.model
    def _apply_log(self, item):
        controller = self._find_or_create_controller(item.get("controller_code"), item.get("controller_name"))
        Gate = self.env["nsp.gate"].sudo()
        gate = Gate.search([("code", "=", item.get("gate_code"))], limit=1) if item.get("gate_code") else Gate.browse()
        vehicle = self.env["nsp.vehicle"].sudo().search([("license_plate", "=", item.get("license_plate"))], limit=1) if item.get("license_plate") else self.env["nsp.vehicle"].browse()
        Log = self.env["nsp.parking.transaction"].sudo()
        uid = item.get("transaction_uid") or item.get("local_id") or item.get("record_key")
        if not uid:
            uid = "%s:%s:%s:%s" % (controller.controller_id, item.get("gate_code") or "", item.get("time_entered") or "", item.get("vehicle_tid") or "")
        lane = self.env["nsp.gate.lane"].sudo().search([("gate_id", "=", gate.id), ("code", "=", str(item.get("lane_code") or "").strip().upper())], limit=1) if gate and item.get("lane_code") else self.env["nsp.gate.lane"].browse()
        vals = {
            "controller_id": controller.id,
            "gate_id": gate.id if gate else False,
            "branch_id": gate.branch_id.id if gate and gate.branch_id else False,
            "gate_code": item.get("gate_code"),
            "lane_id": lane.id if lane else False,
            "lane_code": item.get("lane_code"),
            "transaction_uid": uid,
            "controller_local_id": item.get("controller_local_id") or item.get("local_id"),
            "time_entered": item.get("time_entered") or fields.Datetime.now(),
            "direction": item.get("direction") or "entry",
            "status": item.get("status") or "allowed",
            "error_message": item.get("error_message"),
            "vehicle_id": vehicle.id if vehicle else False,
            "license_plate": item.get("license_plate"),
            "vehicle_tid": item.get("vehicle_tid"),
            "user_tid": item.get("user_tid"),
            "device_serial": item.get("device_serial"),
            "antenna_id": int(item.get("antenna_id") or 0) or False,
            "antenna_sequence": item.get("antenna_sequence"),
            "effective_direction": item.get("effective_direction"),
            "config_revision": item.get("config_revision") or False,
        }
        existing = Log.search([("transaction_uid", "=", uid)], limit=1)
        if existing:
            existing.write(vals)
            return existing
        return Log.create(vals)

    @api.model
    def _apply_vehicle_borrow(self, item):
        if "nsp.vehicle.borrow.request" not in self.env.registry.models:
            raise UserError(_("Vehicle Borrow model is not ready. Upgrade NSP Vehicle before syncing borrow requests."))
        Borrow = self.env["nsp.vehicle.borrow.request"].sudo()
        code = str(item.get("borrow_code") or item.get("record_key") or item.get("borrow_id") or "").strip()
        if not code:
            raise UserError(_("Borrow code is required."))
        borrow = Borrow.search([("borrow_code", "=", code)], limit=1)
        Vehicle = self.env["nsp.vehicle"].sudo()
        vehicle = Vehicle.browse(int(item.get("vehicle_id"))) if str(item.get("vehicle_id") or "").isdigit() else Vehicle.browse()
        if not vehicle and item.get("license_plate"):
            vehicle = Vehicle.search([("license_plate", "=", item.get("license_plate"))], limit=1)
        borrower = self.env["nsp.user"].sudo().search([("user_code", "=", item.get("borrower_employee_id") or item.get("borrower_user_code") or item.get("borrower_user_id"))], limit=1)
        if not vehicle or not borrower:
            raise UserError(_("Vehicle and borrower are required for borrow sync."))
        vals = {
            "vehicle_id": vehicle.id,
            "borrower_id": borrower.id,
            "valid_from": item.get("valid_from") or fields.Datetime.now(),
            "valid_to": item.get("valid_to") or fields.Datetime.now(),
            "state": item.get("state") or "approved",
            "returned_at": item.get("returned_at") or False,
        }
        if borrow:
            borrow.write(vals)
            return borrow
        vals["borrow_code"] = code
        return Borrow.create(vals)

    @api.model
    def _apply_items(self, kind, items):
        results, failed = [], []
        Record = self.env["nsp.sync.record"].sudo()
        if kind == "pairing_decision":
            edge_server = self.auth_id.edge_server_id
            if not edge_server:
                raise UserError(_("Select Edge Server Identity before pulling Controller Pairing Decisions."))
            Pairing = self.env["nsp.controller.pairing.request"].sudo()
            for idx, item in enumerate(items if isinstance(items, list) else []):
                key = str((item or {}).get("pairing_request_uid") or "").strip() if isinstance(item, dict) else ""
                try:
                    if not key:
                        raise UserError(_("pairing_request_uid is required."))
                    pairing = Pairing.search([
                        ("pairing_request_uid", "=", key),
                        ("edge_server_id", "=", edge_server.id),
                    ], limit=1)
                    if not pairing:
                        raise UserError(_("Pairing request was not found on this Edge Server."))
                    pairing.apply_cloud_decision(item)
                    Record.mark_result(
                        sync_job=self,
                        action_code=self.sync_action_code, action_name=self.sync_action_name,
                        route_suffix=self.route_suffix, record=pairing, record_key=key,
                        status="synced", message="Pairing decision applied by NSP Sync job.",
                        operation="pull",
                    )
                    results.append({
                        "index": idx, "record_key": key,
                        "record_model": pairing._name, "record_id": pairing.id,
                        "success": True,
                    })
                except Exception as exc:
                    failed.append({"index": idx, "record_key": key, "error": str(exc)})
            return results, failed
        for idx, item in enumerate(items if isinstance(items, list) else []):
            try:
                if kind == "branch":
                    rec = self._apply_branch(item); key = rec.code
                elif kind == "card":
                    rec = self._apply_card(item); key = rec.tid
                elif kind == "gate_config":
                    rec = self._apply_gate(item); key = rec.code
                elif kind == "user":
                    rec = self._apply_user(item); key = self._user_key(rec)
                elif kind == "vehicle":
                    rec = self._apply_vehicle(item); key = rec.license_plate
                elif kind == "parking_transaction":
                    rec = self._apply_log(item); key = rec.transaction_uid
                elif kind == "measurement":
                    rec = self.env["nsp.gatekeeper.api.service"].sudo()._apply_measurement_sync_item(item); key = rec.measurement_uid or str(rec.id)
                elif kind == "vehicle_borrow":
                    rec = self._apply_vehicle_borrow(item); key = getattr(rec, "sync_record_key", False) or rec.borrow_code
                else:
                    raise UserError(_("Unsupported NSP Sync action kind: %s") % kind)
                Record.mark_result(sync_job=self, action_code=self.sync_action_code, action_name=self.sync_action_name, route_suffix=self.route_suffix, record=rec, record_key=key, status="synced", message="Applied by NSP Sync job.", operation="pull")
                results.append({"index": idx, "record_key": key, "record_model": rec._name, "record_id": rec.id, "success": True})
            except Exception as exc:
                failed.append({"index": idx, "record_key": item.get("record_key") if isinstance(item, dict) else False, "error": str(exc)})
        return results, failed

    # ---------------------- Core API payload adapters --------------------
    def _response_payload(self, data):
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            payload = dict(data.get("data") or {})
            for key, value in data.items():
                payload.setdefault(key, value)
            return payload
        return data if isinstance(data, dict) else {}

    def _items_from_response(self, data):
        payload = self._response_payload(data)
        kind = self._action_kind()
        keys_by_kind = {"edge_server_status": ("edge_server", "items"), "device_status": ("devices", "items"), "branch": ("branches", "items"), "card": ("cards", "items"), "user": ("users", "employees", "items"), "vehicle": ("vehicles", "items"), "gate_config": ("gates", "items"), "parking_transaction": ("transactions", "items"), "vehicle_borrow": ("borrows", "vehicle_borrows", "items"), "measurement": ("measurements", "items"), "pairing_decision": ("items",)}
        for key in keys_by_kind.get(kind, ("items",)):
            items = payload.get(key)
            if isinstance(items, dict):
                return [items]
            if isinstance(items, list):
                return items
        return []

    def _build_pull_payload(self):
        self.ensure_one()
        edge_server = self.auth_id.edge_server_id
        if not edge_server:
            raise UserError(_("Select Edge Server Identity before pulling Cloud data."))
        return {
            "edge_server_code": edge_server.controller_id,
            "sync_cursor": self.pull_cursor or None,
            "limit": max(1, min(int(self.batch_size or 100), 1000)),
        }

    def _record_key_from_item(self, item):
        if not isinstance(item, dict):
            return False
        return item.get("record_key") or item.get("pairing_request_uid") or item.get("controller_code") or item.get("edge_server_code") or item.get("serial_number") or item.get("device_code") or item.get("key") or item.get("branch_code") or item.get("user_code") or item.get("employee_id") or item.get("license_plate") or item.get("borrow_code") or item.get("gate_code") or item.get("measurement_uid") or item.get("local_id") or item.get("transaction_uid") or False

    def _normalize_items_for_remote(self, items):
        route = (self.route_suffix or "").strip().strip("/")
        if route in ("parking/logs/push", "parking-transactions/sync"):
            normalized = []
            for item in items:
                payload = dict(item or {})
                payload.setdefault("local_id", payload.get("controller_local_id") or payload.get("transaction_uid") or payload.get("record_key"))
                payload.setdefault("time_entered", payload.get("check_time") or payload.get("event_time"))
                normalized.append(payload)
            return normalized
        return items

    def _build_push_payload(self, items):
        self.ensure_one()
        edge_server = self.auth_id.edge_server_id
        if not edge_server:
            raise UserError(_("Select Edge Server Identity before pushing Cloud data."))
        items = self._normalize_items_for_remote(items)
        route = (self.route_suffix or "").strip().strip("/")
        edge_server_code = edge_server.controller_id
        if route == "edge-server/status":
            payload = dict(items[0] if items else {})
            payload["edge_server_code"] = edge_server_code
            return payload
        if route == "devices-status/sync":
            return {"edge_server_code": edge_server_code, "devices": items}
        if route == "parking-transactions/sync":
            return {"edge_server_code": edge_server_code, "items": items}
        if route == "gate-measurement/sync":
            if not items:
                return {"edge_server_code": edge_server_code, "measurements": []}
            session_uid = items[0].get("_measurement_session_uid")
            run_uid = items[0].get("_measurement_run_uid")
            measurements = [
                {key: value for key, value in item.items() if not key.startswith("_")}
                for item in items
            ]
            return {
                "edge_server_code": edge_server_code,
                "measurement_session_uid": session_uid,
                "measurement_run_uid": run_uid,
                "measurements": measurements,
            }
        if route == "controller-pairing-requests/sync":
            return {"edge_server_code": edge_server_code, "requests": items}
        return {"edge_server_code": edge_server_code, "items": items}

    # --------------------------- execution ------------------------------
    def run_push_once(self):
        self.ensure_one()
        kind = self._action_kind()
        items = self._serialize_records_by_kind(kind, since=self.last_push_at, limit=self.batch_size)
        if not items:
            self.write({"last_push_at": fields.Datetime.now(), "last_message": "No changed records to push."})
            return {"pushed": 0, "failed": 0, "message": "No changed records to push."}
        Record = self.env["nsp.sync.record"].sudo()
        for item in items:
            key = self._record_key_from_item(item)
            if key:
                Record.mark_pending(sync_job=self, action_code=self.sync_action_code, action_name=self.sync_action_name, route_suffix=self.route_suffix, record_key=key, message="Waiting for remote API acceptance.", operation="push")
        payload = self._build_push_payload(items)
        response = self._post_remote(self.sync_action_id, payload, timeout=120)
        data = self._json_or_error(response)
        response_results = data.get("results") if isinstance(data.get("results"), list) else []
        rejected = []
        result_by_key = {}
        for result in response_results:
            if not isinstance(result, dict):
                continue
            key = result.get("record_key") or result.get("key")
            if key:
                result_by_key[str(key)] = result
            if result.get("status") == "rejected":
                rejected.append(result)
        for item in items:
            key = self._record_key_from_item(item)
            if not key:
                continue
            remote_result = result_by_key.get(str(key), {})
            remote_status = remote_result.get("status")
            is_failed = remote_status == "rejected"
            Record.mark_result(
                sync_job=self, action_code=self.sync_action_code,
                action_name=self.sync_action_name, route_suffix=self.route_suffix,
                record_key=key, status="failed" if is_failed else "synced",
                message=(remote_result.get("message") or remote_result.get("error") or
                         ("Remote API rejected the record." if is_failed else "Remote API accepted.")),
                operation="push",
            )
            if kind == "measurement":
                event = self.env["nsp.gate.measurement.event"].sudo().search([
                    ("measurement_uid", "=", key),
                ], limit=1)
                if event:
                    if is_failed:
                        retry_count = event.retry_count + 1
                        delay = min(300, max(5, 2 ** min(retry_count, 8)))
                        event.write({
                            "sync_state": "failed",
                            "retry_count": retry_count,
                            "next_retry_at": fields.Datetime.now() + timedelta(seconds=delay),
                        })
                    else:
                        event.write({
                            "sync_state": "synced",
                            "last_sync_at": fields.Datetime.now(),
                            "next_retry_at": False,
                        })
        if rejected or int(data.get("failed") or 0) > 0:
            raise UserError(json.dumps(rejected or response_results or data, ensure_ascii=False))
        self.write({"last_push_at": fields.Datetime.now(), "last_message": "Pushed %s records via %s." % (len(items), self.route_suffix)})
        return {"pushed": len(items), "failed": 0, "message": "Pushed %s records." % len(items)}

    def run_pull_once(self):
        self.ensure_one()
        payload = self._build_pull_payload()
        response = self._post_remote(self.sync_action_id, payload, timeout=120)
        data = self._json_or_error(response)
        payload_data = self._response_payload(data)
        items = self._items_from_response(data)
        next_cursor = payload_data.get("next_sync_cursor") or self.pull_cursor or False
        if not items:
            self.write({
                "pull_cursor": next_cursor,
                "last_pull_at": fields.Datetime.now(),
                "last_message": "No changed records to pull from %s." % self.route_suffix,
            })
            return {"pulled": 0, "failed": 0, "message": "No changed records to pull."}
        kind = self._action_kind()
        results, failed = self._apply_items(kind, items)
        if failed:
            raise UserError(json.dumps(failed, ensure_ascii=False))
        self.write({
            "pull_cursor": next_cursor,
            "last_pull_at": fields.Datetime.now(),
            "last_message": "Pulled %s records via %s." % (len(results), self.route_suffix),
        })
        return {"pulled": len(results), "failed": 0, "message": "Pulled %s records." % len(results)}

    def run_once(self):
        for rec in self:
            messages = []
            if not rec.active:
                rec.write({"status": "disabled", "last_message": "Sync job disabled."})
                continue
            rec.write({"status": "running", "last_message": False})
            try:
                if rec.direction == "pull":
                    messages.append(rec.run_pull_once().get("message"))
                elif rec.direction == "push":
                    messages.append(rec.run_push_once().get("message"))
                rec.write({"status": "success", "last_message": " ".join([m for m in messages if m]) or "Done."})
            except Exception as exc:
                rec.write({"status": "failed", "last_message": str(exc)})
                _logger.exception("NSP Sync job failed: %s", rec.display_name)
            finally:
                rec._schedule_next()
        return True

    def action_run_now(self):
        self.run_once()
        return {"type": "ir.actions.client", "tag": "display_notification", "params": {"title": _("NSP Sync"), "message": _("Sync job run completed. Check Status / Last Message."), "type": "success", "sticky": False}}

    @api.model
    def run_due_jobs(self):
        now = fields.Datetime.now()
        jobs = self.sudo().search(["&", ("active", "=", True), "|", ("next_run_at", "=", False), ("next_run_at", "<=", now)], order="sequence, id")
        if jobs:
            jobs.run_once()
        return len(jobs)

    @api.model
    def cron_run_job_loop(self):
        deadline = time.time() + 55
        count = 0
        while time.time() < deadline:
            count += self.run_due_jobs()
            time.sleep(1)
        return count
