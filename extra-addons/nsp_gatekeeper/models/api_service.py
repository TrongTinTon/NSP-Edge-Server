# -*- coding: utf-8 -*-
"""NSP Gatekeeper Core API service endpoints.

All runtime controller-facing APIs are exposed through T4 Core API
Action Endpoints instead of direct @http.route aliases.
"""

import base64
import json
import logging
from datetime import datetime, timezone

from odoo import api, fields, models

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

        Internal database IDs are deliberately rejected.
        """
        data = data or {}
        return str(data.get("controller_code") or "").strip()

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

        # Authentication only resolves and authorizes the Controller. Runtime
        # liveness is owned by the dedicated heartbeat/status APIs so high-volume
        # detection requests do not write Controller state for every detected TID.
        return self.env["nsp.controller"].sudo().browse(controller.id), None

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
    def _safe_positive_int(self, value, default=1):
        try:
            parsed = int(value)
            return parsed if parsed > 0 else default
        except Exception:
            return default

    @api.model
    def _user_code(self, user):
        return str(user.user_code or "").strip() if user else ""

    # ------------------------------------------------------------------
    # Runtime Core API endpoints
    # ------------------------------------------------------------------
    @endpoint("NSP Gatekeeper Health", route_path="health", methods="GET", code="nsp_gatekeeper_health")
    def api_health(self):
        return self._ok({
            "service": "nsp_gatekeeper",
            "status": "running",
            "server_time": self._iso_datetime(fields.Datetime.now()),
        }, message="NSP Gatekeeper is running.")

    @endpoint("NSP Gatekeeper Heartbeat", route_path="heartbeat", methods="POST", code="nsp_gatekeeper_heartbeat")
    def api_controller_heartbeat(self):
        data = self._payload()
        controller, error = self._auth_controller(data)
        if error:
            return error
        now = fields.Datetime.now()
        if controller.status != "online":
            controller.write({"timestamp": now, "status": "online"})
        else:
            self.env.cr.execute(
                "UPDATE nsp_controller SET timestamp = %s WHERE id = %s",
                (now, controller.id),
            )
            controller.invalidate_recordset(["timestamp"])
        return self._ok({
            "controller_code": controller.controller_id,
            "current_status": "online",
            "last_seen_at": self._iso_datetime(now),
            "reader_count": self._whitelisted_device_count(controller),
        }, message="Heartbeat accepted.")

    @api.model
    def _edge_server_code_from_payload(self, data=None):
        data = data or {}
        return str(data.get("edge_server_code") or "").strip()

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
        if parent.status != current_status:
            parent.write({"timestamp": last_seen_at, "status": current_status})
        else:
            self.env.cr.execute(
                "UPDATE nsp_edge_server SET timestamp = %s WHERE id = %s",
                (last_seen_at, parent.id),
            )
            parent.invalidate_recordset(["timestamp"])
        return parent

    @api.model
    def _whitelisted_device_count(self, controller):
        """Count configured Readers that are currently whitelisted in one query."""
        if not controller:
            return 0
        self.env.cr.execute(
            """
            SELECT COUNT(*)
              FROM nsp_device AS device
              JOIN nsp_device_whitelist AS whitelist
                ON whitelist.serial_number = device.serial_number
             WHERE device.controller_id = %s
            """,
            (controller.id,),
        )
        row = self.env.cr.fetchone()
        return int(row[0] or 0) if row else 0

    @api.model
    def _whitelisted_devices(self, devices):
        """Return Readers whose Serial exists in Device Whitelist."""
        serials = [str(value or "").strip().upper() for value in devices.mapped("serial_number") if value]
        if not serials:
            return devices.browse()
        allowed = set(self.env["nsp.device.whitelist"].sudo().search([
            ("serial_number", "in", serials),
        ]).mapped("serial_number"))
        return devices.filtered(lambda reader: reader.serial_number in allowed)

    @api.model
    def _device_status_cache(self, controllers, items):
        serials = {
            str(item.get("serial_number") or "").strip().upper()
            for item in (items or []) if isinstance(item, dict)
        }
        serials.discard("")
        controller_ids = controllers.ids if controllers else []
        Device = self.env["nsp.device"].sudo()
        devices = Device.search([
            ("controller_id", "in", controller_ids),
            ("serial_number", "in", list(serials)),
        ]) if controller_ids and serials else Device.browse()
        whitelist = self.env["nsp.device.whitelist"].sudo().search([
            ("serial_number", "in", list(serials)),
        ]) if serials else self.env["nsp.device.whitelist"].browse()
        return {
            "device_by_key": {(device.controller_id.id, device.serial_number): device for device in devices},
            "whitelist_serials": set(whitelist.mapped("serial_number")),
        }

    @api.model
    def _apply_device_status(self, controller, item, cache=None):
        """Apply Reader runtime status using Serial Number as the only device identity.

        ``device_code`` is a server-side management code and is never accepted
        from Controllers or Edge Server runtime reports. Antenna declarations are
        server-managed; a runtime report may include antenna numbers only as an
        inventory assertion.
        """
        if not isinstance(item, dict):
            raise ValueError("invalid_payload")
        allowed_fields = {
            "serial_number", "antennas", "device_status",
            "last_seen_at", "firmware_version",
        }
        unsupported = sorted(set(item) - allowed_fields)
        if unsupported:
            raise ValueError("unsupported_field:%s" % ",".join(unsupported))
        serial_number = str(item.get("serial_number") or "").strip().upper()
        if not serial_number:
            raise ValueError("serial_number is required")
        cache = cache or self._device_status_cache(controller, [item])
        if serial_number not in cache.get("whitelist_serials", set()):
            raise ValueError("device_not_whitelisted")
        device = cache.get("device_by_key", {}).get((controller.id, serial_number))
        if not device:
            raise ValueError("device_not_found")
        status = str(item.get("device_status") or "online").strip().lower()
        if status not in ("online", "offline", "degraded"):
            raise ValueError("invalid_payload")

        reported_antennas = item.get("antennas")
        if reported_antennas is not None:
            if not isinstance(reported_antennas, list):
                raise ValueError("antennas must be an array")
            try:
                reported_numbers = {int(value) for value in reported_antennas}
            except Exception as exc:
                raise ValueError("invalid_antenna_number") from exc
            if any(number <= 0 for number in reported_numbers):
                raise ValueError("invalid_antenna_number")
            declared_numbers = set(device.antennas_ids.mapped("antenna_no"))
            if reported_numbers != declared_numbers:
                raise ValueError("antenna_inventory_mismatch")

        last_seen_at = self._safe_datetime_value(item.get("last_seen_at"), default_now=False)
        vals = {"status": status}
        if last_seen_at:
            vals["last_seen"] = last_seen_at
        elif status == "online":
            vals["last_seen"] = fields.Datetime.now()
        if item.get("firmware_version") not in (None, ""):
            vals["firmware_version"] = str(item.get("firmware_version"))
        device.write(vals)
        return device

    @endpoint("NSP Gatekeeper Edge Server Status", route_path="edge-server/status", methods="POST", code="nsp_gatekeeper_edge_server_status")
    def api_edge_server_status(self):
        """Accept one Edge heartbeat including its Controllers and Reader runtime inventory."""
        data = self._payload()
        _application, _actor, edge_server, error = self._auth_edge_server_sync(data)
        if error:
            return error
        heartbeat_data = dict(data)
        heartbeat_data["_heartbeat_received"] = True
        heartbeat_data.setdefault("current_status", "online")
        self._update_edge_server_status_from_payload(edge_server, heartbeat_data)

        controller_items = data.get("controllers") or []
        if not isinstance(controller_items, list):
            return self._error(
                "controllers must be an array", 400, error_code="invalid_payload",
                details={"field": "controllers"},
            )

        Controller = self.env["nsp.controller"].sudo().with_context(active_test=False)
        controller_codes = {
            str(item.get("controller_code") or "").strip().upper()
            for item in controller_items if isinstance(item, dict)
        }
        controller_codes.discard("")
        controllers = Controller.search([
            ("controller_id", "in", list(controller_codes)),
            ("edge_server_id", "=", edge_server.id),
        ]) if controller_codes else Controller.browse()
        controller_by_code = {record.controller_id: record for record in controllers}
        reported_device_items = [
            device_item
            for controller_item in controller_items if isinstance(controller_item, dict)
            for device_item in (controller_item.get("devices") or [])
            if isinstance(device_item, dict)
        ]
        device_cache = self._device_status_cache(controllers, reported_device_items)

        results = []
        reported_controller_ids = set()
        controller_count = device_count = failed = 0
        controllers_marked_offline = devices_marked_offline = 0
        for controller_index, controller_item in enumerate(controller_items):
            controller_code = ""
            try:
                if not isinstance(controller_item, dict):
                    raise ValueError("invalid_controller_payload")
                controller_code = str(controller_item.get("controller_code") or "").strip().upper()
                if not controller_code:
                    raise ValueError("missing_controller_code")
                controller = controller_by_code.get(controller_code)
                if not controller:
                    raise ValueError("controller_not_found")

                controller_status = str(controller_item.get("current_status") or "online").strip().lower()
                if controller_status not in ("online", "offline", "error", "block", "revoked"):
                    raise ValueError("invalid_controller_status")
                controller_seen = self._safe_datetime_value(
                    controller_item.get("last_seen_at"), default_now=False
                )
                controller_values = {"status": controller_status}
                if controller_seen:
                    controller_values["timestamp"] = controller_seen
                elif controller_status == "online":
                    controller_values["timestamp"] = fields.Datetime.now()
                controller.write(controller_values)
                reported_controller_ids.add(controller.id)
                controller_count += 1

                devices = controller_item.get("devices") or []
                if not isinstance(devices, list):
                    raise ValueError("devices must be an array")
                reported_serials = {
                    str(item.get("serial_number") or "").strip().upper()
                    for item in devices if isinstance(item, dict)
                    and str(item.get("serial_number") or "").strip()
                }
                for device_index, device_item in enumerate(devices):
                    serial_number = str(
                        device_item.get("serial_number") or ""
                    ).strip().upper() if isinstance(device_item, dict) else ""
                    try:
                        device = self._apply_device_status(controller, device_item, cache=device_cache)
                        device_count += 1
                        results.append({
                            "controller_index": controller_index,
                            "device_index": device_index,
                            "controller_code": controller_code,
                            "record_key": device.serial_number,
                            "status": "processed",
                            "message": "Processed",
                        })
                    except Exception as exc:
                        failed += 1
                        results.append({
                            "controller_index": controller_index,
                            "device_index": device_index,
                            "controller_code": controller_code,
                            "record_key": serial_number,
                            "status": "rejected",
                            "message": str(exc),
                        })

                missing_devices = controller.device_ids.filtered(
                    lambda record: record.serial_number not in reported_serials
                    and record.status != "offline"
                )
                if missing_devices:
                    devices_marked_offline += len(missing_devices)
                    missing_devices.write({"status": "offline"})
            except Exception as exc:
                failed += 1
                results.append({
                    "controller_index": controller_index,
                    "controller_code": controller_code,
                    "record_key": controller_code,
                    "status": "rejected",
                    "message": str(exc),
                })

        missing_controllers = edge_server.controller_ids.filtered(
            lambda record: record.active
            and record.id not in reported_controller_ids
            and record.status not in ("offline", "block", "revoked")
        )
        if missing_controllers:
            controllers_marked_offline = len(missing_controllers)
            missing_controllers.write({"status": "offline"})
            missing_devices = missing_controllers.mapped("device_ids").filtered(
                lambda record: record.status != "offline"
            )
            if missing_devices:
                devices_marked_offline += len(missing_devices)
                missing_devices.write({"status": "offline"})

        return self._ok({
            "edge_server_code": edge_server.edge_server_code,
            "current_status": edge_server.status,
            "last_seen_at": self._iso_datetime(edge_server.timestamp),
            "controllers_processed": controller_count,
            "devices_processed": device_count,
            "controllers_marked_offline": controllers_marked_offline,
            "devices_marked_offline": devices_marked_offline,
            "failed": failed,
            "results": results,
            "server_time": self._iso_datetime(fields.Datetime.now()),
        }, message="Edge Server status and managed device runtime accepted.")

    @endpoint("NSP Gatekeeper Devices Report", route_path="devices/report", methods="POST", code="nsp_gatekeeper_devices_report")
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
        device_cache = self._device_status_cache(controller, items)
        for index, item in enumerate(items):
            key = str(item.get("serial_number") or "").strip() if isinstance(item, dict) else ""
            try:
                device = self._apply_device_status(controller, item, cache=device_cache)
                processed += 1
                results.append({"index": index, "record_key": device.serial_number, "status": "processed", "message": "Processed"})
            except Exception as exc:
                failed += 1
                results.append({"index": index, "record_key": key, "status": "rejected", "message": str(exc)})
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

    def _card_sync_payload(self, card, user_line=False, vehicle_line=False):
        """Serialize one Master Card using preloaded active assignments."""
        if user_line and vehicle_line:
            _logger.error(
                "Card %s has simultaneous active User and Vehicle assignments; "
                "Vehicle assignment is selected for sync.", card.tid,
            )

        assignment = {"type": "unassigned", "code": False}
        card_type = card.card_type
        assigned_at = False
        if vehicle_line:
            vehicle = vehicle_line.vehicle_id
            assignment = {
                "type": "vehicle",
                "code": vehicle.vehicle_code or "",
            }
            card_type = "vehicle_card"
            assigned_at = vehicle_line.assigned_at
        elif user_line:
            user = user_line.user_id
            assignment = {
                "type": "user",
                "code": self._user_code(user),
            }
            card_type = "user_card"
            assigned_at = user_line.assigned_at

        payload = {
            "card_uid": card.tid,
            "card_type": card_type,
            "assignment": assignment,
        }
        if assigned_at:
            payload["assigned_at"] = self._iso_datetime(assigned_at)
        return payload

    @endpoint("NSP Device Whitelist Sync", route_path="device-whitelist/sync", methods="POST", code="nsp_device_whitelist_sync")
    def api_device_whitelist_sync(self):
        data = self._payload()
        _application, _actor_kind, _edge_server, error = self._auth_edge_server_sync(data)
        if error:
            return error
        unsupported = sorted(set(data) - {"edge_server_code"})
        if unsupported:
            return self._error(
                "Unsupported field(s): %s" % ", ".join(unsupported),
                400,
                error_code="invalid_payload",
                details={"unsupported_fields": unsupported},
            )
        Whitelist = self.env["nsp.device.whitelist"].sudo()
        records = Whitelist.search([], order="serial_number asc, id asc")
        items = [{
            "serial_number": record.serial_number,
            "model_number": record.model_number or "",
            "vendor": record.device_vendor or "",
            "device_type": record.device_type,
        } for record in records]
        return self._ok({
            "items": items,
            "next_sync_cursor": False,
            "has_more": False,
            "server_time": self._iso_datetime(fields.Datetime.now()),
        }, message="Device Whitelist snapshot loaded.")

    @endpoint("NSP Vehicle Configuration Sync", route_path="vehicle-config/sync", methods="POST", code="nsp_vehicle_config_sync")
    def api_vehicle_config_sync(self):
        data = self._payload()
        _application, _actor_kind, _edge_server, error = self._auth_edge_server_sync(data)
        if error:
            return error
        unsupported = sorted(set(data) - {"edge_server_code"})
        if unsupported:
            return self._error(
                "Unsupported field(s): %s" % ", ".join(unsupported),
                400,
                error_code="invalid_payload",
                details={"unsupported_fields": unsupported},
            )
        VehicleType = self.env["nsp.vehicle.type"].sudo().with_context(active_test=False)
        VehicleBrand = self.env["nsp.vehicle.brand"].sudo().with_context(active_test=False)
        VehicleModel = self.env["nsp.vehicle.model"].sudo().with_context(active_test=False)
        VehicleColor = self.env["nsp.vehicle.color"].sudo().with_context(active_test=False)
        vehicle_types = VehicleType.search([], order="code asc, id asc")
        brands = VehicleBrand.search([], order="code asc, id asc")
        models_data = VehicleModel.search([], order="code asc, id asc")
        colors = VehicleColor.search([], order="code asc, id asc")
        return self._ok({
            "vehicle_types": [{
                "code": record.code,
                "name": record.name,
                "active": bool(record.active),
            } for record in vehicle_types],
            "brands": [{
                "code": record.code,
                "name": record.name,
                "active": bool(record.active),
            } for record in brands],
            "models": [{
                "code": record.code,
                "name": record.name,
                "brand_code": record.brand_id.code if record.brand_id else False,
                "active": bool(record.active),
            } for record in models_data],
            "colors": [{
                "code": record.code,
                "name": record.name,
                "active": bool(record.active),
            } for record in colors],
            "next_sync_cursor": False,
            "has_more": False,
            "server_time": self._iso_datetime(fields.Datetime.now()),
        }, message="Vehicle Configuration snapshot loaded.")

    @endpoint("NSP Gatekeeper Cards Sync", route_path="cards/sync", methods="POST", code="nsp_gatekeeper_cards_sync")
    def api_cards_sync(self):
        data = self._payload()
        _application, _actor_kind, _edge_server, error = self._auth_edge_server_sync(data)
        if error:
            return error
        unsupported = sorted(set(data) - {"edge_server_code"})
        if unsupported:
            return self._error(
                "Unsupported field(s): %s" % ", ".join(unsupported),
                400,
                error_code="invalid_payload",
                details={"unsupported_fields": unsupported},
            )
        cards = self.env["nsp.rfid.card"].sudo().search([], order="tid asc, id asc")
        card_ids = cards.ids
        user_by_card = {}
        vehicle_by_card = {}
        if card_ids:
            user_lines = self.env["nsp.user.card"].sudo().search([
                ("card_id", "in", card_ids), ("state", "=", "active"),
            ], order="assigned_at desc, id desc")
            vehicle_lines = self.env["nsp.vehicle.card"].sudo().search([
                ("card_id", "in", card_ids), ("state", "=", "active"),
            ], order="assigned_at desc, id desc")
            for line in user_lines:
                user_by_card.setdefault(line.card_id.id, line)
            for line in vehicle_lines:
                vehicle_by_card.setdefault(line.card_id.id, line)
        items = [
            self._card_sync_payload(
                card,
                user_line=user_by_card.get(card.id),
                vehicle_line=vehicle_by_card.get(card.id),
            )
            for card in cards
        ]
        user_card_count = sum(
            1 for item in items
            if (item.get("assignment") or {}).get("type") == "user"
        )
        vehicle_card_count = sum(
            1 for item in items
            if (item.get("assignment") or {}).get("type") == "vehicle"
        )
        unassigned_count = len(items) - user_card_count - vehicle_card_count
        return self._ok({
            "items": items,
            "summary": {
                "master_cards": len(items),
                "user_cards": user_card_count,
                "vehicle_cards": vehicle_card_count,
                "unassigned_cards": unassigned_count,
            },
            "next_sync_cursor": False,
            "has_more": False,
            "server_time": self._iso_datetime(fields.Datetime.now()),
        }, message="Cards snapshot loaded.")

    @endpoint("NSP Gatekeeper Users Sync", route_path="users/sync", methods="POST", code="nsp_gatekeeper_users_sync")
    def api_users_sync(self):
        data = self._payload()
        application, actor_kind, edge_server, error = self._auth_edge_server_sync(data)
        if error:
            return error
        User = self.env["nsp.user"].sudo()
        users, next_cursor, has_more, server_time = self._cursor_page(
            User, data, domain=[("user_code", "!=", False), ("user_code", "!=", "")]
        )
        items = []
        for user in users:
            item = {
                "user_code": self._user_code(user),
                "name": user.name or user.display_name,
                "active": bool(user.active),
            }
            items.append(item)
        return self._ok({
            "items": items, "next_sync_cursor": next_cursor, "has_more": has_more,
            "server_time": self._iso_datetime(server_time),
        }, message="Users sync loaded.")

    @endpoint("NSP Gatekeeper Vehicles Sync", route_path="vehicles/sync", methods="POST", code="nsp_gatekeeper_vehicles_sync")
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
            vehicle_code = vehicle.vehicle_code or ""
            item = {
                "vehicle_code": vehicle_code,
                "license_plate": vehicle.license_plate or "",
                "vehicle_type_code": vehicle.vehicle_type_id.code if vehicle.vehicle_type_id else False,
                "brand_code": vehicle.brand_id.code if vehicle.brand_id else False,
                "model_code": vehicle.model_id.code if vehicle.model_id else False,
                "color_code": vehicle.color_id.code if vehicle.color_id else False,
                "active": bool(vehicle.active),
            }
            owner_user_code = self._user_code(owner)
            if owner_user_code:
                item["owner_user_code"] = owner_user_code
            items.append(item)
        return self._ok({
            "items": items, "next_sync_cursor": next_cursor, "has_more": has_more,
            "server_time": self._iso_datetime(server_time),
        }, message="Vehicles sync loaded.")

    @endpoint("NSP Gatekeeper Branches Sync", route_path="branches/sync", methods="POST", code="nsp_gatekeeper_branches_sync")
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

    @endpoint("NSP Gatekeeper Vehicle Borrow Sync", route_path="vehicle-borrow/sync", methods="POST", code="nsp_gatekeeper_vehicle_borrow_sync")
    def api_vehicle_borrow_sync(self):
        data = self._payload()
        application, actor_kind, edge_server, error = self._auth_edge_server_sync(data)
        if error:
            return error
        if "nsp.vehicle.borrow" not in self.env.registry.models:
            return self._ok({"items": [], "next_sync_cursor": data.get("sync_cursor") or False, "has_more": False, "server_time": self._iso_datetime(fields.Datetime.now())})
        Borrow = self.env["nsp.vehicle.borrow"].sudo()
        records, next_cursor, has_more, server_time = self._cursor_page(Borrow, data)
        items = []
        for borrow in records:
            vehicle = borrow.vehicle_id
            borrower = borrow.borrower_id
            borrow_uid = borrow.borrow_code or ""
            vehicle_code = vehicle.vehicle_code if vehicle else ""
            item = {
                "borrow_uid": borrow_uid,
                "vehicle_code": vehicle_code,
                "borrower_user_code": self._user_code(borrower),
                "state": borrow.state,
            }
            if borrow.valid_from:
                item["valid_from"] = self._iso_datetime(borrow.valid_from)
            if borrow.valid_to:
                item["valid_to"] = self._iso_datetime(borrow.valid_to)
            if borrow.returned_at:
                item["returned_at"] = self._iso_datetime(borrow.returned_at)
            items.append(item)
        return self._ok({
            "items": items, "next_sync_cursor": next_cursor, "has_more": has_more,
            "server_time": self._iso_datetime(server_time),
        }, message="Vehicle borrow sync loaded.")

    @endpoint("NSP Parking Operation Configuration Sync", route_path="parking-config/sync", methods="POST", code="nsp_parking_config_sync")
    def api_parking_config_sync(self):
        data = self._payload()
        _application, _actor_kind, _edge_server, error = self._auth_edge_server_sync(data)
        if error:
            return error
        unsupported = sorted(set(data) - {"edge_server_code"})
        if unsupported:
            return self._error(
                "Unsupported field(s): %s" % ", ".join(unsupported),
                400,
                error_code="invalid_payload",
                details={"unsupported_fields": unsupported},
            )
        ParkingOperation = self.env["nsp.parking.area"].sudo()
        records = ParkingOperation.search([], order="branch_id, code, id")
        return self._ok({
            "items": [record.prepare_sync_payload() for record in records],
            "next_sync_cursor": False,
            "has_more": False,
            "server_time": self._iso_datetime(fields.Datetime.now()),
        }, message="Parking operation configuration snapshot loaded.")

    @endpoint("NSP Controller Device Configuration Pull", route_path="controller/device-config/pull", methods="POST", code="nsp_controller_device_config_pull")
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
        devices = self._whitelisted_devices(controller.device_ids).sorted(
            key=lambda rec: (rec.serial_number or "", rec.id)
        )
        return self._ok({
            "controller_code": controller.controller_id,
            "devices": [device._build_config_payload() for device in devices],
            "server_time": self._iso_datetime(fields.Datetime.now()),
        }, message="Controller device configuration loaded.")

    # ------------------------------------------------------------------
    # Measurement Session / Event APIs
    # ------------------------------------------------------------------
    @api.model
    def _measurement_input(self):
        data = self._payload()
        if str(self.env.context.get("core_api_method") or "").upper() == "GET":
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
    def _measurement_session(self, measurement_code):
        code = str(measurement_code or "").strip().upper()
        if not code:
            raise ValueError("missing_measurement_code")
        session = self.env["nsp.measurement.session"].sudo().search(
            [("measurement_code", "=", code)], limit=1
        )
        if not session:
            raise ValueError("measurement_session_not_found")
        return session

    @api.model
    def _measurement_session_in_local_scope(self, session, edge_server):
        return bool(session.controller_id and session.controller_id.edge_server_id == edge_server)

    @api.model
    def _measurement_antenna_payload(self, session):
        grouped = {}
        for antenna in session.antenna_ids.sorted(
            key=lambda item: (
                item.device_id.serial_number or "",
                item.antenna_no or 0,
                item.id,
            )
        ):
            grouped.setdefault(antenna.device_id.serial_number, []).append(int(antenna.antenna_no))
        return [
            {"serial_number": serial_number, "antennas": sorted(set(numbers))}
            for serial_number, numbers in sorted(grouped.items())
        ]

    @api.model
    def _measurement_config_payload(self, session):
        payload = {
            "measurement_code": session.measurement_code,
            "controller_code": session.controller_id.controller_id,
            "status": session.status,
            "desired_state": "running" if session.status in ("ready", "running") else "stopped",
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
            "event_count": int(session.event_count or 0),
            "created_at": self._iso_datetime(session.create_date),
        })
        if session.started_at:
            payload["started_at"] = self._iso_datetime(session.started_at)
        if session.ended_at:
            payload["ended_at"] = self._iso_datetime(session.ended_at)
        if include_detail:
            payload["antenna_summary"] = [
                {
                    **row,
                    "first_read_at": self._iso_datetime(row.get("first_read_at")),
                    "last_read_at": self._iso_datetime(row.get("last_read_at")),
                }
                for row in session._antenna_summary()
            ]
            payload["transition_summary"] = session._transition_summary()
        return payload

    @api.model
    def _measurement_resolve_controller(self, data, current_session=False):
        controller_code = str(
            data.get("controller_code")
            or (current_session.controller_id.controller_id if current_session else "")
        ).strip()
        self._measurement_require_fields({"controller_code": controller_code}, ["controller_code"])
        controller = self.env["nsp.controller"].sudo().with_context(active_test=False).search(
            [("controller_id", "=", controller_code)], limit=1
        )
        if not controller:
            raise ValueError("controller_not_found")
        return controller

    @api.model
    def _measurement_resolve_antennas(self, controller, values):
        if not isinstance(values, list) or not values:
            raise ValueError("missing_measurement_antennas")

        keys = set()
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
                if key in keys:
                    raise ValueError("duplicate_antenna_mapping")
                keys.add(key)

        serials = {serial for serial, _antenna_no in keys}
        whitelisted = set(
            self.env["nsp.device.whitelist"].sudo().search([
                ("serial_number", "in", list(serials)),
            ]).mapped("serial_number")
        ) if serials else set()
        missing_whitelist = serials - whitelisted
        if missing_whitelist:
            raise ValueError("device_not_whitelisted")

        antenna_numbers = {antenna_no for _serial, antenna_no in keys}
        antennas = self.env["nsp.device.antenna"].sudo().search([
            ("device_id.controller_id", "=", controller.id),
            ("device_id.serial_number", "in", list(serials)),
            ("antenna_no", "in", list(antenna_numbers)),
        ])
        by_key = {
            (antenna.device_id.serial_number, int(antenna.antenna_no or 0)): antenna
            for antenna in antennas
        }
        if any(key not in by_key for key in keys):
            raise ValueError("antenna_not_found")
        return self.env["nsp.device.antenna"].browse([by_key[key].id for key in sorted(keys)])

    @api.model
    def _measurement_error_response(self, exc):
        text = str(exc)
        code = text.split(":", 1)[0].strip()
        status = 400
        if code.endswith("_not_found") or code in {"controller_not_found", "antenna_not_found"}:
            status = 404
        elif code in {"controller_not_in_scope", "edge_server_not_in_scope", "route_not_allowed"}:
            status = 403
        elif code in {
            "measurement_session_not_editable",
            "invalid_status_transition",
            "event_uid_conflict",
            "measurement_not_running",
        }:
            status = 409
        return self._error(text.replace("_", " "), status, error_code=code, details={})

    @api.model
    def _measurement_set_status(self, session, status, occurred_at=False, message=False):
        target = str(status or "").strip().lower()
        allowed_statuses = {"draft", "ready", "running", "completed", "failed", "cancelled"}
        if target not in allowed_statuses:
            raise ValueError("invalid_measurement_status")
        current = session.status
        transitions = {
            "draft": {"ready", "cancelled"},
            "ready": {"running", "completed", "failed", "cancelled"},
            "running": {"completed", "failed", "cancelled"},
            "completed": set(),
            "failed": set(),
            "cancelled": set(),
        }
        if target != current and target not in transitions.get(current, set()):
            raise ValueError("invalid_status_transition")
        when = occurred_at or fields.Datetime.now()
        vals = {}
        if target != current:
            vals["status"] = target
        if target == "running" and not session.started_at:
            vals["started_at"] = when
        if target in ("completed", "failed", "cancelled") and not session.ended_at:
            vals["ended_at"] = when
        if vals:
            session.with_context(measurement_sync=True).write(vals)
        if message:
            session.message_post(body=str(message))
        return session

    @endpoint("NSP Measurement Session Create", route_path="measurement-sessions", methods="POST", code="nsp_measurement_session_create")
    def api_measurement_session_create(self):
        data = self._payload()
        _application, _actor, error = self._auth_sync_application(data)
        if error:
            return error
        try:
            allowed = {
                "controller_code", "planned_start_at", "planned_end_at",
                "note", "measurement_antennas",
            }
            self._measurement_reject_unknown_fields(data, allowed)
            self._measurement_require_fields(data, ["controller_code", "measurement_antennas"])
            controller = self._measurement_resolve_controller(data)
            antennas = self._measurement_resolve_antennas(controller, data.get("measurement_antennas"))
            planned_start = self._measurement_datetime(data.get("planned_start_at"))
            planned_end = self._measurement_datetime(data.get("planned_end_at"))
            if planned_start and planned_end and planned_end <= planned_start:
                raise ValueError("invalid_planned_time_range")
            session = self.env["nsp.measurement.session"].sudo().create({
                "controller_id": controller.id,
                "planned_start_at": planned_start,
                "planned_end_at": planned_end,
                "note": str(data.get("note") or "").strip() or False,
                "antenna_ids": [(6, 0, antennas.ids)],
            })
            return self._ok({"data": self._measurement_session_payload(session, include_detail=True)}, status_code=201, message="Measurement Session created.")
        except Exception as exc:
            return self._measurement_error_response(exc)

    @endpoint("NSP Measurement Session Update", route_path="measurement-sessions/update", methods="PATCH", code="nsp_measurement_session_update")
    def api_measurement_session_update(self):
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
            self._measurement_require_fields(data, ["measurement_code"])
            session = self._measurement_session(data.get("measurement_code"))
            if session.status != "draft":
                raise ValueError("measurement_session_not_editable")
            controller = self._measurement_resolve_controller(data, current_session=session)
            vals = {}
            if "controller_code" in data:
                vals["controller_id"] = controller.id
            if "planned_start_at" in data:
                vals["planned_start_at"] = self._measurement_datetime(data.get("planned_start_at"))
            if "planned_end_at" in data:
                vals["planned_end_at"] = self._measurement_datetime(data.get("planned_end_at"))
            if "note" in data:
                vals["note"] = str(data.get("note") or "").strip() or False
            if "measurement_antennas" in data:
                antennas = self._measurement_resolve_antennas(controller, data.get("measurement_antennas"))
                vals["antenna_ids"] = [(6, 0, antennas.ids)]
            if vals:
                session.write(vals)
            return self._ok({"data": self._measurement_session_payload(session, include_detail=True)}, message="Measurement Session updated.")
        except Exception as exc:
            return self._measurement_error_response(exc)

    @endpoint("NSP Measurement Session Detail", route_path="measurement-sessions/detail", methods="GET,POST", code="nsp_measurement_session_detail")
    def api_measurement_session_detail(self):
        data = self._measurement_input()
        _application, _actor, error = self._auth_sync_application(data)
        if error:
            return error
        try:
            self._measurement_reject_unknown_fields(data, {"measurement_code"})
            session = self._measurement_session(data.get("measurement_code"))
            return self._ok({"data": self._measurement_session_payload(session, include_detail=True)}, message="Measurement Session loaded.")
        except Exception as exc:
            return self._measurement_error_response(exc)

    @endpoint("NSP Measurement Session State", route_path="measurement-sessions/state", methods="POST", code="nsp_measurement_session_state")
    def api_measurement_session_state(self):
        data = self._payload()
        _application, _actor, error = self._auth_sync_application(data)
        if error:
            return error
        try:
            self._measurement_reject_unknown_fields(data, {"measurement_code", "status", "occurred_at", "message"})
            self._measurement_require_fields(data, ["measurement_code", "status"])
            session = self._measurement_session(data.get("measurement_code"))
            target = str(data.get("status") or "").strip().lower()
            occurred_at = self._measurement_datetime(data.get("occurred_at"), default_now=True)
            if target == "ready":
                if not session.antenna_ids:
                    raise ValueError("missing_measurement_antennas")
                session._check_antenna_scope()
            self._measurement_set_status(session, target, occurred_at, data.get("message"))
            return self._ok({"data": self._measurement_session_payload(session)}, message="Measurement Session state updated.")
        except Exception as exc:
            return self._measurement_error_response(exc)

    @endpoint("NSP Measurement Configuration Sync", route_path="measurement-config/sync", methods="POST", code="nsp_measurement_config_sync")
    def api_measurement_config_sync(self):
        data = self._payload()
        _application, _actor, edge_server, error = self._auth_edge_server_sync(data)
        if error:
            return error
        try:
            self._measurement_reject_unknown_fields(data, {"edge_server_code", "sync_cursor", "limit"})
            Session = self.env["nsp.measurement.session"].sudo()
            records, next_cursor, has_more, server_time = self._cursor_page(
                Session,
                data,
                domain=[
                    ("status", "!=", "draft"),
                    ("controller_id.edge_server_id", "=", edge_server.id),
                ],
            )
            return self._ok({
                "items": [self._measurement_config_payload(session) for session in records],
                "next_sync_cursor": next_cursor,
                "has_more": has_more,
                "server_time": self._iso_datetime(server_time),
            }, message="Measurement configuration sync loaded.")
        except Exception as exc:
            return self._measurement_error_response(exc)

    @endpoint("NSP Controller Measurement Pull", route_path="controller/measurement/pull", methods="POST", code="nsp_controller_measurement_pull")
    def api_controller_measurement_pull(self):
        data = self._payload()
        controller, error = self._auth_controller(data)
        if error:
            return error
        try:
            self._measurement_reject_unknown_fields(data, {"controller_code", "current_measurement_code"})
            current_code = str(data.get("current_measurement_code") or "").strip().upper()
            session = self.env["nsp.measurement.session"].sudo().browse()
            if current_code:
                current = self.env["nsp.measurement.session"].sudo().search([
                    ("measurement_code", "=", current_code),
                    ("controller_id", "=", controller.id),
                ], limit=1)
                if current and current.status in ("completed", "failed", "cancelled"):
                    session = current
            if not session:
                session = self.env["nsp.measurement.session"].sudo().search([
                    ("controller_id", "=", controller.id),
                    ("status", "in", ["ready", "running"]),
                ], order="planned_start_at asc, id asc", limit=1)
            if not session:
                return self._ok({"data": {"measurement_available": False}}, message="No Measurement Session is available.")
            return self._ok({"data": {"measurement_available": True, **self._measurement_config_payload(session)}}, message="Measurement configuration loaded.")
        except Exception as exc:
            return self._measurement_error_response(exc)

    @api.model
    def _measurement_event_values(self, session, item, allowed_antennas=None):
        allowed = {"event_uid", "serial_number", "antenna_no", "tid", "read_at", "rssi_dbm"}
        self._measurement_reject_unknown_fields(item, allowed)
        self._measurement_require_fields(item, ["event_uid", "serial_number", "antenna_no", "tid", "read_at"])
        event_uid = str(item.get("event_uid") or "").strip()
        serial_number = str(item.get("serial_number") or "").strip().upper()
        tid = str(item.get("tid") or "").strip().upper()
        try:
            antenna_no = int(item.get("antenna_no") or 0)
        except Exception:
            antenna_no = 0
        if antenna_no <= 0:
            raise ValueError("antenna_not_found")
        if allowed_antennas is None:
            allowed_antennas = {
                (antenna.device_id.serial_number, int(antenna.antenna_no or 0))
                for antenna in session.antenna_ids
            }
        if (serial_number, antenna_no) not in allowed_antennas:
            raise ValueError("antenna_not_found")
        read_at = self._measurement_datetime(item.get("read_at"), required=True)
        if item.get("rssi_dbm") in (None, ""):
            rssi = False
        else:
            try:
                rssi = float(item.get("rssi_dbm"))
            except Exception:
                raise ValueError("invalid_rssi")
        return {
            "event_uid": event_uid,
            "session_id": session.id,
            "serial_number": serial_number,
            "antenna_no": antenna_no,
            "tid": tid,
            "read_at": read_at,
            "rssi_dbm": rssi,
        }

    @api.model
    def _measurement_event_matches(self, event, values):
        return (
            event.session_id.id == values["session_id"]
            and event.serial_number == values["serial_number"]
            and int(event.antenna_no or 0) == int(values["antenna_no"] or 0)
            and event.tid == values["tid"]
            and fields.Datetime.to_string(event.read_at) == fields.Datetime.to_string(values["read_at"])
            and (False if event.rssi_dbm in (False, None) else float(event.rssi_dbm))
            == (False if values["rssi_dbm"] in (False, None) else float(values["rssi_dbm"]))
        )

    @api.model
    def _measurement_process_event_batch(self, session, items, allow_final=False):
        """Validate/idempotently store one Measurement batch with bounded queries.

        Event UIDs are preloaded once and new valid Events are created in one ORM
        batch. Repeated UIDs inside the same request are treated idempotently when
        their payload is identical and rejected when their payload conflicts.
        """
        Event = self.env["nsp.measurement.event"].sudo()
        allowed_antennas = {
            (antenna.device_id.serial_number, int(antenna.antenna_no or 0))
            for antenna in session.antenna_ids
        }
        prepared = []
        results = [None] * len(items)

        for index, item in enumerate(items):
            key = str(item.get("event_uid") or "") if isinstance(item, dict) else ""
            try:
                if not isinstance(item, dict):
                    raise ValueError("invalid_payload")
                values = self._measurement_event_values(
                    session, item, allowed_antennas=allowed_antennas
                )
                prepared.append((index, key, values))
            except Exception as exc:
                results[index] = {
                    "index": index,
                    "record_key": key,
                    "status": "rejected",
                    "error_code": str(exc).split(":", 1)[0],
                    "message": str(exc),
                }

        event_uids = list({values["event_uid"] for _index, _key, values in prepared})
        existing_by_uid = {
            event.event_uid: event
            for event in Event.search([("event_uid", "in", event_uids)])
        } if event_uids else {}

        first_values_by_uid = {}
        pending_by_uid = {}
        duplicate_indices = {}
        for index, key, values in prepared:
            uid = values["event_uid"]
            existing = existing_by_uid.get(uid)
            if existing:
                if not self._measurement_event_matches(existing, values):
                    results[index] = {
                        "index": index, "record_key": key, "status": "rejected",
                        "error_code": "event_uid_conflict", "message": "event_uid_conflict",
                    }
                else:
                    results[index] = {
                        "index": index, "record_key": key, "status": "duplicate",
                        "message": "Already processed",
                    }
                continue

            first = first_values_by_uid.get(uid)
            if first is not None:
                same = (
                    first["session_id"] == values["session_id"]
                    and first["serial_number"] == values["serial_number"]
                    and int(first["antenna_no"]) == int(values["antenna_no"])
                    and first["tid"] == values["tid"]
                    and fields.Datetime.to_string(first["read_at"]) == fields.Datetime.to_string(values["read_at"])
                    and (False if first["rssi_dbm"] in (False, None) else float(first["rssi_dbm"]))
                    == (False if values["rssi_dbm"] in (False, None) else float(values["rssi_dbm"]))
                )
                if same:
                    duplicate_indices.setdefault(uid, []).append((index, key))
                else:
                    results[index] = {
                        "index": index, "record_key": key, "status": "rejected",
                        "error_code": "event_uid_conflict", "message": "event_uid_conflict",
                    }
                continue

            if not allow_final and session.status in ("completed", "failed", "cancelled"):
                results[index] = {
                    "index": index, "record_key": key, "status": "rejected",
                    "error_code": "measurement_not_running", "message": "measurement_not_running",
                }
                continue

            first_values_by_uid[uid] = values
            pending_by_uid[uid] = (index, key, values)

        created_records = Event.browse()
        pending = list(pending_by_uid.values())
        if pending:
            try:
                with self.env.cr.savepoint():
                    created_records = Event.create([values for _index, _key, values in pending])
                created_by_uid = {event.event_uid: event for event in created_records}
                for uid, (index, key, _values) in pending_by_uid.items():
                    if uid in created_by_uid:
                        results[index] = {
                            "index": index, "record_key": key, "status": "processed",
                            "message": "Processed",
                        }
            except Exception:
                # Rare concurrent UID collisions or DB-level failures are isolated
                # per Event so one bad item does not reject the rest of the batch.
                created_records = Event.browse()
                for uid, (index, key, values) in pending_by_uid.items():
                    try:
                        existing = Event.search([("event_uid", "=", uid)], limit=1)
                        if existing:
                            if not self._measurement_event_matches(existing, values):
                                raise ValueError("event_uid_conflict")
                            results[index] = {
                                "index": index, "record_key": key, "status": "duplicate",
                                "message": "Already processed",
                            }
                            continue
                        with self.env.cr.savepoint():
                            event = Event.create(values)
                        created_records |= event
                        results[index] = {
                            "index": index, "record_key": key, "status": "processed",
                            "message": "Processed",
                        }
                    except Exception as exc:
                        results[index] = {
                            "index": index, "record_key": key, "status": "rejected",
                            "error_code": str(exc).split(":", 1)[0], "message": str(exc),
                        }

        for uid, duplicate_rows in duplicate_indices.items():
            primary = pending_by_uid.get(uid)
            primary_result = results[primary[0]] if primary else None
            for index, key in duplicate_rows:
                if primary_result and primary_result.get("status") in ("processed", "duplicate"):
                    results[index] = {
                        "index": index, "record_key": key, "status": "duplicate",
                        "message": "Already processed",
                    }
                else:
                    message = (primary_result or {}).get("message") or "event_uid_conflict"
                    results[index] = {
                        "index": index, "record_key": key, "status": "rejected",
                        "error_code": (primary_result or {}).get("error_code", "event_uid_conflict"),
                        "message": message,
                    }

        final_results = [row for row in results if row is not None]
        failed = sum(1 for row in final_results if row["status"] == "rejected")
        processed = len(final_results) - failed
        return {
            "received": len(items),
            "processed": processed,
            "failed": failed,
            "results": final_results,
        }, created_records

    @api.model
    def _forward_measurement_events_now(self, events):
        if not events or "nsp.sync.job" not in self.env.registry.models:
            return False
        try:
            return self.env["nsp.sync.job"].sudo().push_measurement_events_now(events)
        except Exception:
            _logger.exception("Immediate Measurement Event forwarding failed; fallback retry will handle it.")
            return False

    @api.model
    def _forward_measurement_status_now(self, session):
        if not session or "nsp.sync.job" not in self.env.registry.models:
            return False
        try:
            return self.env["nsp.sync.job"].sudo().push_measurement_status_now(session)
        except Exception:
            _logger.exception("Immediate Measurement status forwarding failed; fallback retry will handle it.")
            return False

    @endpoint("NSP Controller Measurement Events", route_path="controller/measurement/events", methods="POST", code="nsp_controller_measurement_events")
    def api_controller_measurement_events(self):
        data = self._payload()
        controller, error = self._auth_controller(data)
        if error:
            return error
        try:
            self._measurement_reject_unknown_fields(data, {"controller_code", "measurement_code", "events"})
            self._measurement_require_fields(data, ["measurement_code", "events"])
            session = self._measurement_session(data.get("measurement_code"))
            if session.controller_id != controller:
                raise ValueError("controller_not_in_scope")
            items = data.get("events")
            if not isinstance(items, list) or not items or len(items) > 100:
                raise ValueError("invalid_payload")
            result, records = self._measurement_process_event_batch(session, items)
            if result["processed"] and session.status == "ready":
                self._measurement_set_status(session, "running", fields.Datetime.now())
                self._forward_measurement_status_now(session)
            self._forward_measurement_events_now(records)
            return self._ok(result, message="Measurement Events processed.")
        except Exception as exc:
            return self._measurement_error_response(exc)

    @endpoint("NSP Controller Measurement Status", route_path="controller/measurement/status", methods="POST", code="nsp_controller_measurement_status")
    def api_controller_measurement_status(self):
        data = self._payload()
        controller, error = self._auth_controller(data)
        if error:
            return error
        try:
            self._measurement_reject_unknown_fields(data, {"controller_code", "measurement_code", "status", "occurred_at", "message"})
            self._measurement_require_fields(data, ["measurement_code", "status", "occurred_at"])
            session = self._measurement_session(data.get("measurement_code"))
            if session.controller_id != controller:
                raise ValueError("controller_not_in_scope")
            occurred_at = self._measurement_datetime(data.get("occurred_at"), required=True)
            self._measurement_set_status(session, data.get("status"), occurred_at, data.get("message"))
            self._forward_measurement_status_now(session)
            return self._ok({"data": self._measurement_session_payload(session)}, message="Measurement status recorded.")
        except Exception as exc:
            return self._measurement_error_response(exc)

    @endpoint("NSP Measurement Events Sync", route_path="measurement-events/sync", methods="POST", code="nsp_measurement_events_sync")
    def api_measurement_events_sync(self):
        data = self._payload()
        _application, _actor, edge_server, error = self._auth_edge_server_sync(data)
        if error:
            return error
        try:
            self._measurement_reject_unknown_fields(data, {"edge_server_code", "measurement_code", "events"})
            self._measurement_require_fields(data, ["measurement_code", "events"])
            session = self._measurement_session(data.get("measurement_code"))
            if not self._measurement_session_in_local_scope(session, edge_server):
                raise ValueError("edge_server_not_in_scope")
            items = data.get("events")
            if not isinstance(items, list) or not items or len(items) > 100:
                raise ValueError("invalid_payload")
            result, _records = self._measurement_process_event_batch(
                session, items, allow_final=True
            )
            return self._ok(result, message="Measurement Events synchronized.")
        except Exception as exc:
            return self._measurement_error_response(exc)

    @endpoint("NSP Measurement Status Sync", route_path="measurement-status/sync", methods="POST", code="nsp_measurement_status_sync")
    def api_measurement_status_sync(self):
        data = self._payload()
        _application, _actor, edge_server, error = self._auth_edge_server_sync(data)
        if error:
            return error
        try:
            self._measurement_reject_unknown_fields(data, {"edge_server_code", "measurement_code", "status", "occurred_at", "message"})
            self._measurement_require_fields(data, ["measurement_code", "status", "occurred_at"])
            session = self._measurement_session(data.get("measurement_code"))
            if not self._measurement_session_in_local_scope(session, edge_server):
                raise ValueError("edge_server_not_in_scope")
            occurred_at = self._measurement_datetime(data.get("occurred_at"), required=True)
            self._measurement_set_status(
                session, data.get("status"), occurred_at, data.get("message")
            )
            return self._ok({"data": self._measurement_session_payload(session)}, message="Measurement status synchronized.")
        except Exception as exc:
            return self._measurement_error_response(exc)

    @api.model
    def _prepare_parking_transaction_sync_cache(self, edge_server, items):
        """Preload topology, RFID assignments and existing UIDs for one sync batch."""
        rows = [item for item in items if isinstance(item, dict)]
        controller_codes = {str(item.get("controller_code") or "").strip() for item in rows}
        area_codes = {str(item.get("parking_area_code") or "").strip().upper() for item in rows}
        lane_codes = {str(item.get("lane_code") or "").strip().upper() for item in rows}
        serials = {str(item.get("serial_number") or "").strip().upper() for item in rows}
        vehicle_tids = {str(item.get("vehicle_tid") or "").strip() for item in rows}
        user_tids = {str(item.get("user_tid") or "").strip() for item in rows}
        uids = {str(item.get("transaction_uid") or "").strip() for item in rows}
        antenna_nos = set()
        for item in rows:
            try:
                value = int(item.get("antenna_no") or 0)
            except Exception:
                continue
            if value > 0:
                antenna_nos.add(value)
        for values in (controller_codes, area_codes, lane_codes, serials, vehicle_tids, user_tids, uids):
            values.discard("")

        Controller = self.env["nsp.controller"].sudo()
        controllers = Controller.search([
            ("edge_server_id", "=", edge_server.id),
            ("controller_id", "in", list(controller_codes)),
        ]) if controller_codes else Controller.browse()
        controller_by_code = {record.controller_id: record for record in controllers}

        Area = self.env["nsp.parking.area"].sudo()
        areas = Area.search([("code", "in", list(area_codes))]) if area_codes else Area.browse()
        area_by_code = {record.code: record for record in areas}

        Lane = self.env["nsp.parking.lane"].sudo()
        lanes = Lane.search([
            ("controller_id", "in", controllers.ids),
            ("parking_area_id", "in", areas.ids),
            ("code", "in", list(lane_codes)),
            ("active", "=", True),
        ]) if controllers and areas and lane_codes else Lane.browse()
        lane_by_key = {
            (record.controller_id.id, record.parking_area_id.code, record.code): record
            for record in lanes
        }

        Device = self.env["nsp.device"].sudo()
        devices = Device.search([
            ("controller_id", "in", controllers.ids),
            ("serial_number", "in", list(serials)),
        ]) if controllers and serials else Device.browse()
        device_by_key = {(record.controller_id.id, record.serial_number): record for record in devices}

        Antenna = self.env["nsp.device.antenna"].sudo()
        antennas = Antenna.search([
            ("device_id", "in", devices.ids),
            ("antenna_no", "in", list(antenna_nos)),
        ]) if devices and antenna_nos else Antenna.browse()
        antenna_by_key = {(record.device_id.id, int(record.antenna_no or 0)): record for record in antennas}

        Mapping = self.env["nsp.parking.lane.antenna.mapping"].sudo()
        mappings = Mapping.search([
            ("lane_id", "in", lanes.ids),
            ("antenna_ref_id", "in", antennas.ids),
        ]) if lanes and antennas else Mapping.browse()
        mapping_keys = {(record.lane_id.id, record.antenna_ref_id.id) for record in mappings}

        VehicleCard = self.env["nsp.vehicle.card"].sudo()
        vehicle_lines = VehicleCard.search([
            ("card_id.tid", "in", list(vehicle_tids)),
            ("state", "=", "active"),
            ("vehicle_id.active", "=", True),
        ]) if vehicle_tids else VehicleCard.browse()
        vehicle_by_tid = {}
        for line in vehicle_lines:
            vehicle_by_tid.setdefault(line.card_id.tid, line.vehicle_id)

        UserCard = self.env["nsp.user.card"].sudo()
        user_lines = UserCard.search([
            ("card_id.tid", "in", list(user_tids)),
            ("card_id.card_type", "=", "user_card"),
            ("state", "=", "active"),
            ("user_id.active", "=", True),
        ]) if user_tids else UserCard.browse()
        user_by_tid = {}
        for line in user_lines:
            user_by_tid.setdefault(line.card_id.tid, line.user_id)

        Transaction = self.env["nsp.parking.transaction"].sudo()
        existing = Transaction.search([("transaction_uid", "in", list(uids))]) if uids else Transaction.browse()

        return {
            "controller_by_code": controller_by_code,
            "area_by_code": area_by_code,
            "lane_by_key": lane_by_key,
            "device_by_key": device_by_key,
            "antenna_by_key": antenna_by_key,
            "mapping_keys": mapping_keys,
            "vehicle_by_tid": vehicle_by_tid,
            "user_by_tid": user_by_tid,
            "transaction_by_uid": {record.transaction_uid: record for record in existing},
        }

    @api.model
    def _upsert_parking_transaction_sync(self, edge_server, item, cache=None):
        if not isinstance(item, dict):
            raise ValueError("invalid_payload")
        allowed_fields = {
            "transaction_uid", "controller_code", "parking_area_code", "lane_code",
            "serial_number", "antenna_no", "event_type", "event_time",
            "vehicle_tid", "user_tid", "decision", "decision_reason_code",
            "decision_message",
        }
        unsupported = sorted(set(item) - allowed_fields)
        if unsupported:
            raise ValueError(
                "invalid_payload: unsupported field(s): %s" % ", ".join(unsupported)
            )

        uid = str(item.get("transaction_uid") or "").strip()
        controller_code = str(item.get("controller_code") or "").strip()
        parking_area_code = str(item.get("parking_area_code") or "").strip().upper()
        lane_code = str(item.get("lane_code") or "").strip().upper()
        serial_number = str(item.get("serial_number") or "").strip().upper()
        if not uid:
            raise ValueError("missing_transaction_uid")
        if not controller_code:
            raise ValueError("missing_controller_code")
        if not parking_area_code:
            raise ValueError("missing_parking_area_code")
        if not lane_code:
            raise ValueError("missing_lane_code")
        if not serial_number:
            raise ValueError("missing_serial_number")
        try:
            antenna_no = int(item.get("antenna_no") or 0)
        except Exception as exc:
            raise ValueError("invalid_antenna_no") from exc
        if antenna_no <= 0:
            raise ValueError("invalid_antenna_no")

        use_cache = cache is not None
        cache = cache or {}
        if use_cache:
            controller = cache["controller_by_code"].get(controller_code)
        else:
            controller = self.env["nsp.controller"].sudo().search([
                ("controller_id", "=", controller_code),
                ("edge_server_id", "=", edge_server.id),
            ], limit=1)
        if not controller:
            raise ValueError("route_not_allowed")

        if use_cache:
            parking_area = cache["area_by_code"].get(parking_area_code)
        else:
            parking_area = self.env["nsp.parking.area"].sudo().search([
                ("code", "=", parking_area_code),
            ], limit=1)
        if not parking_area:
            raise ValueError("parking_area_not_found")

        if use_cache:
            lane = cache["lane_by_key"].get((controller.id, parking_area_code, lane_code))
        else:
            lane = self.env["nsp.parking.lane"].sudo().search([
                ("parking_area_id", "=", parking_area.id),
                ("controller_id", "=", controller.id),
                ("code", "=", lane_code),
                ("active", "=", True),
            ], limit=1)
        if not lane:
            raise ValueError("lane_not_found")

        if use_cache:
            device = cache["device_by_key"].get((controller.id, serial_number))
        else:
            device = self.env["nsp.device"].sudo().search([
                ("controller_id", "=", controller.id),
                ("serial_number", "=", serial_number),
            ], limit=1)
        if not device:
            raise ValueError("device_not_found")

        if use_cache:
            antenna = cache["antenna_by_key"].get((device.id, antenna_no))
        else:
            antenna = self.env["nsp.device.antenna"].sudo().search([
                ("device_id", "=", device.id),
                ("antenna_no", "=", antenna_no),
            ], limit=1)
        if not antenna:
            raise ValueError("antenna_not_found")

        event_time = self._safe_datetime_value(item.get("event_time"), default_now=False)
        if not event_time:
            raise ValueError("event_time is required")
        event_type = str(item.get("event_type") or "").strip().lower()
        if event_type not in ("check_in", "check_out"):
            raise ValueError("invalid_event_type")
        direction = {"check_in": "entry", "check_out": "exit"}[event_type]
        if lane.direction != "both" and direction != lane.direction:
            raise ValueError("invalid_event_type")
        if use_cache:
            if (lane.id, antenna.id) not in cache["mapping_keys"]:
                raise ValueError("no_antenna_rule")
        elif not self.env["nsp.parking.lane.antenna.mapping"].sudo().search([
            ("lane_id", "=", lane.id),
            ("antenna_ref_id", "=", antenna.id),
        ], limit=1):
            raise ValueError("no_antenna_rule")
        decision = str(item.get("decision") or "").strip().lower()
        if decision not in ("allowed", "denied"):
            raise ValueError("invalid_decision")
        Transaction = self.env["nsp.parking.transaction"].sudo()
        vehicle_tid = str(item.get("vehicle_tid") or "").strip()
        if not vehicle_tid:
            raise ValueError("missing_vehicle_tid")
        user_tid = str(item.get("user_tid") or "").strip()
        if use_cache:
            vehicle = cache["vehicle_by_tid"].get(vehicle_tid) or self.env["nsp.vehicle"].browse()
            user = cache["user_by_tid"].get(user_tid) or self.env["nsp.user"].browse()
        else:
            vehicle = Transaction._resolve_vehicle_by_tid(vehicle_tid)
            user = Transaction._resolve_user_by_tid(user_tid)
        reason_code = Transaction._normalize_error_code(
            item.get("decision_reason_code"), item.get("decision_message")
        )
        if decision == "denied" and not reason_code:
            reason_code = "unknown"
        if decision == "allowed" and (item.get("decision_reason_code") or item.get("decision_message")):
            raise ValueError("allowed_event_cannot_have_decision_reason")

        vals = {
            "transaction_uid": uid,
            "event_time": event_time,
            "controller_id": controller.id,
            "lane_id": lane.id,
            "antenna_id": antenna.id,
            "event_type": event_type,
            "status": decision,
            "error_code": reason_code or False,
            "error_message": str(item.get("decision_message") or "").strip() or False,
            "vehicle_id": vehicle.id if vehicle else False,
            "vehicle_tid": vehicle_tid or False,
            "user_id": user.id if user else False,
            "user_tid": user_tid or False,
        }
        return Transaction.create_idempotent(
            vals, existing_by_uid=cache.get("transaction_by_uid")
        )

    @endpoint("NSP Gatekeeper Parking Transactions Sync", route_path="parking-transactions/sync", methods="POST", code="nsp_gatekeeper_parking_transactions_sync")
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
        cache = self._prepare_parking_transaction_sync_cache(edge_server, incoming)
        results = []
        processed = failed = 0
        for idx, item in enumerate(incoming):
            key = str(item.get("transaction_uid") or "").strip() if isinstance(item, dict) else ""
            try:
                with self.env.cr.savepoint():
                    rec, duplicate = self._upsert_parking_transaction_sync(edge_server, item, cache=cache)
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

    @endpoint("NSP Controller Parking Detection Push", route_path="parking/detections/push", methods="POST", code="nsp_controller_parking_detection_push")
    def api_parking_detection_push(self):
        """Accept a batch of raw TID detections from one authenticated Controller.

        The Controller only reports physical detections. One batch may contain
        detections from multiple Readers and antennas owned by that Controller.
        Edge validates, suppresses repeated reads, groups detections, and creates
        Parking Transactions internally. The Controller receives only one minimal
        acknowledgement for an accepted batch.
        """
        data = self._payload()
        controller, error = self._auth_controller(data)
        if error:
            return error

        allowed_fields = {"controller_code", "detections"}
        unsupported = sorted(set(data) - allowed_fields)
        if unsupported:
            return self._error(
                "invalid_payload: unsupported field(s): %s" % ", ".join(unsupported),
                400,
                error_code="parking_detection_rejected",
                details={"unsupported_fields": unsupported},
            )

        incoming = data.get("detections")
        if not isinstance(incoming, list) or not incoming:
            return self._error(
                "detections must be a non-empty array",
                400,
                error_code="parking_detection_rejected",
                details={"field": "detections"},
            )
        if len(incoming) > 1000:
            return self._error(
                "detections exceeds the maximum batch size of 1000",
                400,
                error_code="parking_detection_rejected",
                details={"field": "detections", "max_items": 1000},
            )

        item_fields = {"event_uid", "serial_number", "antenna_no", "detected_at", "tid"}
        normalized = []
        tids = set()
        Card = self.env["nsp.rfid.card"].sudo()

        # Validate the whole transport contract before writing any detection.
        for index, item in enumerate(incoming):
            if not isinstance(item, dict):
                return self._error(
                    "Each detection must be an object",
                    400,
                    error_code="parking_detection_rejected",
                    details={"index": index},
                )
            unsupported_item = sorted(set(item) - item_fields)
            if unsupported_item:
                return self._error(
                    "invalid_payload: unsupported detection field(s): %s" % ", ".join(unsupported_item),
                    400,
                    error_code="parking_detection_rejected",
                    details={"index": index, "unsupported_fields": unsupported_item},
                )

            event_uid = str(item.get("event_uid") or "").strip()
            serial_number = str(item.get("serial_number") or "").strip().upper()
            tid = Card._normalize_tid(item.get("tid"))
            detected_at = self._safe_datetime_value(item.get("detected_at"), default_now=False)
            try:
                antenna_no = int(item.get("antenna_no") or 0)
            except (TypeError, ValueError):
                antenna_no = 0

            missing = []
            if not event_uid:
                missing.append("event_uid")
            if not serial_number:
                missing.append("serial_number")
            if antenna_no <= 0:
                missing.append("antenna_no")
            if not detected_at:
                missing.append("detected_at")
            if not tid:
                missing.append("tid")
            if missing:
                return self._error(
                    "Invalid or missing detection field(s): %s" % ", ".join(missing),
                    400,
                    error_code="parking_detection_rejected",
                    details={"index": index, "fields": missing, "record_key": event_uid},
                )

            payload = {
                "event_uid": event_uid,
                "serial_number": serial_number,
                "antenna_no": antenna_no,
                "detected_at": detected_at,
                "tid": tid,
            }
            normalized.append(payload)
            tids.add(tid)

        # One database lookup for all cards in the batch. Unknown TIDs are
        # terminally ignored at the API boundary and are never persisted.
        cards_by_tid = {
            card.tid: card
            for card in Card.search([("tid", "in", list(tids))])
        }
        accepted = [
            (payload, cards_by_tid[payload["tid"]])
            for payload in normalized
            if payload["tid"] in cards_by_tid
        ]

        if accepted:
            try:
                self.env["nsp.parking.detection.event"].sudo().ingest_controller_detections(
                    controller, accepted
                )
            except Exception as exc:
                _logger.exception(
                    "Parking detection batch failed: controller=%s count=%s",
                    controller.controller_id, len(accepted),
                )
                return self._error(
                    str(exc), 500, error_code="parking_detection_failed"
                )

        return {"status_code": 200, "status": "success", "message": "OK", "data": {}}
