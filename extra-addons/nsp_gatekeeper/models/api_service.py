# -*- coding: utf-8 -*-
"""NSP Gatekeeper Core API service endpoints.

All runtime controller-facing APIs are exposed through T4 Core API
Action Endpoints instead of direct @http.route aliases.
"""

import base64
import json
import hashlib
import logging
from datetime import datetime, timezone

from psycopg2 import IntegrityError

from odoo import api, fields, models, SUPERUSER_ID
from odoo.http import request

from odoo.addons.t4_coreapi.utils import endpoint, get_params, get_body

_logger = logging.getLogger(__name__)

class NspGatekeeperApiService(models.AbstractModel):
    _name = "nsp.gatekeeper.api.service"
    _description = "NSP Gatekeeper API Service"

    # ------------------------------------------------------------------
    # Core API response helpers
    # ------------------------------------------------------------------
    @api.model
    def _ok(self, payload=None, message="OK", status_code=200, **extra):
        """Return the canonical Core API success envelope.

        T4 Core API owns the HTTP transport wrapper. The payload below is the
        integration contract exposed to Postman/clients: success plus either
        business data or batch counters/results. Legacy ``ok`` is intentionally
        not emitted.
        """
        data = {"success": True}
        if isinstance(payload, dict):
            data.update(payload)
        elif payload is not None:
            data["data"] = payload
        data.update(extra)
        return {"status_code": status_code, "message": message, "data": data}

    @api.model
    def _error(self, message, status_code=400, error_code="invalid_payload", details=None, **extra):
        """Return the canonical validation/authentication error envelope."""
        detail_values = dict(details or {})
        detail_values.update(extra)
        data = {
            "success": False,
            "error_code": str(error_code or "invalid_payload"),
            "message": str(message or "Request failed"),
            "details": detail_values,
        }
        return {"status_code": status_code, "message": data["message"], "data": data}

    @api.model
    def _payload(self):
        try:
            body = get_body(self) or {}
        except Exception:
            body = {}
        return body if isinstance(body, dict) else {}

    @api.model
    def _params(self):
        try:
            params = get_params(self) or {}
        except Exception:
            params = {}
        return params if isinstance(params, dict) else {}

    # ------------------------------------------------------------------
    # Controller authentication and common conversion helpers
    # ------------------------------------------------------------------
    @api.model
    def _controller_code_from_data(self, data=None):
        """Read the only supported Controller integration identifier.

        Internal database IDs and historical aliases are deliberately rejected.
        """
        data = data or {}
        headers = request.httprequest.headers
        return str(headers.get("X-Controller-Code") or data.get("controller_code") or "").strip()

    @api.model
    def _application_from_context(self):
        app_id = self.env.context.get("core_api_application_id")
        if not app_id:
            return self.env["core.api.application"].sudo().browse()
        return self.env["core.api.application"].sudo().browse(app_id).exists()

    @api.model
    def _auth_controller(self, data=None):
        """Resolve a runtime Controller by shared Application + controller_code.

        NSP can use one shared Application for all Controllers. Therefore the
        Core API Application authenticates the caller class, while the concrete
        Controller identity must be supplied in payload/header.
        """
        data = data or self._payload()
        Controller = self.env["nsp.controller"].sudo().with_context(active_test=False)
        app = self._application_from_context()
        if not app:
            return None, self._error("Core API Application authentication is required", 401, error_code="invalid_token")
        service_code = (app.service_code or "").strip()
        if not service_code:
            return None, self._error("Core API Application has no service_code", 401, error_code="invalid_client")

        controller_code = self._controller_code_from_data(data)
        if not controller_code:
            return None, self._error(
                "controller_code is required",
                400,
                error_code="missing_controller_code",
                details={"field": "controller_code"},
            )
        controller = Controller.search([("controller_id", "=", controller_code)], limit=1)
        if not controller:
            return None, self._error(
                "Controller was not found", 404, error_code="controller_not_found",
                details={"controller_code": controller_code},
            )

        # T4 Core API authenticates and authorizes the route. Controller Code
        # resolves the concrete runtime Controller; no Core API Application is stored on NSP nodes.
        if not controller.active or controller.status in ("revoked", "block"):
            return None, self._error(
                "Controller is blocked or revoked", 403, error_code="route_not_allowed",
                details={"controller_code": controller.controller_id},
            )

        try:
            request.update_env(user=SUPERUSER_ID)
            controller = request.env["nsp.controller"].sudo().browse(controller.id)
        except Exception:
            controller = self.env["nsp.controller"].sudo().browse(controller.id)

        controller.write({
            "timestamp": fields.Datetime.now(),
            "status": "online",
        })
        return controller, None

    @api.model
    def _auth_sync_application(self, data=None):
        """Authorize NSP Sync/read-sync endpoints by Core API Application only.

        These endpoints are Odoo-to-Odoo / external cache-sync APIs. They are
        not controller runtime APIs, so they must not resolve, create, block or
        revoke nsp.controller records. A valid Core API token + route permission
        is enough; route authorization remains owned by t4_coreapi.
        """
        app = self._application_from_context()
        if not app:
            return app, "none", self._error("Core API Application authentication is required", 401)
        try:
            request.update_env(user=SUPERUSER_ID)
        except Exception:
            pass
        return app.sudo(), "core_api", None

    @api.model
    def _auth_edge_server_sync(self, data=None):
        data = data or self._payload()
        application, actor_kind, error = self._auth_sync_application(data)
        if error:
            return application, actor_kind, self.env["nsp.edge.server"].browse(), error
        edge_server, node_error = self._edge_server_for_sync_application(application, data)
        return application, actor_kind, edge_server, node_error

    @api.model
    def _safe_datetime_value(self, value, default_now=False):
        if not value:
            return fields.Datetime.now() if default_now else False
        text = str(value).strip()
        if not text:
            return fields.Datetime.now() if default_now else False
        try:
            normalized = text.replace("Z", "+00:00")
            parsed = datetime.fromisoformat(normalized)
        except Exception:
            try:
                parsed = fields.Datetime.to_datetime(text)
            except Exception:
                parsed = False
        if not parsed:
            return fields.Datetime.now() if default_now else False
        if parsed.tzinfo:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        else:
            parsed = parsed.replace(tzinfo=None)
        return fields.Datetime.to_string(parsed)

    @api.model
    def _float_or_false(self, value):
        try:
            return False if value in (None, "") else float(value)
        except Exception:
            return False

    @api.model
    def _safe_positive_int(self, value, default=1):
        try:
            parsed = int(value)
            return parsed if parsed > 0 else default
        except Exception:
            return default

    @api.model
    def _employee_code_fields(self):
        User = self.env["nsp.user"].sudo()
        preferred = ["user_code"]
        return [fname for fname in preferred if fname in User._fields]

    @api.model
    def _employee_code(self, user):
        for fname in self._employee_code_fields():
            value = str(user[fname] or "").strip()
            if value:
                return value
        return ""

    @api.model
    def _employee_pin_fields(self):
        User = self.env["nsp.user"].sudo()
        preferred = ["pin"]
        return [fname for fname in preferred if fname in User._fields]

    @api.model
    def _employee_pin(self, user):
        for fname in self._employee_pin_fields():
            value = str(user[fname] or "").strip()
            if value:
                return value
        return ""

    @api.model
    def _find_employee_by_api_id(self, user_id):
        raw = str(user_id or "").strip()
        User = self.env["nsp.user"].sudo().with_context(active_test=False)
        if not raw:
            return User.browse()
        for fname in self._employee_code_fields():
            user = User.search([(fname, "=", raw)], limit=1)
            if user:
                return user
        return User.browse()

    # ------------------------------------------------------------------
    # Runtime Core API endpoints
    # ------------------------------------------------------------------
    @endpoint("NSP Gatekeeper Health", route_suffix="health", methods="GET,POST", code="nsp_gatekeeper_health")
    def api_health(self):
        return self._ok({
            "service": "nsp_gatekeeper",
            "status": "running",
            "server_time": self._iso_datetime(fields.Datetime.now()),
        }, message="NSP Gatekeeper is running.")

    @endpoint("NSP Gatekeeper Heartbeat", route_suffix="heartbeat", methods="POST", code="nsp_gatekeeper_heartbeat")
    def api_controller_heartbeat(self):
        data = self._payload()
        controller, error = self._auth_controller(data)
        if error:
            return error
        now = fields.Datetime.now()
        vals = {"timestamp": now, "status": "online"}
        controller.write(vals)
        return self._ok({
            "controller_code": controller.controller_id,
            "current_status": "online",
            "last_seen_at": self._iso_datetime(now),
            "managed_device_count": len(controller.device_ids),
        }, message="Heartbeat accepted.")

    @api.model
    def _edge_server_code_from_payload(self, data=None):
        data = data or {}
        headers = request.httprequest.headers
        return str(headers.get("X-Edge-Server-Code") or data.get("edge_server_code") or "").strip()

    @api.model
    def _edge_server_for_sync_application(self, application, data=None):
        """Resolve a predeclared Edge Server by its assigned code.

        Core API authentication and route permission are owned by t4_coreapi.
        NSP Edge Servers do not store or manage Core API Application records.
        """
        EdgeServer = self.env["nsp.edge.server"].sudo().with_context(active_test=False)
        edge_server_code = self._edge_server_code_from_payload(data or {})
        if not edge_server_code:
            return EdgeServer.browse(), self._error(
                "edge_server_code is required", 400,
                error_code="missing_edge_server_code",
                details={"field": "edge_server_code"},
            )
        edge_server = EdgeServer.search([("edge_server_code", "=", edge_server_code.upper())], limit=1)
        if not edge_server:
            return EdgeServer.browse(), self._error(
                "Edge Server was not found", 404, error_code="record_not_found",
                details={"edge_server_code": edge_server_code},
            )
        if not edge_server.active or edge_server.status in ("block", "revoked"):
            return EdgeServer.browse(), self._error(
                "Edge Server is blocked or revoked", 403, error_code="route_not_allowed",
                details={"edge_server_code": edge_server_code},
            )
        return edge_server, None

    @api.model
    def _update_edge_server_status_from_payload(self, parent, data):
        if not parent:
            return parent
        current_status = str(data.get("current_status") or "online").strip().lower()
        if current_status not in ("online", "offline", "error", "block", "revoked"):
            raise ValueError("invalid_payload")
        last_seen_at = self._safe_datetime_value(data.get("last_seen_at"), default_now=False) or fields.Datetime.now()
        vals = {
            "timestamp": last_seen_at,
            "status": current_status,
        }
        parent.write(vals)
        return parent

    @api.model
    def _device_status_payloads_from_data(self, data):
        items = data.get("devices") if "devices" in data else data.get("items")
        if isinstance(items, dict):
            items = [items]
        return items if isinstance(items, list) else []

    @api.model
    def _apply_device_status(self, controller, item):
        if not isinstance(item, dict):
            raise ValueError("invalid_payload")
        serial_number = str(item.get("serial_number") or "").strip()
        if not serial_number:
            raise ValueError("serial_number is required")
        Device = self.env["nsp.device"].sudo()
        device = Device.search([
            ("controller_id", "=", controller.id),
            ("serial_number", "=", serial_number),
            ("managed", "=", True),
        ], limit=1)
        if not device:
            raise ValueError("device_not_found")
        device_code = str(item.get("device_code") or "").strip()
        if device_code and device.device_code and device_code != device.device_code:
            raise ValueError("device_not_found")
        status = str(item.get("device_status") or "online").strip().lower()
        if status not in ("online", "offline", "degraded"):
            raise ValueError("invalid_payload")
        last_seen_at = self._safe_datetime_value(item.get("last_seen_at"), default_now=False) or fields.Datetime.now()
        connection = item.get("connection") if isinstance(item.get("connection"), dict) else {}
        vals = {"status": status, "last_seen": last_seen_at}
        if item.get("firmware_version") not in (None, ""):
            vals["firmware_version"] = str(item.get("firmware_version"))
        if connection.get("ip_address") not in (None, ""):
            vals["device_ip"] = str(connection.get("ip_address"))
        if connection.get("port") not in (None, ""):
            vals["device_port"] = int(connection.get("port"))
        antennas = item.get("antennas") or []
        if not isinstance(antennas, list):
            raise ValueError("antennas must be an array")
        Antenna = self.env["nsp.device.antenna"].sudo().with_context(active_test=False)
        antenna_updates = []
        for antenna_item in antennas:
            if not isinstance(antenna_item, dict):
                raise ValueError("invalid_payload")
            try:
                antenna_no = int(antenna_item.get("antenna_no") or 0)
            except Exception:
                antenna_no = 0
            antenna = Antenna.search([("device_id", "=", device.id), ("antenna_id", "=", antenna_no)], limit=1)
            if not antenna:
                raise ValueError("antenna_not_found")
            antenna_status = str(antenna_item.get("antenna_status") or "online").strip().lower()
            if antenna_status not in ("online", "offline", "degraded"):
                raise ValueError("invalid_payload")
            antenna_vals = {
                "status": antenna_status,
                "is_active": bool(antenna_item.get("enabled", antenna.is_active)),
            }
            if "last_seen" in Antenna._fields:
                antenna_vals["last_seen"] = self._safe_datetime_value(antenna_item.get("last_seen_at"), default_now=False) or last_seen_at
            for field_name in ("power_dbm", "return_loss_db"):
                if field_name in Antenna._fields and antenna_item.get(field_name) not in (None, ""):
                    antenna_vals[field_name] = int(antenna_item.get(field_name))
            antenna_updates.append((antenna, antenna_vals))
        device.write(vals)
        for antenna, antenna_vals in antenna_updates:
            antenna.write(antenna_vals)
        return device

    @endpoint("NSP Gatekeeper Edge Server Status", route_suffix="edge-server/status", methods="POST", code="nsp_gatekeeper_edge_server_status")
    def api_edge_server_status(self):
        data = self._payload()
        application, _actor, edge_server, error = self._auth_edge_server_sync(data)
        if error:
            return error
        heartbeat_data = dict(data)
        heartbeat_data["_heartbeat_received"] = True
        heartbeat_data.setdefault("current_status", "online")
        self._update_edge_server_status_from_payload(edge_server, heartbeat_data)
        return self._ok({
            "edge_server_code": edge_server.edge_server_code,
            "current_status": edge_server.status,
            "last_seen_at": self._iso_datetime(edge_server.timestamp),
            "server_time": self._iso_datetime(fields.Datetime.now()),
        }, message="Edge Server status accepted.")

    @endpoint("NSP Gatekeeper Devices Status Sync", route_suffix="devices-status/sync", methods="POST", code="nsp_gatekeeper_devices_status_sync")
    def api_devices_status_sync(self):
        data = self._payload()
        _application, _actor, edge_server, error = self._auth_edge_server_sync(data)
        if error:
            return error
        items = self._device_status_payloads_from_data(data)
        results = []
        processed = failed = 0
        Controller = self.env["nsp.controller"].sudo()
        for index, item in enumerate(items):
            key = str(item.get("serial_number") or item.get("device_code") or "").strip() if isinstance(item, dict) else ""
            try:
                if not isinstance(item, dict):
                    raise ValueError("invalid_payload")
                controller_code = str(item.get("controller_code") or "").strip()
                if not controller_code:
                    raise ValueError("missing_controller_code")
                controller = Controller.search([
                    ("controller_id", "=", controller_code),
                    ("edge_server_id", "=", edge_server.id),
                ], limit=1)
                if not controller:
                    raise ValueError("route_not_allowed")
                device = self._apply_device_status(controller, item)
                processed += 1
                results.append({"index": index, "record_key": device.serial_number, "status": "processed", "message": "Processed"})
            except Exception as exc:
                failed += 1
                results.append({"index": index, "record_key": key, "status": "rejected", "message": str(exc)})
        return self._ok({"received": len(items), "processed": processed, "failed": failed, "results": results}, message="Device status synchronized.")

    @endpoint("NSP Gatekeeper Devices Report", route_suffix="devices/report", methods="POST", code="nsp_gatekeeper_devices_report")
    def api_devices_report(self):
        data = self._payload()
        controller, error = self._auth_controller(data)
        if error:
            return error
        items = data.get("devices") or data.get("items") or []
        if isinstance(items, dict):
            items = [items]
        if not isinstance(items, list):
            return self._error("devices must be an array", 400, error_code="invalid_payload", details={"field": "devices"})
        results = []
        processed = failed = 0
        for index, item in enumerate(items):
            key = str(item.get("serial_number") or item.get("device_code") or "").strip() if isinstance(item, dict) else ""
            try:
                device = self._apply_device_status(controller, item)
                processed += 1
                results.append({"index": index, "record_key": device.serial_number, "status": "processed", "message": "Processed"})
            except Exception as exc:
                failed += 1
                results.append({"index": index, "record_key": key, "status": "rejected", "message": str(exc)})
        controller.write({"last_device_report_at": fields.Datetime.now()})
        return self._ok({"received": len(items), "processed": processed, "failed": failed, "results": results}, message="Device report processed.")

    @api.model
    def _encode_sync_cursor(self, record):
        if not record:
            return False
        value = {
            "write_date": fields.Datetime.to_string(record.write_date or record.create_date),
            "id": int(record.id),
        }
        raw = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @api.model
    def _decode_sync_cursor(self, token):
        if not token:
            return False
        try:
            text = str(token).strip()
            text += "=" * (-len(text) % 4)
            value = json.loads(base64.urlsafe_b64decode(text.encode("ascii")).decode("utf-8"))
            write_date = self._safe_datetime_value(value.get("write_date"), default_now=False)
            record_id = int(value.get("id") or 0)
            if not write_date or record_id <= 0:
                raise ValueError()
            return write_date, record_id
        except Exception:
            raise ValueError("invalid_sync_cursor")

    @api.model
    def _cursor_page(self, model, data, domain=None, max_limit=500):
        limit = min(max(self._safe_positive_int((data or {}).get("limit"), 500), 1), max_limit)
        cursor = self._decode_sync_cursor((data or {}).get("sync_cursor"))
        search_domain = list(domain or [])
        if cursor:
            cursor_date, cursor_id = cursor
            search_domain += [
                "|", ("write_date", ">", cursor_date),
                "&", ("write_date", "=", cursor_date), ("id", ">", cursor_id),
            ]
        records = model.with_context(active_test=False).search(search_domain, order="write_date asc, id asc", limit=limit + 1)
        has_more = len(records) > limit
        page_records = records[:limit]
        next_cursor = self._encode_sync_cursor(page_records[-1]) if page_records else ((data or {}).get("sync_cursor") or False)
        return page_records, next_cursor, has_more, fields.Datetime.now()

    def _card_sync_payload(self, card):
        UserCard = self.env["nsp.user.card"].sudo() if "nsp.user.card" in self.env.registry.models else False
        VehicleCard = self.env["nsp.vehicle.card"].sudo() if "nsp.vehicle.card" in self.env.registry.models else False
        user_line = UserCard.search([("card_id", "=", card.id), ("state", "=", "active")], limit=1) if UserCard else False
        vehicle_line = VehicleCard.search([("card_id", "=", card.id), ("state", "=", "active")], limit=1) if VehicleCard else False
        payload = {
            "card_uid": card.tid,
            "card_type": card.card_type,
            "owner_type": "unassigned",
            "active": bool(getattr(card, "active", True)),
        }
        if vehicle_line:
            vehicle = vehicle_line.vehicle_id
            owner_code = ""
            for field_name in ("vehicle_code", "code"):
                if field_name in vehicle._fields and vehicle[field_name]:
                    owner_code = str(vehicle[field_name]).strip()
                    break
            payload["owner_type"] = "vehicle"
            if owner_code:
                payload["owner_code"] = owner_code
        elif user_line:
            owner_code = self._employee_code(user_line.user_id)
            payload["owner_type"] = "user"
            if owner_code:
                payload["owner_code"] = owner_code
        return payload

    @endpoint("NSP Gatekeeper Cards Sync", route_suffix="cards/sync", methods="POST", code="nsp_gatekeeper_cards_sync")
    def api_cards_sync(self):
        data = self._payload()
        application, actor_kind, edge_server, error = self._auth_edge_server_sync(data)
        if error:
            return error
        Card = self.env["nsp.rfid.card"].sudo()
        cards, next_cursor, has_more, server_time = self._cursor_page(Card, data)
        items = [self._card_sync_payload(card) for card in cards]
        return self._ok({
            "items": items, "next_sync_cursor": next_cursor, "has_more": has_more,
            "server_time": self._iso_datetime(server_time),
        }, message="Cards sync loaded.")

    @endpoint("NSP Gatekeeper Users Sync", route_suffix="employees/sync", methods="POST", code="nsp_gatekeeper_employees_sync")
    def api_employees_sync(self):
        data = self._payload()
        application, actor_kind, edge_server, error = self._auth_edge_server_sync(data)
        if error:
            return error
        User = self.env["nsp.user"].sudo()
        domain = []
        code_fields = self._employee_code_fields()
        if code_fields:
            domain += [(code_fields[0], "!=", False), (code_fields[0], "!=", "")]
        users, next_cursor, has_more, server_time = self._cursor_page(User, data, domain=domain)
        items = []
        for user in users:
            item = {
                "user_code": self._employee_code(user),
                "name": user.name or user.display_name,
                "active": bool(user.active) if "active" in user._fields else True,
            }
            items.append(item)
        return self._ok({
            "items": items, "next_sync_cursor": next_cursor, "has_more": has_more,
            "server_time": self._iso_datetime(server_time),
        }, message="Users sync loaded.")

    @api.model
    def _user_access_code(self, user):
        """Return the stable user/employee code used by Controller cache.

        NSP no longer depends on Odoo HR employee records. The Controller stores
        `employee_id` as a text key, so use `nsp.user.user_code` first and fall
        back to the record id only when a code is missing.
        """
        if not user:
            return ""
        code = ""
        try:
            if "user_code" in user._fields:
                code = user.user_code or ""
        except Exception:
            code = ""
        return str(code or "").strip()

    @endpoint("NSP Gatekeeper Vehicles Sync", route_suffix="vehicles/sync", methods="POST", code="nsp_gatekeeper_vehicles_sync")
    def api_vehicles_sync(self):
        data = self._payload()
        application, actor_kind, edge_server, error = self._auth_edge_server_sync(data)
        if error:
            return error
        Vehicle = self.env["nsp.vehicle"].sudo()
        vehicles, next_cursor, has_more, server_time = self._cursor_page(Vehicle, data)
        items = []
        for vehicle in vehicles:
            owner = vehicle.owner_id
            vehicle_code = ""
            for field_name in ("vehicle_code", "code"):
                if field_name in vehicle._fields and vehicle[field_name]:
                    vehicle_code = vehicle[field_name]
                    break
            vehicle_code = vehicle_code or vehicle.license_plate or ""
            item = {
                "vehicle_code": vehicle_code,
                "license_plate": vehicle.license_plate or "",
                "active": vehicle.state == "approved",
            }
            owner_user_code = self._user_access_code(owner)
            if owner_user_code:
                item["owner_user_code"] = owner_user_code
            items.append(item)
        return self._ok({
            "items": items, "next_sync_cursor": next_cursor, "has_more": has_more,
            "server_time": self._iso_datetime(server_time),
        }, message="Vehicles sync loaded.")

    @endpoint("NSP Gatekeeper Branches Sync", route_suffix="branches/sync", methods="POST", code="nsp_gatekeeper_branches_sync")
    def api_branches_sync(self):
        data = self._payload()
        application, actor_kind, edge_server, error = self._auth_edge_server_sync(data)
        if error:
            return error
        Branch = self.env["nsp.branch"].sudo().with_context(active_test=False)
        branches, next_cursor, has_more, server_time = self._cursor_page(Branch, data)
        items = [{
            "branch_code": branch.code,
            "branch_name": branch.name,
            "timezone": branch.timezone or "Asia/Ho_Chi_Minh",
            "active": branch.status == "active",
        } for branch in branches]
        return self._ok({
            "items": items, "next_sync_cursor": next_cursor, "has_more": has_more,
            "server_time": self._iso_datetime(server_time),
        }, message="Branches sync loaded.")

    @endpoint("NSP Gatekeeper Vehicle Borrow Sync", route_suffix="vehicle-borrow/sync", methods="POST", code="nsp_gatekeeper_vehicle_borrow_sync")
    def api_vehicle_borrow_sync(self):
        data = self._payload()
        application, actor_kind, edge_server, error = self._auth_edge_server_sync(data)
        if error:
            return error
        if "nsp.vehicle.borrow.request" not in self.env.registry.models:
            return self._ok({"items": [], "next_sync_cursor": data.get("sync_cursor") or False, "has_more": False, "server_time": self._iso_datetime(fields.Datetime.now())})
        Borrow = self.env["nsp.vehicle.borrow.request"].sudo()
        records, next_cursor, has_more, server_time = self._cursor_page(Borrow, data)
        items = []
        for borrow in records:
            vehicle = borrow.vehicle_id
            borrower = borrow.borrower_id
            borrow_uid = getattr(borrow, "borrow_uid", False) or getattr(borrow, "borrow_code", False) or ""
            vehicle_code = ""
            for field_name in ("vehicle_code", "code"):
                if vehicle and field_name in vehicle._fields and vehicle[field_name]:
                    vehicle_code = vehicle[field_name]
                    break
            item = {
                "borrow_uid": borrow_uid,
                "vehicle_code": vehicle_code or (vehicle.license_plate if vehicle else ""),
                "borrower_user_code": self._user_access_code(borrower),
                "active": borrow.state == "approved" and not getattr(borrow, "returned_at", False),
            }
            if borrow.valid_from:
                item["valid_from"] = self._iso_datetime(borrow.valid_from)
            if borrow.valid_to:
                item["valid_to"] = self._iso_datetime(borrow.valid_to)
            items.append(item)
        return self._ok({
            "items": items, "next_sync_cursor": next_cursor, "has_more": has_more,
            "server_time": self._iso_datetime(server_time),
        }, message="Vehicle borrow sync loaded.")

    @endpoint("NSP Parking Operation Configuration Sync", route_suffix="parking-config/sync", methods="POST", code="nsp_parking_config_sync")
    def api_parking_config_sync(self):
        data = self._payload()
        _application, _actor_kind, _edge_server, error = self._auth_edge_server_sync(data)
        if error:
            return error
        ParkingOperation = self.env["nsp.parking.area"].sudo().with_context(active_test=False)
        domain = []
        parking_area_code = str(data.get("parking_area_code") or "").strip().upper()
        if parking_area_code:
            domain.append(("code", "=", parking_area_code))
        records, next_cursor, has_more, server_time = self._cursor_page(ParkingOperation, data, domain=domain)
        return self._ok({
            "items": [record.prepare_sync_payload() for record in records],
            "next_sync_cursor": next_cursor,
            "has_more": has_more,
            "server_time": self._iso_datetime(server_time),
        }, message="Parking operation configuration sync loaded.")

    @endpoint("NSP Controller Device Configuration Pull", route_suffix="controller/device-config/pull", methods="POST", code="nsp_controller_device_config_pull")
    def api_controller_device_config_pull(self):
        data = self._payload()
        controller, error = self._auth_controller(data)
        if error:
            return error
        unsupported = sorted(set(data) - {"controller_code"})
        if unsupported:
            return self._error(
                "Unsupported field(s): %s" % ", ".join(unsupported),
                400,
                error_code="invalid_payload",
                details={"unsupported_fields": unsupported},
            )
        devices = controller.device_ids.filtered(lambda rec: rec.managed).sorted(
            key=lambda rec: (rec.serial_number or "", rec.id)
        )
        return self._ok({
            "controller_code": controller.controller_id,
            "devices": [device._build_config_payload() for device in devices],
            "server_time": self._iso_datetime(fields.Datetime.now()),
        }, message="Controller device configuration loaded.")

    # ------------------------------------------------------------------
    # Measurement Session / Configuration / Run / Event APIs
    # ------------------------------------------------------------------
    @api.model
    def _measurement_input(self):
        data = self._payload()
        if request.httprequest.method == "GET":
            data.update(self._params())
        return data

    @api.model
    def _measurement_require_fields(self, data, required):
        missing = [name for name in required if data.get(name) in (None, "", [])]
        if missing:
            raise ValueError("missing_%s" % missing[0])

    @api.model
    def _measurement_reject_unknown_fields(self, data, allowed):
        unknown = sorted(set(data or {}) - set(allowed))
        if unknown:
            raise ValueError("invalid_payload: unsupported field(s): %s" % ", ".join(unknown))

    @api.model
    def _iso_datetime(self, value):
        if not value:
            return None
        parsed = fields.Datetime.to_datetime(value)
        if not parsed:
            return None
        if parsed.tzinfo:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")

    @api.model
    def _measurement_datetime(self, value, required=False, default_now=False):
        if not value:
            if required:
                raise ValueError("missing_datetime")
            return fields.Datetime.now() if default_now else False
        text = str(value).strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except Exception:
            parsed = fields.Datetime.to_datetime(text)
        if not parsed:
            raise ValueError("invalid_datetime")
        if parsed.tzinfo:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        else:
            parsed = parsed.replace(tzinfo=None)
        return fields.Datetime.to_string(parsed)

    @api.model
    def _measurement_session(self, uid):
        value = str(uid or "").strip().upper()
        if not value:
            raise ValueError("missing_measurement_session_uid")
        session = self.env["nsp.measurement.session"].sudo().search([
            ("measurement_session_uid", "=", value),
        ], limit=1)
        if not session:
            raise ValueError("measurement_session_not_found")
        return session

    @api.model
    def _measurement_run(self, uid, session=False):
        value = str(uid or "").strip().upper()
        if not value:
            raise ValueError("missing_measurement_run_uid")
        domain = [("measurement_run_uid", "=", value)]
        if session:
            domain.append(("session_id", "=", session.id))
        run = self.env["nsp.measurement.run"].sudo().search(domain, limit=1)
        if not run:
            raise ValueError("measurement_run_not_found")
        return run

    @api.model
    def _measurement_session_in_local_scope(self, session, edge_server):
        return bool(session.controller_id and session.controller_id.edge_server_id == edge_server)

    @api.model
    def _measurement_antenna_payload(self, session):
        grouped = {}
        for line in session.antenna_ids.sorted(
            key=lambda item: (item.serial_number or "", item.antenna_no or 0, item.id)
        ):
            grouped.setdefault(line.serial_number, []).append(int(line.antenna_no))
        return [
            {"serial_number": serial_number, "antennas": sorted(set(antenna_numbers))}
            for serial_number, antenna_numbers in sorted(grouped.items())
        ]

    @api.model
    def _measurement_config_payload(self, session):
        payload = {
            "measurement_session_uid": session.measurement_session_uid,
            "measurement_code": session.measurement_code,
            "controller_code": session.controller_id.controller_id,
            "measurement_status": session.measurement_status,
            "measurement_antennas": self._measurement_antenna_payload(session),
        }
        if session.planned_start_at:
            payload["planned_start_at"] = self._iso_datetime(session.planned_start_at)
        if session.planned_end_at:
            payload["planned_end_at"] = self._iso_datetime(session.planned_end_at)
        if session.note:
            payload["note"] = session.note
        return payload

    @api.model
    def _measurement_session_payload(self, session, include_detail=False):
        payload = self._measurement_config_payload(session)
        payload.update({
            "run_count": int(session.run_count or 0),
            "measurement_event_count": int(session.event_count or 0),
            "created_at": self._iso_datetime(session.create_date),
        })
        if session.started_at:
            payload["started_at"] = self._iso_datetime(session.started_at)
        if session.completed_at:
            payload["completed_at"] = self._iso_datetime(session.completed_at)
        if session.cancelled_at:
            payload["cancelled_at"] = self._iso_datetime(session.cancelled_at)
        if include_detail:
            payload["runs"] = [{
                "measurement_run_uid": run.measurement_run_uid,
                "run_status": run.run_status,
                "measurement_count": int(run.measurement_count or 0),
                **({"started_at": self._iso_datetime(run.started_at)} if run.started_at else {}),
                **({"stopped_at": self._iso_datetime(run.stopped_at)} if run.stopped_at else {}),
            } for run in session.run_ids.sorted(key=lambda item: (item.create_date, item.id))]
            payload["antenna_summaries"] = [{
                "serial_number": row.serial_number,
                "antenna_no": int(row.antenna_no),
                "read_count": int(row.read_count or 0),
                "min_rssi_dbm": row.min_rssi_dbm,
                "max_rssi_dbm": row.max_rssi_dbm,
                "average_rssi_dbm": row.average_rssi_dbm,
                "first_read_at": self._iso_datetime(row.first_read_at),
                "last_read_at": self._iso_datetime(row.last_read_at),
            } for row in session.antenna_summary_ids]
            payload["pair_summaries"] = [{
                "from_serial_number": row.from_serial_number,
                "from_antenna_no": int(row.from_antenna_no),
                "to_serial_number": row.to_serial_number,
                "to_antenna_no": int(row.to_antenna_no),
                "sample_count": int(row.sample_count or 0),
                "min_interval_ms": int(row.min_interval_ms or 0),
                "max_interval_ms": int(row.max_interval_ms or 0),
                "average_interval_ms": int(row.average_interval_ms or 0),
            } for row in session.pair_summary_ids]
        return payload

    @api.model
    def _measurement_resolve_controller(self, data, current_session=False):
        controller_code = str(
            data.get("controller_code")
            or (current_session.controller_id.controller_id if current_session else "")
        ).strip()
        self._measurement_require_fields({"controller_code": controller_code}, ["controller_code"])
        controller = self.env["nsp.controller"].sudo().with_context(active_test=False).search([
            ("controller_id", "=", controller_code),
        ], limit=1)
        if not controller:
            raise ValueError("controller_not_found")
        return controller

    @api.model
    def _measurement_resolve_antennas(self, controller, values):
        if not isinstance(values, list) or not values:
            raise ValueError("missing_measurement_antennas")
        Antenna = self.env["nsp.device.antenna"].sudo()
        result, seen = [], set()
        for item in values:
            if not isinstance(item, dict):
                raise ValueError("invalid_payload")
            self._measurement_reject_unknown_fields(item, {"serial_number", "antennas"})
            serial_number = str(item.get("serial_number") or "").strip().upper()
            antenna_numbers = item.get("antennas")
            if not serial_number or not isinstance(antenna_numbers, list) or not antenna_numbers:
                raise ValueError("invalid_payload")
            for raw_number in antenna_numbers:
                try:
                    antenna_no = int(raw_number)
                except Exception:
                    antenna_no = 0
                key = (serial_number, antenna_no)
                if antenna_no <= 0:
                    raise ValueError("antenna_not_found")
                if key in seen:
                    raise ValueError("duplicate_antenna_mapping")
                seen.add(key)
                antenna = Antenna.search([
                    ("device_id.controller_id", "=", controller.id),
                    ("device_id.managed", "=", True),
                    ("device_id.device_type_id.supports_antenna_mapping", "=", True),
                    ("device_id.serial_number", "=", serial_number),
                    ("antenna_id", "=", antenna_no),
                ], limit=1)
                if not antenna:
                    raise ValueError("antenna_not_found")
                result.append(antenna)
        return result

    @api.model
    def _measurement_error_response(self, exc):
        text = str(exc)
        code = text.split(":", 1)[0].strip()
        status = 400
        if code.endswith("_not_found") or code in {"controller_not_found", "antenna_not_found"}:
            status = 404
        elif code in {"controller_not_in_scope", "edge_server_not_in_scope", "route_not_allowed"}:
            status = 403
        elif code == "controller_offline":
            status = 503
        elif code in {
            "measurement_session_conflict", "measurement_session_not_editable",
            "measurement_session_completed", "measurement_session_cancelled",
            "sync_uid_conflict", "invalid_status_transition",
            "measurement_run_already_running", "measurement_run_not_running",
            "measurement_run_not_stopped", "measurement_sync_pending",
            "measurement_command_status_conflict", "measurement_not_running",
        }:
            status = 409
        return self._error(text.replace("_", " "), status, error_code=code, details={})

    @endpoint("NSP Measurement Session Create", route_suffix="measurement-sessions", methods="POST", code="nsp_measurement_session_create")
    def api_measurement_session_create(self):
        data = self._payload()
        _application, _actor, error = self._auth_sync_application(data)
        if error:
            return error
        try:
            allowed = {
                "measurement_code", "controller_code", "planned_start_at", "planned_end_at",
                "note", "measurement_antennas",
            }
            self._measurement_reject_unknown_fields(data, allowed)
            self._measurement_require_fields(data, ["measurement_code", "controller_code", "measurement_antennas"])
            controller = self._measurement_resolve_controller(data)
            antennas = self._measurement_resolve_antennas(controller, data.get("measurement_antennas"))
            planned_start = self._measurement_datetime(data.get("planned_start_at"))
            planned_end = self._measurement_datetime(data.get("planned_end_at"))
            if planned_start and planned_end and planned_end <= planned_start:
                raise ValueError("invalid_planned_time_range")
            with self.env.cr.savepoint():
                session = self.env["nsp.measurement.session"].sudo().create({
                    "measurement_code": str(data.get("measurement_code") or "").strip().upper(),
                    "controller_id": controller.id,
                    "planned_start_at": planned_start,
                    "planned_end_at": planned_end,
                    "note": str(data.get("note") or "").strip() or False,
                })
                self.env["nsp.measurement.antenna"].sudo().create([
                    {"session_id": session.id, "antenna_ref_id": antenna.id}
                    for antenna in antennas
                ])
            return self._ok(
                {"data": self._measurement_session_payload(session, include_detail=True)},
                status_code=201,
                message="Measurement Session created.",
            )
        except Exception as exc:
            return self._measurement_error_response(exc)

    @endpoint("NSP Measurement Session Update", route_suffix="measurement-sessions/update", methods="PATCH", code="nsp_measurement_session_update")
    def api_measurement_session_update(self):
        data = self._payload()
        _application, _actor, error = self._auth_sync_application(data)
        if error:
            return error
        try:
            allowed = {
                "measurement_session_uid", "measurement_code", "controller_code",
                "planned_start_at", "planned_end_at", "note", "measurement_antennas",
            }
            self._measurement_reject_unknown_fields(data, allowed)
            session = self._measurement_session(data.get("measurement_session_uid"))
            if session.measurement_status != "draft":
                raise ValueError("measurement_session_not_editable")
            controller = self._measurement_resolve_controller(data, current_session=session)
            vals = {"controller_id": controller.id}
            if "measurement_code" in data:
                code = str(data.get("measurement_code") or "").strip().upper()
                if not code:
                    raise ValueError("invalid_payload")
                vals["measurement_code"] = code
            if "planned_start_at" in data:
                vals["planned_start_at"] = self._measurement_datetime(data.get("planned_start_at"))
            if "planned_end_at" in data:
                vals["planned_end_at"] = self._measurement_datetime(data.get("planned_end_at"))
            if "note" in data:
                vals["note"] = str(data.get("note") or "").strip() or False
            effective_start = vals.get("planned_start_at", session.planned_start_at)
            effective_end = vals.get("planned_end_at", session.planned_end_at)
            if effective_start and effective_end and effective_end <= effective_start:
                raise ValueError("invalid_planned_time_range")
            controller_changed = controller != session.controller_id
            if controller_changed and "measurement_antennas" not in data:
                raise ValueError("missing_measurement_antennas")
            antennas = None
            if "measurement_antennas" in data:
                antennas = self._measurement_resolve_antennas(controller, data.get("measurement_antennas"))
            with self.env.cr.savepoint():
                session.write(vals)
                if antennas is not None:
                    session.antenna_ids.unlink()
                    self.env["nsp.measurement.antenna"].sudo().create([
                        {"session_id": session.id, "antenna_ref_id": antenna.id}
                        for antenna in antennas
                    ])
            return self._ok({"data": self._measurement_session_payload(session, include_detail=True)}, message="Measurement Session updated.")
        except Exception as exc:
            return self._measurement_error_response(exc)

    @endpoint("NSP Measurement Session Detail", route_suffix="measurement-sessions/detail", methods="GET,POST", code="nsp_measurement_session_detail")
    def api_measurement_session_detail(self):
        data = self._measurement_input()
        _application, _actor, error = self._auth_sync_application(data)
        if error:
            return error
        try:
            self._measurement_reject_unknown_fields(data, {"measurement_session_uid"})
            session = self._measurement_session(data.get("measurement_session_uid"))
            return self._ok({"data": self._measurement_session_payload(session, include_detail=True)}, message="Measurement Session loaded.")
        except Exception as exc:
            return self._measurement_error_response(exc)

    @endpoint("NSP Measurement Session Ready", route_suffix="measurement-sessions/ready", methods="POST", code="nsp_measurement_session_ready")
    def api_measurement_session_ready(self):
        data = self._payload()
        _application, _actor, error = self._auth_sync_application(data)
        if error:
            return error
        try:
            self._measurement_reject_unknown_fields(data, {"measurement_session_uid"})
            session = self._measurement_session(data.get("measurement_session_uid"))
            session.action_ready()
            return self._ok({"data": self._measurement_session_payload(session)}, message="Measurement Session is ready.")
        except Exception as exc:
            return self._measurement_error_response(exc)

    @endpoint("NSP Measurement Session Complete", route_suffix="measurement-sessions/complete", methods="POST", code="nsp_measurement_session_complete")
    def api_measurement_session_complete(self):
        data = self._payload()
        _application, _actor, error = self._auth_sync_application(data)
        if error:
            return error
        try:
            self._measurement_reject_unknown_fields(data, {"measurement_session_uid"})
            session = self._measurement_session(data.get("measurement_session_uid"))
            if session.event_ids.filtered(lambda item: item.sync_state != "synced"):
                raise ValueError("measurement_sync_pending")
            if session.run_ids.filtered(lambda item: item.run_status != "stopped"):
                raise ValueError("measurement_run_not_stopped")
            session.action_complete()
            return self._ok({"data": self._measurement_session_payload(session, include_detail=True)}, message="Measurement Session completed.")
        except Exception as exc:
            return self._measurement_error_response(exc)

    @endpoint("NSP Measurement Session Cancel", route_suffix="measurement-sessions/cancel", methods="POST", code="nsp_measurement_session_cancel")
    def api_measurement_session_cancel(self):
        data = self._payload()
        _application, _actor, error = self._auth_sync_application(data)
        if error:
            return error
        try:
            self._measurement_reject_unknown_fields(data, {"measurement_session_uid"})
            session = self._measurement_session(data.get("measurement_session_uid"))
            session.action_cancel()
            return self._ok({"data": self._measurement_session_payload(session)}, message="Measurement Session cancelled.")
        except Exception as exc:
            return self._measurement_error_response(exc)

    @endpoint("NSP Measurement Configuration Sync", route_suffix="measurement-config/sync", methods="POST", code="nsp_measurement_config_sync")
    def api_measurement_config_sync(self):
        data = self._payload()
        _application, _actor, edge_server, error = self._auth_edge_server_sync(data)
        if error:
            return error
        try:
            self._measurement_reject_unknown_fields(data, {"edge_server_code", "sync_cursor", "limit"})
            Session = self.env["nsp.measurement.session"].sudo()
            sessions, next_cursor, has_more, server_time = self._cursor_page(Session, data, domain=[
                ("controller_id.edge_server_id", "=", edge_server.id),
                ("measurement_status", "!=", "draft"),
            ], max_limit=100)
            return self._ok({
                "items": [self._measurement_config_payload(session) for session in sessions],
                "next_sync_cursor": next_cursor,
                "has_more": has_more,
                "server_time": self._iso_datetime(server_time),
            }, message="Measurement Configuration sync loaded.")
        except Exception as exc:
            return self._measurement_error_response(exc)

    @endpoint("NSP Controller Measurement Configuration Pull", route_suffix="controller/measurement-config/pull", methods="POST", code="nsp_controller_measurement_config_pull")
    def api_controller_measurement_config_pull(self):
        data = self._payload()
        controller, error = self._auth_controller(data)
        if error:
            return error
        try:
            self._measurement_reject_unknown_fields(data, {"controller_code", "current_measurement_session_uid"})
            session = self.env["nsp.measurement.session"].sudo().search([
                ("controller_id", "=", controller.id),
                ("measurement_status", "in", ["ready", "measuring"]),
            ], order="write_date desc, id desc", limit=1)
            if not session:
                return self._ok({"data": {"update_available": False}}, message="No Measurement Configuration is available.")
            current_uid = str(data.get("current_measurement_session_uid") or "").strip().upper()
            if current_uid == session.measurement_session_uid:
                return self._ok({"data": {"update_available": False}}, message="Measurement Configuration is current.")
            payload = self._measurement_config_payload(session)
            payload["update_available"] = True
            return self._ok({"data": payload}, message="Measurement Configuration loaded.")
        except Exception as exc:
            return self._measurement_error_response(exc)

    @endpoint("NSP Edge Measurement Session Start", route_suffix="edge/measurement-sessions/start", methods="POST", code="nsp_edge_measurement_session_start")
    def api_edge_measurement_session_start(self):
        data = self._payload()
        _application, _actor, edge_server, error = self._auth_edge_server_sync(data)
        if error:
            return error
        try:
            allowed = {"edge_server_code", "controller_code", "measurement_session_uid", "requested_at"}
            self._measurement_reject_unknown_fields(data, allowed)
            self._measurement_require_fields(data, ["controller_code", "measurement_session_uid", "requested_at"])
            session = self._measurement_session(data.get("measurement_session_uid"))
            if not self._measurement_session_in_local_scope(session, edge_server):
                raise ValueError("edge_server_not_in_scope")
            if str(data.get("controller_code") or "").strip() != session.controller_id.controller_id:
                raise ValueError("controller_not_in_scope")
            if session.measurement_status not in ("ready", "measuring"):
                raise ValueError("invalid_status_transition")
            if session.controller_id.status != "online":
                raise ValueError("controller_offline")
            requested_at = self._measurement_datetime(data.get("requested_at"), required=True)
            Run = self.env["nsp.measurement.run"].sudo()
            Command = self.env["nsp.measurement.command"].sudo()
            with self.env.cr.savepoint():
                active = Run.search([
                    ("session_id.controller_id", "=", session.controller_id.id),
                    ("run_status", "in", ["pending", "starting", "running", "stopping"]),
                ], order="id desc", limit=1)
                if active:
                    raise ValueError("measurement_run_already_running")
                run = Run.create({
                    "session_id": session.id,
                    "run_status": "pending",
                })
                command = Command.create({
                    "session_id": session.id,
                    "run_id": run.id,
                    "command_type": "start_measurement",
                    "command_status": "pending",
                    "requested_at": requested_at,
                })
            return self._ok({"data": {
                "measurement_session_uid": session.measurement_session_uid,
                "measurement_run_uid": run.measurement_run_uid,
                "run_status": run.run_status,
                "command_status": command.command_status,
            }}, message="Measurement start command created.")
        except Exception as exc:
            return self._measurement_error_response(exc)

    @endpoint("NSP Edge Measurement Session Stop", route_suffix="edge/measurement-sessions/stop", methods="POST", code="nsp_edge_measurement_session_stop")
    def api_edge_measurement_session_stop(self):
        data = self._payload()
        _application, _actor, edge_server, error = self._auth_edge_server_sync(data)
        if error:
            return error
        try:
            allowed = {"edge_server_code", "controller_code", "measurement_session_uid", "measurement_run_uid", "requested_at"}
            self._measurement_reject_unknown_fields(data, allowed)
            self._measurement_require_fields(data, ["controller_code", "measurement_session_uid", "measurement_run_uid", "requested_at"])
            session = self._measurement_session(data.get("measurement_session_uid"))
            if not self._measurement_session_in_local_scope(session, edge_server):
                raise ValueError("edge_server_not_in_scope")
            if str(data.get("controller_code") or "").strip() != session.controller_id.controller_id:
                raise ValueError("controller_not_in_scope")
            run = self._measurement_run(data.get("measurement_run_uid"), session=session)
            requested_at = self._measurement_datetime(data.get("requested_at"), required=True)
            Command = self.env["nsp.measurement.command"].sudo()
            if run.run_status == "stopped":
                return self._ok({"data": {
                    "measurement_session_uid": session.measurement_session_uid,
                    "measurement_run_uid": run.measurement_run_uid,
                    "run_status": "stopped",
                    "command_status": "succeeded",
                }}, message="Measurement Run already stopped.")
            if run.run_status == "stopping":
                command = Command.search([
                    ("run_id", "=", run.id),
                    ("command_type", "=", "stop_measurement"),
                    ("command_status", "=", "pending"),
                ], order="id desc", limit=1)
                return self._ok({"data": {
                    "measurement_session_uid": session.measurement_session_uid,
                    "measurement_run_uid": run.measurement_run_uid,
                    "run_status": "stopping",
                    "command_status": command.command_status if command else "pending",
                }}, message="Measurement stop command already exists.")
            if run.run_status != "running":
                raise ValueError("measurement_run_not_running")
            run.write({"run_status": "stopping"})
            command = Command.create({
                "session_id": session.id,
                "run_id": run.id,
                "command_type": "stop_measurement",
                "command_status": "pending",
                "requested_at": requested_at,
            })
            return self._ok({"data": {
                "measurement_session_uid": session.measurement_session_uid,
                "measurement_run_uid": run.measurement_run_uid,
                "run_status": run.run_status,
                "command_status": command.command_status,
            }}, message="Measurement stop command created.")
        except Exception as exc:
            return self._measurement_error_response(exc)

    @endpoint("NSP Edge Measurement Session Detail", route_suffix="edge/measurement-sessions/detail", methods="GET,POST", code="nsp_edge_measurement_session_detail")
    def api_edge_measurement_session_detail(self):
        data = self._measurement_input()
        _application, _actor, edge_server, error = self._auth_edge_server_sync(data)
        if error:
            return error
        try:
            self._measurement_reject_unknown_fields(data, {"edge_server_code", "measurement_session_uid"})
            session = self._measurement_session(data.get("measurement_session_uid"))
            if not self._measurement_session_in_local_scope(session, edge_server):
                raise ValueError("edge_server_not_in_scope")
            return self._ok({"data": self._measurement_session_payload(session, include_detail=True)}, message="Edge Measurement Session loaded.")
        except Exception as exc:
            return self._measurement_error_response(exc)

    @endpoint("NSP Controller Measurement Command Pull", route_suffix="controller/measurement-command/pull", methods="POST", code="nsp_controller_measurement_command_pull")
    def api_controller_measurement_command_pull(self):
        data = self._payload()
        controller, error = self._auth_controller(data)
        if error:
            return error
        try:
            self._measurement_reject_unknown_fields(data, {"controller_code"})
            command = self.env["nsp.measurement.command"].sudo().search([
                ("session_id.controller_id", "=", controller.id),
                ("session_id.measurement_status", "in", ["ready", "measuring"]),
                ("command_status", "=", "pending"),
            ], order="requested_at asc, id asc", limit=1)
            if not command:
                return self._ok({"data": {"command_available": False}}, message="No Measurement command is available.")
            if command.command_type == "start_measurement" and command.run_id.run_status == "pending":
                command.run_id.write({"run_status": "starting"})
            payload = {
                "command_available": True,
                "command_uid": command.command_uid,
                "command_type": command.command_type,
                "measurement_session_uid": command.session_id.measurement_session_uid,
                "measurement_run_uid": command.run_id.measurement_run_uid,
                "requested_at": self._iso_datetime(command.requested_at),
            }
            return self._ok({"data": payload}, message="Measurement command loaded.")
        except Exception as exc:
            return self._measurement_error_response(exc)

    @endpoint("NSP Controller Measurement Command Status", route_suffix="controller/measurement-command/status", methods="POST", code="nsp_controller_measurement_command_status")
    def api_controller_measurement_command_status(self):
        data = self._payload()
        controller, error = self._auth_controller(data)
        if error:
            return error
        try:
            allowed = {
                "controller_code", "command_uid", "command_type", "measurement_session_uid",
                "measurement_run_uid", "command_status", "effective_at", "error_code", "error_message",
            }
            self._measurement_reject_unknown_fields(data, allowed)
            self._measurement_require_fields(data, [
                "command_uid", "command_type", "measurement_session_uid",
                "measurement_run_uid", "command_status", "effective_at",
            ])
            command = self.env["nsp.measurement.command"].sudo().search([
                ("command_uid", "=", str(data.get("command_uid") or "").strip().upper()),
            ], limit=1)
            if not command:
                raise ValueError("measurement_command_not_found")
            session, run = command.session_id, command.run_id
            if session.controller_id != controller:
                raise ValueError("controller_not_in_scope")
            if str(data.get("measurement_session_uid") or "").strip().upper() != session.measurement_session_uid:
                raise ValueError("measurement_session_conflict")
            if str(data.get("measurement_run_uid") or "").strip().upper() != run.measurement_run_uid:
                raise ValueError("measurement_session_conflict")
            if str(data.get("command_type") or "").strip() != command.command_type:
                raise ValueError("measurement_session_conflict")
            status = str(data.get("command_status") or "").strip().lower()
            if status not in ("succeeded", "failed"):
                raise ValueError("invalid_payload")
            effective_at = self._measurement_datetime(data.get("effective_at"), required=True)
            if status == "succeeded":
                if data.get("error_code") or data.get("error_message"):
                    raise ValueError("invalid_payload")
                error_code = error_message = False
            else:
                self._measurement_require_fields(data, ["error_code", "error_message"])
                error_code = str(data.get("error_code") or "").strip()
                error_message = str(data.get("error_message") or "").strip()
            if command.command_status in ("succeeded", "failed"):
                if command.command_status == status:
                    return self._ok(message="Measurement command status already recorded.")
                raise ValueError("measurement_command_status_conflict")
            command.write({
                "command_status": status,
                "effective_at": effective_at,
                "error_code": error_code,
                "error_message": error_message,
            })
            if status == "succeeded" and command.command_type == "start_measurement":
                run.write({"run_status": "running", "started_at": effective_at})
                session.with_context(measurement_sync=True).write({
                    "measurement_status": "measuring",
                    "started_at": session.started_at or effective_at,
                })
            elif status == "succeeded":
                run.write({"run_status": "stopped", "stopped_at": effective_at})
            elif command.command_type == "start_measurement":
                run.write({"run_status": "failed"})
            else:
                run.write({"run_status": "running"})
            return self._ok(message="Measurement command status recorded.")
        except Exception as exc:
            return self._measurement_error_response(exc)

    @api.model
    def _measurement_event_canonical(self, session, run, controller_code, item, allow_controller_code=False):
        allowed = {"measurement_uid", "serial_number", "antenna_no", "tid", "read_at", "rssi_dbm"}
        if allow_controller_code:
            allowed.add("controller_code")
        self._measurement_reject_unknown_fields(item, allowed)
        required = ["measurement_uid", "serial_number", "antenna_no", "tid", "read_at"]
        if allow_controller_code:
            required.append("controller_code")
        self._measurement_require_fields(item, required)
        if allow_controller_code and str(item.get("controller_code") or "").strip() != controller_code:
            raise ValueError("controller_not_in_scope")
        measurement_uid = str(item.get("measurement_uid") or "").strip()
        serial_number = str(item.get("serial_number") or "").strip().upper()
        try:
            antenna_no = int(item.get("antenna_no") or 0)
        except Exception:
            antenna_no = 0
        if antenna_no <= 0:
            raise ValueError("antenna_not_found")
        tid = str(item.get("tid") or "").strip()
        if not tid:
            raise ValueError("invalid_tid")
        read_at = self._measurement_datetime(item.get("read_at"), required=True)
        mapping = self.env["nsp.measurement.antenna"].sudo().search([
            ("session_id", "=", session.id),
            ("antenna_ref_id.device_id.serial_number", "=", serial_number),
            ("antenna_ref_id.antenna_id", "=", antenna_no),
        ], limit=1)
        if not mapping:
            raise ValueError("antenna_not_found")
        if item.get("rssi_dbm") in (None, ""):
            rssi = False
        else:
            try:
                rssi = float(item.get("rssi_dbm"))
            except Exception:
                raise ValueError("invalid_rssi")
        canonical = {
            "measurement_uid": measurement_uid,
            "measurement_session_uid": session.measurement_session_uid,
            "measurement_run_uid": run.measurement_run_uid,
            "controller_code": controller_code,
            "serial_number": serial_number,
            "antenna_no": antenna_no,
            "tid": tid,
            "read_at": self._iso_datetime(read_at),
        }
        if rssi is not False:
            canonical["rssi_dbm"] = rssi
        payload_hash = hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        ).hexdigest()
        return canonical, read_at, rssi, payload_hash

    @api.model
    def _measurement_store_event(self, session, run, controller_code, item, sync_state, allow_controller_code=False):
        canonical, read_at, rssi, payload_hash = self._measurement_event_canonical(
            session, run, controller_code, item, allow_controller_code=allow_controller_code
        )
        Event = self.env["nsp.measurement.event"].sudo()
        existing = Event.search([("measurement_uid", "=", canonical["measurement_uid"])], limit=1)
        if existing:
            if existing.payload_hash != payload_hash:
                raise ValueError("sync_uid_conflict")
            return existing, True
        try:
            with self.env.cr.savepoint():
                event = Event.create({
                    "measurement_uid": canonical["measurement_uid"],
                    "session_id": session.id,
                    "run_id": run.id,
                    "serial_number": canonical["serial_number"],
                    "antenna_no": canonical["antenna_no"],
                    "tid": canonical["tid"],
                    "read_at": read_at,
                    "rssi_dbm": rssi,
                    "payload_hash": payload_hash,
                    "sync_state": sync_state,
                    "last_sync_at": fields.Datetime.now() if sync_state == "synced" else False,
                })
            return event, False
        except IntegrityError:
            existing = Event.search([("measurement_uid", "=", canonical["measurement_uid"])], limit=1)
            if not existing or existing.payload_hash != payload_hash:
                raise ValueError("sync_uid_conflict")
            return existing, True

    @api.model
    def _measurement_batch_result(self, items, handler):
        results, processed, failed = [], 0, 0
        for index, item in enumerate(items):
            key = str(item.get("measurement_uid") or "") if isinstance(item, dict) else ""
            try:
                if not isinstance(item, dict):
                    raise ValueError("invalid_payload")
                duplicate = handler(item)
                processed += 1
                results.append({
                    "index": index,
                    "record_key": key,
                    "status": "duplicate" if duplicate else "processed",
                    "message": "Already processed" if duplicate else "Processed",
                })
            except Exception as exc:
                failed += 1
                results.append({
                    "index": index,
                    "record_key": key,
                    "status": "rejected",
                    "error_code": str(exc).split(":", 1)[0],
                    "message": str(exc),
                })
        return {
            "received": len(items), "processed": processed, "failed": failed, "results": results,
        }

    @endpoint("NSP Controller Measurement Data Report", route_suffix="controller/measurement-data/report", methods="POST", code="nsp_controller_measurement_data_report")
    def api_controller_measurement_data_report(self):
        data = self._payload()
        controller, error = self._auth_controller(data)
        if error:
            return error
        try:
            allowed = {"controller_code", "measurement_session_uid", "measurement_run_uid", "measurements"}
            self._measurement_reject_unknown_fields(data, allowed)
            self._measurement_require_fields(data, ["measurement_session_uid", "measurement_run_uid", "measurements"])
            session = self._measurement_session(data.get("measurement_session_uid"))
            if session.controller_id != controller:
                raise ValueError("controller_not_in_scope")
            run = self._measurement_run(data.get("measurement_run_uid"), session=session)
            if session.measurement_status != "measuring" or run.run_status != "running":
                raise ValueError("measurement_not_running")
            items = data.get("measurements")
            if not isinstance(items, list) or not items:
                raise ValueError("invalid_payload")
            result = self._measurement_batch_result(items, lambda item: self._measurement_store_event(
                session, run, controller.controller_id, item, sync_state="pending"
            )[1])
            return self._ok(result, message="Measurement data processed.")
        except Exception as exc:
            return self._measurement_error_response(exc)

    @endpoint("NSP Measurement Data Sync", route_suffix="measurement-data/sync", methods="POST", code="nsp_measurement_data_sync")
    def api_measurement_data_sync(self):
        data = self._payload()
        _application, _actor, edge_server, error = self._auth_edge_server_sync(data)
        if error:
            return error
        try:
            allowed = {"edge_server_code", "measurement_session_uid", "measurement_run_uid", "measurements"}
            self._measurement_reject_unknown_fields(data, allowed)
            self._measurement_require_fields(data, ["measurement_session_uid", "measurement_run_uid", "measurements"])
            session = self._measurement_session(data.get("measurement_session_uid"))
            if not self._measurement_session_in_local_scope(session, edge_server):
                raise ValueError("edge_server_not_in_scope")
            run = self._measurement_run(data.get("measurement_run_uid"), session=session)
            items = data.get("measurements")
            if not isinstance(items, list) or not items:
                raise ValueError("invalid_payload")

            def store(item):
                controller_code = str(item.get("controller_code") or "").strip()
                if controller_code != session.controller_id.controller_id:
                    raise ValueError("controller_not_in_scope")
                existing = self.env["nsp.measurement.event"].sudo().search([
                    ("measurement_uid", "=", str(item.get("measurement_uid") or "")),
                ], limit=1)
                if not existing and session.measurement_status in ("completed", "cancelled"):
                    raise ValueError("measurement_session_completed")
                return self._measurement_store_event(
                    session, run, controller_code, item, sync_state="synced", allow_controller_code=True
                )[1]

            return self._ok(self._measurement_batch_result(items, store), message="Measurement sync processed.")
        except Exception as exc:
            return self._measurement_error_response(exc)

    @endpoint("NSP Measurement Session Status Sync", route_suffix="measurement-session-status/sync", methods="POST", code="nsp_measurement_session_status_sync")
    def api_measurement_session_status_sync(self):
        data = self._payload()
        _application, _actor, edge_server, error = self._auth_edge_server_sync(data)
        if error:
            return error
        try:
            allowed = {"edge_server_code", "measurement_session_uid", "measurement_status", "runs", "reported_at"}
            self._measurement_reject_unknown_fields(data, allowed)
            self._measurement_require_fields(data, ["measurement_session_uid", "measurement_status", "runs", "reported_at"])
            reported_at = self._measurement_datetime(data.get("reported_at"), required=True)
            session = self._measurement_session(data.get("measurement_session_uid"))
            if not self._measurement_session_in_local_scope(session, edge_server):
                raise ValueError("edge_server_not_in_scope")
            measurement_status = str(data.get("measurement_status") or "").strip().lower()
            if measurement_status not in ("ready", "measuring", "completed", "cancelled"):
                raise ValueError("invalid_measurement_status")
            if session.measurement_status not in ("completed", "cancelled"):
                vals = {"measurement_status": measurement_status}
                if measurement_status == "measuring":
                    vals["started_at"] = session.started_at or reported_at
                elif measurement_status == "completed":
                    vals["completed_at"] = reported_at
                elif measurement_status == "cancelled":
                    vals["cancelled_at"] = reported_at
                session.with_context(measurement_sync=True).write(vals)
            runs = data.get("runs")
            if not isinstance(runs, list):
                raise ValueError("invalid_payload")
            Run = self.env["nsp.measurement.run"].sudo()
            for item in runs:
                if not isinstance(item, dict):
                    raise ValueError("invalid_payload")
                self._measurement_reject_unknown_fields(item, {
                    "measurement_run_uid", "run_status",
                    "started_at", "stopped_at", "measurement_count",
                })
                self._measurement_require_fields(item, [
                    "measurement_run_uid", "run_status", "measurement_count",
                ])
                uid = str(item.get("measurement_run_uid") or "").strip().upper()
                run = Run.search([("measurement_run_uid", "=", uid), ("session_id", "=", session.id)], limit=1)
                status = str(item.get("run_status") or "").strip().lower()
                if status not in ("pending", "starting", "running", "stopping", "stopped", "failed"):
                    raise ValueError("invalid_payload")
                try:
                    count = max(int(item.get("measurement_count") or 0), 0)
                except Exception:
                    raise ValueError("invalid_payload")
                vals = {"run_status": status, "measurement_count": count}
                if item.get("started_at"):
                    vals["started_at"] = self._measurement_datetime(item.get("started_at"), required=True)
                if item.get("stopped_at"):
                    vals["stopped_at"] = self._measurement_datetime(item.get("stopped_at"), required=True)
                if run:
                    vals["measurement_count"] = max(count, int(run.measurement_count or 0))
                    run.write(vals)
                else:
                    vals.update({"measurement_run_uid": uid, "session_id": session.id})
                    Run.with_context(measurement_sync=True).create(vals)
            return self._ok(message="Measurement Session status synchronized.")
        except Exception as exc:
            return self._measurement_error_response(exc)

    @api.model
    def _upsert_parking_transaction_sync(self, edge_server, item):
        if not isinstance(item, dict):
            raise ValueError("invalid_payload")
        allowed_fields = {
            "transaction_uid", "controller_code", "parking_area_code", "lane_code",
            "direction", "check_time", "vehicle_tid", "user_tid",
            "vehicle_code", "user_code", "decision", "decision_reason_code",
        }
        unsupported = sorted(set(item) - allowed_fields)
        if unsupported:
            raise ValueError("invalid_payload: unsupported field(s): %s" % ", ".join(unsupported))
        uid = str(item.get("transaction_uid") or "").strip()
        if not uid:
            raise ValueError("missing_transaction_uid")
        controller_code = str(item.get("controller_code") or "").strip()
        if not controller_code:
            raise ValueError("missing_controller_code")
        Controller = self.env["nsp.controller"].sudo()
        controller = Controller.search([
            ("controller_id", "=", controller_code),
            ("edge_server_id", "=", edge_server.id),
        ], limit=1)
        if not controller:
            raise ValueError("route_not_allowed")
        parking_area_code = str(item.get("parking_area_code") or "").strip().upper()
        lane_code = str(item.get("lane_code") or "").strip().upper()
        parking_area = self.env["nsp.parking.area"].sudo().search([
            ("code", "=", parking_area_code),
            ("lane_ids.controller_id", "=", controller.id),
        ], limit=1)
        if not parking_area:
            raise ValueError("parking_area_not_found")
        lane = self.env["nsp.parking.lane"].sudo().search([
            ("parking_area_id", "=", parking_area.id),
            ("controller_id", "=", controller.id),
            ("code", "=", lane_code),
            ("active", "=", True),
        ], limit=1)
        if not lane:
            raise ValueError("lane_not_found")
        check_time = self._safe_datetime_value(item.get("check_time"), default_now=False)
        if not check_time:
            raise ValueError("check_time is required")
        direction = str(item.get("direction") or "").strip().lower()
        if direction not in ("entry", "exit"):
            raise ValueError("invalid_direction")
        if lane.lane_type == "one_way" and direction != lane.direction:
            raise ValueError("invalid_direction")
        decision = str(item.get("decision") or "").strip().lower()
        if decision not in ("allowed", "denied"):
            raise ValueError("invalid_payload")
        Log = self.env["nsp.parking.transaction"].sudo()
        vehicle_tid = str(item.get("vehicle_tid") or "").strip()
        user_tid = str(item.get("user_tid") or "").strip()
        vehicle_code = str(item.get("vehicle_code") or "").strip()
        user_code = str(item.get("user_code") or "").strip()
        vehicle = Log._find_vehicle({"vehicle_code": vehicle_code, "vehicle_tid": vehicle_tid})
        User = self.env["nsp.user"].sudo()
        user = User.search([("user_code", "=", user_code)], limit=1) if user_code and "user_code" in User._fields else User.browse()
        reason_code = Log._normalize_error_code(item.get("decision_reason_code"))
        if decision == "denied" and not reason_code:
            reason_code = "unknown"
        if decision == "allowed" and item.get("decision_reason_code"):
            raise ValueError("invalid_payload")
        vals = {
            "controller_id": controller.id,
            "parking_area_id": parking_area.id,
            "parking_area_code": parking_area.code,
            "lane_id": lane.id,
            "lane_code": lane.code,
            "transaction_uid": uid,
            "time_entered": check_time,
            "direction": direction,
            "status": decision,
            "vehicle_id": vehicle.id if vehicle else False,
            "vehicle_code": vehicle_code or False,
            "license_plate": (vehicle.license_plate if vehicle and "license_plate" in vehicle._fields else False),
            "vehicle_tid": vehicle_tid or False,
            "user_id": user.id if user else False,
            "user_code": user_code or False,
            "user_tid": user_tid or False,
            "error_code": reason_code or False,
        }
        payload_hash = Log._normalized_payload_hash(controller, vals)
        vals["payload_hash"] = payload_hash
        existing = Log.search([("transaction_uid", "=", uid)], limit=1)
        if existing:
            if existing.payload_hash and existing.payload_hash != payload_hash:
                raise ValueError("sync_uid_conflict")
            if not existing.payload_hash:
                existing.write({"payload_hash": payload_hash})
            return existing, True
        try:
            with self.env.cr.savepoint():
                record = Log.create(vals)
            return record, False
        except IntegrityError:
            existing = Log.search([("transaction_uid", "=", uid)], limit=1)
            if not existing:
                raise
            if existing.payload_hash and existing.payload_hash != payload_hash:
                raise ValueError("sync_uid_conflict")
            if not existing.payload_hash:
                existing.write({"payload_hash": payload_hash})
            return existing, True

    @endpoint("NSP Gatekeeper Parking Transactions Sync", route_suffix="parking-transactions/sync", methods="POST", code="nsp_gatekeeper_parking_transactions_sync")
    def api_parking_transactions_sync(self):
        data = self._payload()
        _application, _actor, edge_server, error = self._auth_edge_server_sync(data)
        if error:
            return error
        incoming = data.get("items")
        if isinstance(incoming, dict):
            incoming = [incoming]
        if not isinstance(incoming, list) or not incoming:
            return self._error("items must contain at least one transaction", 400, error_code="invalid_payload", details={"field": "items"})
        results = []
        processed = failed = 0
        for idx, item in enumerate(incoming):
            key = str(item.get("transaction_uid") or "").strip() if isinstance(item, dict) else ""
            try:
                with self.env.cr.savepoint():
                    rec, duplicate = self._upsert_parking_transaction_sync(edge_server, item)
                result = {
                    "index": idx,
                    "record_key": rec.transaction_uid,
                    "status": "duplicate" if duplicate else "processed",
                    "message": "Already processed" if duplicate else "Processed",
                }
                if rec.status == "denied":
                    result.update({
                        "business_decision": "denied",
                        "decision_reason_code": rec.error_code or "unknown",
                    })
                results.append(result)
                if not duplicate:
                    processed += 1
            except Exception as exc:
                failed += 1
                results.append({"index": idx, "record_key": key, "status": "rejected", "message": str(exc)})
        return self._ok({
            "received": len(incoming),
            "processed": processed,
            "failed": failed,
            "results": results,
        }, message="Parking transactions synced.")

    @endpoint("NSP Gatekeeper Parking Logs Push", route_suffix="parking/logs/push", methods="POST", code="nsp_gatekeeper_parking_logs_push")
    def api_parking_logs_push(self):
        data = self._payload()
        controller, error = self._auth_controller(data)
        if error:
            return error
        logs = data.get("logs") or data.get("items") or []
        if isinstance(logs, dict):
            logs = [logs]
        if not isinstance(logs, list):
            return self._error("logs must be an array", 400, error_code="invalid_payload", details={"field": "logs"})
        results = []
        processed = failed = 0
        Log = self.env["nsp.parking.transaction"].sudo()
        for idx, item in enumerate(logs):
            key = str(item.get("transaction_uid") or "").strip() if isinstance(item, dict) else ""
            try:
                with self.env.cr.savepoint():
                    rec, duplicate = Log.ingest_controller_log(controller, item)
                result = {
                    "index": idx,
                    "record_key": rec.transaction_uid,
                    "status": "duplicate" if duplicate else "processed",
                    "message": "Already processed" if duplicate else "Processed",
                }
                if rec.status == "denied":
                    result.update({"business_decision": "denied", "decision_reason_code": rec.error_code or "unknown"})
                results.append(result)
                if not duplicate:
                    processed += 1
            except Exception as exc:
                failed += 1
                results.append({"index": idx, "record_key": key, "status": "rejected", "message": str(exc)})
        controller.write({"timestamp": fields.Datetime.now(), "status": "online"})
        return self._ok({"received": len(logs), "processed": processed, "failed": failed, "results": results}, message="Parking logs accepted.")

