# -*- coding: utf-8 -*-
import base64
import json
import logging
from datetime import datetime, timezone

from odoo import api, fields, models
from odoo.addons.t4_coreapi.utils import endpoint, get_params, get_body

_logger = logging.getLogger(__name__)

class NspMasterGatekeeperSyncApiService(models.AbstractModel):
    _name = 'nsp.master.gatekeeper.sync.api.service'
    _description = 'NSP Master Gatekeeper Sync API Service'

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
    def _application_from_context(self):
        app_id = self.env.context.get("core_api_application_id")
        if not app_id:
            return self.env["core.api.application"].sudo().browse()
        return self.env["core.api.application"].sudo().browse(app_id).exists()

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
        """Authenticate one Edge request against the Cloud master service.

        The deployment boundary is the installed module itself:
        ``nsp_master_gatekeeper`` owns Cloud source endpoints, while
        ``nsp_sync`` is installed only on Edge as the outbound transport.
        Requiring an additional ``nsp.deployment_role`` parameter made every
        fresh Cloud installation default to ``edge_server`` and incorrectly
        reject valid requests with ``route_not_allowed``.
        """
        data = data or self._payload()
        application, actor_kind, error = self._auth_sync_application(data)
        if error:
            return application, actor_kind, False, error
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
            ("active", "=", True),
            ("device_type_code", "=", "RFID_READER"),
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
            "last_seen_at", "firmware_version", "power_dbm", "read_interval_ms",
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
        if item.get("power_dbm") not in (None, ""):
            power = int(item.get("power_dbm"))
            if power < 0 or power > 40:
                raise ValueError("invalid_power_dbm")
            vals["runtime_power_dbm"] = power
        if item.get("read_interval_ms") not in (None, ""):
            read_interval = int(item.get("read_interval_ms"))
            if read_interval <= 0 or read_interval > 60000:
                raise ValueError("invalid_read_interval_ms")
            vals["runtime_read_interval_ms"] = read_interval
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
            ("active", "=", True),
            ("whitelist_id.active", "=", True),
            ("whitelist_id.device_type_code", "=", "CONTROLLER"),
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

    def _rfid_tag_sync_payload(self, tag, assignment=False):
        payload = {"tid": tag.tid}
        if assignment:
            if assignment.user_id:
                payload["assignment"] = {
                    "target": "user",
                    "code": self._user_code(assignment.user_id),
                    "assigned_at": self._iso_datetime(assignment.assigned_at),
                }
            elif assignment.vehicle_id:
                payload["assignment"] = {
                    "target": "vehicle",
                    "code": assignment.vehicle_id.vehicle_code or "",
                    "assigned_at": self._iso_datetime(assignment.assigned_at),
                }
        return payload

    @api.model
    def _published_parking_payload_for_edge(self, area, edge_code):
        """Return the immutable published Parking Layout slice for one Edge."""
        payload = area.prepare_sync_payload()
        if not payload:
            return False
        normalized_edge = str(edge_code or "").strip().upper()
        lanes = []
        for lane in payload.get("lanes") or []:
            if not isinstance(lane, dict):
                continue
            if str(lane.get("server_code") or "").strip().upper() != normalized_edge:
                continue
            lanes.append(dict(lane))
        if not lanes:
            return False
        result = dict(payload)
        result["lanes"] = lanes
        return result

    @api.model
    def _published_gatekeeper_projection(self, edge):
        """Project only devices referenced by published Parking assemblies.

        Device Whitelist remains the Cloud inventory of independent identities.
        This projection is assembled from immutable published Parking Layout
        snapshots and therefore never exposes an unrelated fixed device tree.
        """
        edge_code = str(edge.edge_server_code or "").strip().upper()
        Area = self.env["nsp.parking.area"].sudo()
        areas = Area.search([
            ("published_payload_json", "!=", False),
        ], order="code,id").filtered(lambda area: area.is_published_for_edge(edge_code))

        area_payloads = []
        branch_ids = set()
        referenced_codes = set()
        controller_map = {}
        for area in areas:
            payload = self._published_parking_payload_for_edge(area, edge_code)
            if not payload:
                continue
            area_payloads.append(payload)
            branch_ids.add(area.branch_id.id)
            for lane in payload.get("lanes") or []:
                server_code = str(lane.get("server_code") or "").strip().upper()
                controller_code = str(lane.get("controller_code") or "").strip().upper()
                if not server_code or not controller_code:
                    continue
                referenced_codes.update({server_code, controller_code})
                controller = controller_map.setdefault(controller_code, {
                    "controller_code": controller_code,
                    "controller_name": controller_code,
                    "server_code": server_code,
                    "active": True,
                    "devices": {},
                })
                if controller["server_code"] != server_code:
                    raise ValueError("controller_published_on_multiple_servers")
                for reader in lane.get("readers") or []:
                    if not isinstance(reader, dict):
                        continue
                    reader_code = str(reader.get("technical_code") or "").strip().upper()
                    serial = str(reader.get("serial_number") or "").strip().upper()
                    if not reader_code or not serial:
                        raise ValueError("published_reader_identity_missing")
                    referenced_codes.add(reader_code)
                    reader_payload = dict(reader)
                    reader_payload["technical_code"] = reader_code
                    reader_payload["serial_number"] = serial
                    antenna_rows = []
                    for antenna in reader.get("antennas") or []:
                        if not isinstance(antenna, dict):
                            continue
                        antenna_code = str(antenna.get("technical_code") or "").strip().upper()
                        if not antenna_code:
                            raise ValueError("published_antenna_identity_missing")
                        referenced_codes.add(antenna_code)
                        antenna_row = dict(antenna)
                        antenna_row["technical_code"] = antenna_code
                        antenna_rows.append(antenna_row)
                    reader_payload["antennas"] = antenna_rows
                    previous = controller["devices"].get(reader_code)
                    if previous and previous != reader_payload:
                        raise ValueError("reader_published_with_conflicting_configuration")
                    controller["devices"][reader_code] = reader_payload

        Whitelist = self.env["nsp.device.whitelist"].sudo().with_context(active_test=False)
        identities = Whitelist.search([
            ("technical_code", "in", sorted(referenced_codes)),
        ], order="technical_code,id") if referenced_codes else Whitelist.browse()
        identity_by_code = {record.technical_code: record for record in identities}
        missing = sorted(referenced_codes - set(identity_by_code))
        if missing:
            raise ValueError("published_device_identity_missing:%s" % ",".join(missing))
        inactive = sorted(
            code for code, record in identity_by_code.items() if not record.active
        )
        if inactive:
            raise ValueError("published_device_identity_inactive:%s" % ",".join(inactive))

        for controller_code, controller in controller_map.items():
            identity = identity_by_code.get(controller_code)
            if identity:
                controller["controller_name"] = identity.name or identity.technical_code
            controller["devices"] = sorted(
                controller["devices"].values(),
                key=lambda row: (row.get("serial_number") or "", row.get("technical_code") or ""),
            )

        Branch = self.env["nsp.branch"].sudo()
        branches = Branch.browse(sorted(branch_ids)) if branch_ids else Branch.browse()
        type_order = {"SERVER": 1, "CONTROLLER": 2, "RFID_READER": 3, "ANTENNA": 4}
        whitelist_payload = [
            record._prepare_sync_payload()
            for record in identities.sorted(
                key=lambda row: (
                    type_order.get(row.device_type_code, 9),
                    row.technical_code or "",
                    row.id,
                )
            )
        ]
        return {
            "branches": [{
                "branch_code": branch.code,
                "branch_name": branch.name,
                "timezone": branch.timezone or "Asia/Ho_Chi_Minh",
                "active": branch.status == "active",
            } for branch in branches],
            "controllers": sorted(
                controller_map.values(), key=lambda row: row["controller_code"]
            ),
            "parking_areas": area_payloads,
            "device_whitelist": whitelist_payload,
        }

    @endpoint("NSP Gatekeeper Configuration Sync", route_path="gatekeeper-config/sync", methods="POST", code="nsp_gatekeeper_config_sync")
    def api_gatekeeper_config_sync(self):
        data = self._payload()
        _app, _actor, edge, error = self._auth_edge_server_sync(data)
        if error:
            return error
        unsupported = sorted(set(data) - {"edge_server_code"})
        if unsupported:
            return self._error(
                "Unsupported field(s): %s" % ", ".join(unsupported),
                400,
                error_code="invalid_payload",
            )
        try:
            projection = self._published_gatekeeper_projection(edge)
        except Exception as exc:
            return self._error(
                str(exc).replace("_", " "),
                409,
                error_code=str(exc).split(":", 1)[0] or "invalid_published_configuration",
            )
        return self._ok({
            "edge_server_code": edge.edge_server_code,
            "revision": int(edge.config_revision or 1),
            **projection,
            "server_time": self._iso_datetime(fields.Datetime.now()),
        }, message="Published Gatekeeper assembly snapshot loaded.")

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
        VehicleBrand = self.env["nsp.reference.brand"].sudo().with_context(active_test=False)
        VehicleModel = self.env["nsp.reference.model"].sudo().with_context(active_test=False)
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

    @endpoint("NSP RFID Tag Whitelist Sync", route_path="rfid-tags/sync", methods="POST", code="nsp_rfid_tags_sync")
    def api_rfid_tags_sync(self):
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
        tags = self.env["nsp.rfid.tag"].sudo().search([], order="tid asc, id asc")
        assignments = self.env["nsp.rfid.tag.assignment"].sudo().search([
            ("tag_id", "in", tags.ids), ("state", "=", "active"),
        ], order="assigned_at desc, id desc") if tags else self.env["nsp.rfid.tag.assignment"].browse()
        assignment_by_tag = {}
        for assignment in assignments:
            assignment_by_tag.setdefault(assignment.tag_id.id, assignment)
        items = [
            self._rfid_tag_sync_payload(tag, assignment_by_tag.get(tag.id))
            for tag in tags
        ]
        employee_count = sum(
            1 for item in items if (item.get("assignment") or {}).get("target") == "user"
        )
        vehicle_count = sum(
            1 for item in items if (item.get("assignment") or {}).get("target") == "vehicle"
        )
        return self._ok({
            "items": items,
            "summary": {
                "whitelisted_tags": len(items),
                "employee_assignments": employee_count,
                "vehicle_assignments": vehicle_count,
                "unassigned_tags": len(items) - employee_count - vehicle_count,
            },
            "next_sync_cursor": False,
            "has_more": False,
            "server_time": self._iso_datetime(fields.Datetime.now()),
        }, message="RFID Tag Whitelist snapshot loaded.")

    @endpoint("NSP Gatekeeper Users Sync", route_path="users/sync", methods="POST", code="nsp_users_sync")
    def api_users_sync(self):
        data = self._payload()
        application, actor_kind, edge_server, error = self._auth_edge_server_sync(data)
        if error:
            return error
        User = self.env["nsp.user"].sudo()
        users = User.search([("user_code","!=",False),("user_code","!=","")], order="user_code,id")
        next_cursor, has_more, server_time = False, False, fields.Datetime.now()
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

    @endpoint("NSP Gatekeeper Vehicles Sync", route_path="vehicles/sync", methods="POST", code="nsp_vehicles_sync")
    def api_vehicles_sync(self):
        data = self._payload()
        application, actor_kind, edge_server, error = self._auth_edge_server_sync(data)
        if error:
            return error
        Vehicle = self.env["nsp.vehicle"].sudo()
        vehicles = Vehicle.search([], order="vehicle_code,id")
        next_cursor, has_more, server_time = False, False, fields.Datetime.now()
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

    @endpoint("NSP Gatekeeper Vehicle Borrow Sync", route_path="vehicle-borrow/sync", methods="POST", code="nsp_vehicle_borrow_sync")
    def api_vehicle_borrow_sync(self):
        data = self._payload()
        application, actor_kind, edge_server, error = self._auth_edge_server_sync(data)
        if error:
            return error
        if "nsp.vehicle.borrow" not in self.env.registry.models:
            return self._ok({"items": [], "next_sync_cursor": data.get("sync_cursor") or False, "has_more": False, "server_time": self._iso_datetime(fields.Datetime.now())})
        Borrow = self.env["nsp.vehicle.borrow"].sudo()
        records = Borrow.search([], order="borrow_code,id")
        next_cursor, has_more, server_time = False, False, fields.Datetime.now()
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
            try:
                parsed = fields.Datetime.to_datetime(text)
            except Exception as exc:
                raise ValueError("invalid_datetime") from exc
        if not parsed:
            raise ValueError("invalid_datetime")
        if parsed.tzinfo:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        else:
            parsed = parsed.replace(tzinfo=None)
        return fields.Datetime.to_string(parsed)

    @api.model
    def _measurement_millisecond(self, value):
        text = str(value or "").strip()
        if not text:
            return 0
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return int(parsed.microsecond / 1000)
        except Exception:
            return 0

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
        return bool(session.reader_line_ids.filtered(
            lambda line: line.edge_server_id == edge_server
        ))

    @api.model
    def _measurement_config_payload(self, session, edge_server=False):
        """Return one released calibration assembly for an Edge Server."""
        lines = session.reader_line_ids
        if edge_server:
            lines = lines.filtered(lambda line: line.edge_server_id == edge_server)
        readers = []
        for line in lines.sorted(
            key=lambda item: (
                item.edge_server_id.edge_server_code or "",
                item.controller_id.controller_id or "",
                item.reader_id.serial_number or "",
                item.id,
            )
        ):
            antenna_rows = []
            for mapping in line.antenna_port_ids.filtered("antenna_id").sorted(
                key=lambda item: (item.port_no, item.id)
            ):
                antenna = mapping.antenna_id
                antenna_rows.append({
                    "antenna_no": int(mapping.port_no or 0),
                    "technical_code": antenna.technical_code or antenna.whitelist_id.technical_code or "",
                    "serial_number": antenna.serial_number or antenna.whitelist_id.serial_number or False,
                    "name": antenna.whitelist_id.name or antenna.display_name or "",
                })
            readers.append({
                "server_code": line.edge_server_id.edge_server_code or "",
                "controller_code": line.controller_id.controller_id or "",
                "controller_name": line.controller_id.controller_name or "",
                "technical_code": line.reader_id.device_code or "",
                "serial_number": line.reader_id.serial_number or "",
                "reader_name": line.reader_id.name or line.reader_id.serial_number or "",
                "physical_connection": line.reader_id.connection_type or False,
                "reader_parameters": {
                    "power_dbm": int(line.reader_power_dbm or 0),
                    "read_interval_ms": int(line.read_interval_ms or 200),
                    "tid_start_address": int(line.reader_tid_addr or 0),
                    "tid_length": int(line.reader_tid_len or 0),
                },
                "antennas": antenna_rows,
            })
        vehicles = []
        for line in session.target_line_ids.sorted(
            key=lambda item: ((item.license_plate or ""), item.id)
        ):
            vehicle = line.vehicle_id
            item = {
                "vehicle_tid": line.vehicle_tid or "",
                "vehicle_code": vehicle.vehicle_code or "",
                "license_plate": line.license_plate or "",
            }
            owner_code = self._user_code(vehicle.owner_id) if vehicle.owner_id else ""
            if owner_code:
                item["owner_user_code"] = owner_code
            vehicles.append(item)
        payload = {
            "measurement_code": session.measurement_code,
            "status": session.status,
            "desired_state": "running" if session.status in ("ready", "running") else "stopped",
            "revision": int(session.revision or 1),
            "vehicles": vehicles,
            "readers": readers,
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
        return payload

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

    @endpoint("NSP Measurement Configuration Sync", route_path="measurement-config/sync", methods="POST", code="nsp_measurement_config_sync")
    def api_measurement_config_sync(self):
        data = self._payload()
        _application, _actor, edge_server, error = self._auth_edge_server_sync(data)
        if error:
            return error
        try:
            self._measurement_reject_unknown_fields(data, {"edge_server_code"})
            Session = self.env["nsp.measurement.session"].sudo()
            records = Session.search([
                ("status", "in", ("ready", "running")),
                ("reader_line_ids.edge_server_id", "=", edge_server.id),
            ], order="measurement_code,id")
            return self._ok({
                "items": [self._measurement_config_payload(session, edge_server=edge_server) for session in records],
                "next_sync_cursor": False,
                "has_more": False,
                "server_time": self._iso_datetime(fields.Datetime.now()),
            }, message="Measurement configuration snapshot loaded.")
        except Exception as exc:
            return self._measurement_error_response(exc)

    @api.model
    def _measurement_event_values(
        self, session, item, allowed_antennas=None, accept_snapshot=False,
    ):
        allowed = {
            "event_uid", "serial_number", "antenna_no", "tid", "read_at", "rssi_dbm",
            "revision", "power_dbm", "read_interval_ms",
        }
        self._measurement_reject_unknown_fields(item, allowed)
        self._measurement_require_fields(item, ["event_uid", "serial_number", "antenna_no", "tid", "read_at"])
        event_uid = str(item.get("event_uid") or "").strip()
        serial_number = str(item.get("serial_number") or "").strip().upper()
        tid = self.env["nsp.rfid.tag"].sudo()._normalize_tid(item.get("tid"))
        try:
            antenna_no = int(item.get("antenna_no") or 0)
        except Exception:
            antenna_no = 0
        if antenna_no <= 0:
            raise ValueError("antenna_not_found")
        reader_line = session._measurement_line_for_serial(serial_number)
        # Device Whitelist identities have no permanent parent-child topology.
        # Observation scope is therefore validated only against the Reader
        # Calibration assembly released for this Session, never against a fixed
        # Controller -> Reader -> Antenna tree on the master records.
        if allowed_antennas is None:
            allowed_antennas = session._allowed_antenna_pairs()
        if (serial_number, antenna_no) not in allowed_antennas:
            raise ValueError("antenna_not_found")
        if not reader_line:
            raise ValueError("reader_not_in_scope")
        read_at = self._measurement_datetime(item.get("read_at"), required=True)
        read_at_ms = self._measurement_millisecond(item.get("read_at"))
        if item.get("rssi_dbm") in (None, ""):
            rssi = False
        else:
            try:
                rssi = float(item.get("rssi_dbm"))
            except Exception:
                raise ValueError("invalid_rssi")
        if accept_snapshot:
            try:
                revision = int(item.get("revision") or session.revision or 1)
                fallback_power = (
                    reader_line.reader_power_dbm
                    if reader_line
                    else session._reader_power_for_serial(serial_number)
                )
                power_dbm = int(
                    item.get("power_dbm")
                    if item.get("power_dbm") is not None
                    else fallback_power
                )
                fallback_interval = (
                    reader_line.read_interval_ms
                    if reader_line
                    else session._reader_interval_for_serial(serial_number)
                )
                read_interval_ms = int(
                    item.get("read_interval_ms")
                    if item.get("read_interval_ms") is not None
                    else fallback_interval
                )
            except Exception as exc:
                raise ValueError("invalid_measurement_snapshot") from exc
        else:
            revision = int(session.revision or 1)
            power_dbm = int(reader_line.reader_power_dbm or 0)
            read_interval_ms = int(reader_line.read_interval_ms or 0)
        if (
            revision <= 0
            or power_dbm < 0
            or power_dbm > 40
            or read_interval_ms <= 0
            or read_interval_ms > 60000
        ):
            raise ValueError("invalid_measurement_snapshot")
        return {
            "event_uid": event_uid,
            "session_id": session.id,
            "revision": revision,
            "serial_number": serial_number,
            "antenna_no": antenna_no,
            "tid": tid,
            "read_at": read_at,
            "read_at_ms": read_at_ms,
            "rssi_dbm": rssi,
            "power_dbm": power_dbm,
            "read_interval_ms": read_interval_ms,
        }

    @api.model
    def _measurement_event_matches(self, event, values):
        return (
            event.session_id.id == values["session_id"]
            and int(event.revision or 1) == int(values["revision"] or 1)
            and event.serial_number == values["serial_number"]
            and int(event.antenna_no or 0) == int(values["antenna_no"] or 0)
            and event.tid == values["tid"]
            and fields.Datetime.to_string(event.read_at) == fields.Datetime.to_string(values["read_at"])
            and int(event.read_at_ms or 0) == int(values["read_at_ms"] or 0)
            and (False if event.rssi_dbm in (False, None) else float(event.rssi_dbm))
            == (False if values["rssi_dbm"] in (False, None) else float(values["rssi_dbm"]))
            and int(event.power_dbm or 0) == int(values["power_dbm"] or 0)
            and int(event.read_interval_ms or 0) == int(values["read_interval_ms"] or 0)
        )

    @api.model
    def _measurement_process_event_batch(
        self, session, items, allow_final=False, accept_snapshot=False,
        enforce_current_snapshot=False,
    ):
        """Store only the selected Target Tag, idempotently, with bounded queries."""
        Event = self.env["nsp.measurement.event"].sudo()
        allowed_antennas = session._allowed_antenna_pairs()
        target_tids = session._allowed_target_tids()
        prepared = []
        results = [None] * len(items)

        for index, item in enumerate(items):
            key = str(item.get("event_uid") or "") if isinstance(item, dict) else ""
            try:
                if not isinstance(item, dict):
                    raise ValueError("invalid_payload")
                incoming_tid = self.env["nsp.rfid.tag"].sudo()._normalize_tid(item.get("tid"))
                if incoming_tid not in target_tids:
                    results[index] = {
                        "index": index,
                        "record_key": key,
                        "status": "ignored",
                        "message": "RFID Tag is not in the Measurement target list",
                    }
                    continue
                values = self._measurement_event_values(
                    session,
                    item,
                    allowed_antennas=allowed_antennas,
                    accept_snapshot=accept_snapshot,
                )
                if enforce_current_snapshot and (
                    int(values["revision"] or 1) != int(session.revision or 1)
                    or int(values["power_dbm"] or 0)
                    != int(session._reader_power_for_serial(values["serial_number"]) or 0)
                    or int(values["read_interval_ms"] or 0)
                    != int(session._reader_interval_for_serial(values["serial_number"]) or 0)
                ):
                    results[index] = {
                        "index": index,
                        "record_key": key,
                        "status": "ignored",
                        "message": "Stale Measurement revision/settings ignored",
                    }
                    continue
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
                    and first["revision"] == values["revision"]
                    and first["serial_number"] == values["serial_number"]
                    and int(first["antenna_no"]) == int(values["antenna_no"])
                    and first["tid"] == values["tid"]
                    and fields.Datetime.to_string(first["read_at"]) == fields.Datetime.to_string(values["read_at"])
                    and int(first["read_at_ms"] or 0) == int(values["read_at_ms"] or 0)
                    and (False if first["rssi_dbm"] in (False, None) else float(first["rssi_dbm"]))
                    == (False if values["rssi_dbm"] in (False, None) else float(values["rssi_dbm"]))
                    and int(first["power_dbm"] or 0) == int(values["power_dbm"] or 0)
                    and int(first["read_interval_ms"] or 0)
                    == int(values["read_interval_ms"] or 0)
                )
                if same:
                    duplicate_indices.setdefault(uid, []).append((index, key))
                else:
                    results[index] = {
                        "index": index, "record_key": key, "status": "rejected",
                        "error_code": "event_uid_conflict", "message": "event_uid_conflict",
                    }
                continue

            if not allow_final and session.status in ("completed", "applied", "failed", "cancelled"):
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
        ignored = sum(1 for row in final_results if row["status"] == "ignored")
        processed = len(final_results) - failed - ignored
        return {
            "received": len(items),
            "processed": processed,
            "ignored": ignored,
            "failed": failed,
            "results": final_results,
        }, created_records

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
                session, items, allow_final=True, accept_snapshot=True,
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
        """Preload optional Cloud links for immutable Edge business events.

        Cloud must not re-run parking topology validation for delayed transactions:
        the topology may have been changed or deleted after the event occurred.
        Current master records are used only to enrich navigation when available.
        """
        rows = [item for item in (items or []) if isinstance(item, dict)]
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
                number = int(item.get("antenna_no") or 0)
            except Exception:
                continue
            if number > 0:
                antenna_nos.add(number)
        for values in (controller_codes, area_codes, lane_codes, serials, vehicle_tids, user_tids, uids):
            values.discard("")

        Controller = self.env["nsp.controller"].sudo().with_context(active_test=False)
        controllers = Controller.search([
            ("controller_id", "in", list(controller_codes)),
        ]) if controller_codes else Controller.browse()
        controller_by_code = {record.controller_id: record for record in controllers}

        Area = self.env["nsp.parking.area"].sudo()
        areas = Area.search([("code", "in", list(area_codes))]) if area_codes else Area.browse()
        area_by_code = {record.code: record for record in areas}

        Lane = self.env["nsp.parking.lane"].sudo().with_context(active_test=False)
        lanes = Lane.search([("code", "in", list(lane_codes))]) if lane_codes else Lane.browse()
        lane_by_key = {
            (record.controller_id.id, record.parking_area_id.code, record.code): record
            for record in lanes
            if record.controller_id and record.parking_area_id
        }

        Device = self.env["nsp.device"].sudo()
        devices = Device.search([("serial_number", "in", list(serials))]) if serials else Device.browse()
        device_by_key = {(record.controller_id.id, record.serial_number): record for record in devices if record.controller_id}

        Antenna = self.env["nsp.device.antenna"].sudo()
        antennas = Antenna.search([
            ("device_id", "in", devices.ids),
            ("antenna_no", "in", list(antenna_nos)),
        ]) if devices and antenna_nos else Antenna.browse()
        antenna_by_key = {(record.device_id.id, int(record.antenna_no or 0)): record for record in antennas}

        Assignment = self.env["nsp.rfid.tag.assignment"].sudo()
        assignments = Assignment.search([
            ("tid", "in", list(vehicle_tids | user_tids)),
            ("state", "=", "active"),
        ]) if (vehicle_tids or user_tids) else Assignment.browse()
        vehicle_by_tid = {
            assignment.tid: assignment.vehicle_id
            for assignment in assignments if assignment.vehicle_id
        }
        user_by_tid = {
            assignment.tid: assignment.user_id
            for assignment in assignments if assignment.user_id
        }

        Transaction = self.env["nsp.parking.transaction"].sudo()
        existing = Transaction.search([("transaction_uid", "in", list(uids))]) if uids else Transaction.browse()
        return {
            "controller_by_code": controller_by_code,
            "area_by_code": area_by_code,
            "lane_by_key": lane_by_key,
            "device_by_key": device_by_key,
            "antenna_by_key": antenna_by_key,
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
            "vehicle_tid", "license_plate", "user_tid", "decision", "decision_reason_code",
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
            controller = self.env["nsp.controller"].sudo().with_context(active_test=False).search([
                ("controller_id", "=", controller_code),
            ], limit=1)
        # A current Controller belonging to another Edge is a real scope violation.
        # A missing Controller is allowed for a delayed historical transaction.
        if controller and controller.edge_server_id != edge_server:
            raise ValueError("route_not_allowed")

        if use_cache:
            parking_area = cache["area_by_code"].get(parking_area_code)
        else:
            parking_area = self.env["nsp.parking.area"].sudo().search([
                ("code", "=", parking_area_code),
            ], limit=1)

        lane = self.env["nsp.parking.lane"].browse()
        device = self.env["nsp.device"].browse()
        antenna = self.env["nsp.device.antenna"].browse()
        if controller and parking_area:
            if use_cache:
                lane = cache["lane_by_key"].get((controller.id, parking_area_code, lane_code)) or lane
            else:
                lane = self.env["nsp.parking.lane"].sudo().with_context(active_test=False).search([
                    ("parking_area_id", "=", parking_area.id),
                    ("controller_id", "=", controller.id),
                    ("code", "=", lane_code),
                ], limit=1)
        if controller:
            if use_cache:
                device = cache["device_by_key"].get((controller.id, serial_number)) or device
            else:
                device = self.env["nsp.device"].sudo().search([
                    ("controller_id", "=", controller.id),
                    ("serial_number", "=", serial_number),
                ], limit=1)
        if device:
            if use_cache:
                antenna = cache["antenna_by_key"].get((device.id, antenna_no)) or antenna
            else:
                antenna = self.env["nsp.device.antenna"].sudo().search([
                    ("device_id", "=", device.id),
                    ("antenna_no", "=", antenna_no),
                ], limit=1)

        event_time = self._safe_datetime_value(item.get("event_time"), default_now=False)
        if not event_time:
            raise ValueError("event_time is required")
        event_type = str(item.get("event_type") or "").strip().lower()
        if event_type not in ("check_in", "check_out"):
            raise ValueError("invalid_event_type")
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
            "controller_id": controller.id if controller else False,
            "controller_code": controller_code,
            "parking_area_id": parking_area.id if parking_area else False,
            "parking_area_code": parking_area_code,
            "lane_id": lane.id if lane else False,
            "lane_code": lane_code,
            "antenna_id": antenna.id if antenna else False,
            "serial_number": serial_number,
            "antenna_no": antenna_no,
            "event_type": event_type,
            "status": decision,
            "error_code": reason_code or False,
            "error_message": str(item.get("decision_message") or "").strip() or False,
            "vehicle_id": vehicle.id if vehicle else False,
            "license_plate": str(item.get("license_plate") or (vehicle.license_plate if vehicle else "")).strip() or False,
            "vehicle_tid": vehicle_tid or False,
            "user_id": user.id if user else False,
            "user_tid": user_tid or False,
        }
        return Transaction.create_idempotent(
            vals, existing_by_uid=cache.get("transaction_by_uid")
        )

    @endpoint("NSP Gatekeeper Parking Transactions Sync", route_path="parking-transactions/sync", methods="POST", code="nsp_parking_transactions_sync")
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

