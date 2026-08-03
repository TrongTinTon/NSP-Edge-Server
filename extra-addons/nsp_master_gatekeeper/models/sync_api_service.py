from datetime import datetime, timezone

from odoo import api, fields, models
from odoo.addons.t4_coreapi.utils import endpoint, get_body

class NspMasterGatekeeperSyncApiService(models.AbstractModel):
    _name = 'nsp.master.gatekeeper.sync.api.service'
    _description = 'NSP Master Gatekeeper Sync API Service'

    @api.model
    def _ok(self, payload=None, message="OK", status_code=200, **extra):
        data = {"success": True}
        if isinstance(payload, dict):
            data.update(payload)
        elif payload is not None:
            data["data"] = payload
        data.update(extra)
        return {"status_code": status_code, "message": message, "data": data}

    @api.model
    def _error(self, message, status_code=400, error_code="invalid_payload", details=None, **extra):
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
    def _auth_edge_server_sync(self, data=None):
        if not self._application_from_context():
            return self.env["nsp.edge.server"].browse(), self._error(
                "Core API Application authentication is required",
                401,
            )
        return self._edge_server_from_payload(data or self._payload())

    @api.model
    def _auth_edge_snapshot_request(self, data=None):
        data = data or self._payload()
        edge_server, error = self._auth_edge_server_sync(data)
        if error:
            return edge_server, error
        unsupported = sorted(set(data) - {"edge_server_code"})
        if unsupported:
            return edge_server, self._error(
                "Unsupported field(s): %s" % ", ".join(unsupported),
                400,
                error_code="invalid_payload",
                details={"unsupported_fields": unsupported},
            )
        return edge_server, None

    @api.model
    def _snapshot_meta(self, edge_server, scope):
        return {
            "edge_server_code": edge_server.edge_server_code,
            "snapshot_scope": str(scope or "").strip(),
            "snapshot_mode": "replace",
            "server_time": self._iso_datetime(fields.Datetime.now()),
        }

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
    def _user_code(self, user):
        return str(user.user_code or "").strip() if user else ""

    @api.model
    def _edge_server_code_from_payload(self, data=None):
        data = data or {}
        return str(data.get("edge_server_code") or "").strip()

    @api.model
    def _edge_server_from_payload(self, data=None):
        EdgeServer = self.env["nsp.edge.server"].sudo().with_context(active_test=False)
        edge_server_code = self._edge_server_code_from_payload(data)
        if not edge_server_code:
            return EdgeServer.browse(), self._error(
                "edge_server_code is required",
                400,
                error_code="missing_edge_server_code",
                details={"field": "edge_server_code"},
            )
        edge_server = EdgeServer.search([
            ("edge_server_code", "=", edge_server_code.upper()),
        ], limit=1)
        if not edge_server:
            return EdgeServer.browse(), self._error(
                "Edge Server was not found",
                404,
                error_code="record_not_found",
                details={"edge_server_code": edge_server_code},
            )
        if not edge_server.active or edge_server.status in ("block", "revoked"):
            return EdgeServer.browse(), self._error(
                "Edge Server is blocked or revoked",
                403,
                error_code="route_not_allowed",
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
        if not isinstance(item, dict):
            raise ValueError("invalid_payload")
        allowed_fields = {
            "serial_number", "ports", "device_status",
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

        reported_ports = item.get("ports")
        if reported_ports is not None:
            if not isinstance(reported_ports, list):
                raise ValueError("ports must be an array")
            port_numbers = []
            for value in reported_ports:
                if isinstance(value, bool):
                    raise ValueError("invalid_port_number")
                try:
                    port_no = int(value)
                except (TypeError, ValueError) as exc:
                    raise ValueError("invalid_port_number") from exc
                if port_no <= 0:
                    raise ValueError("invalid_port_number")
                port_numbers.append(port_no)
            if len(port_numbers) != len(set(port_numbers)):
                raise ValueError("duplicate_port_number")

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

    @endpoint("NSP Edge Status", route_path="edge/status", methods="POST", code="nsp_edge_status")
    def api_edge_status(self):
        data = self._payload()
        edge_server, error = self._auth_edge_server_sync(data)
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
        payload = area.prepare_sync_payload()
        if not payload:
            return False
        normalized_edge = str(edge_code or "").strip().upper()
        lanes = []
        for lane in payload.get("lanes") or []:
            if not isinstance(lane, dict):
                raise ValueError("published_lane_payload_invalid")
            server_code = str(lane.get("server_code") or "").strip().upper()
            if server_code == normalized_edge:
                lanes.append(lane)
        if not lanes:
            return False
        return {
            "parking_area_code": payload.get("parking_area_code") or "",
            "parking_area_name": payload.get("parking_area_name") or "",
            "branch_code": payload.get("branch_code") or "",
            "state": payload.get("state") or "",
            "published_revision": int(payload.get("published_revision") or 1),
            "lanes": lanes,
        }

    @api.model
    def _runtime_lane_projection(self, lane):
        lane_code = str(lane.get("lane_code") or "").strip().upper()
        server_code = str(lane.get("server_code") or "").strip().upper()
        controller_code = str(lane.get("controller_code") or "").strip().upper()
        if not lane_code:
            raise ValueError("published_lane_identity_missing")
        if not server_code:
            raise ValueError("published_lane_server_identity_missing:%s" % lane_code)
        if not controller_code:
            raise ValueError("published_lane_controller_identity_missing:%s" % lane_code)

        source_timeline = lane.get("reader_port_timeline") or []
        if not isinstance(source_timeline, list):
            raise ValueError("published_reader_port_timeline_invalid:%s" % lane_code)

        runtime_timeline = []
        ports_by_reader = {}
        serial_by_reader = {}
        timeline_refs = set()
        for point in source_timeline:
            if not isinstance(point, dict):
                raise ValueError("published_reader_port_timeline_point_invalid:%s" % lane_code)
            reader_code = str(point.get("reader_code") or "").strip().upper()
            reader_serial = str(point.get("reader_serial_number") or "").strip().upper()
            try:
                port_no = int(point.get("port_no") or 0)
            except (TypeError, ValueError) as exc:
                raise ValueError("published_reader_port_invalid:%s" % lane_code) from exc
            if not reader_code or not reader_serial or port_no < 1 or port_no > 16:
                raise ValueError("published_reader_port_identity_missing:%s" % lane_code)
            previous_serial = serial_by_reader.get(reader_code)
            if previous_serial and previous_serial != reader_serial:
                raise ValueError("published_reader_serial_conflict:%s" % reader_code)
            serial_by_reader[reader_code] = reader_serial
            runtime_ref = (reader_code, port_no)
            if runtime_ref in timeline_refs:
                raise ValueError("published_reader_port_duplicated:%s:%s" % runtime_ref)
            timeline_refs.add(runtime_ref)
            ports_by_reader.setdefault(reader_code, set()).add(port_no)
            runtime_timeline.append({
                "sequence": int(point.get("sequence") or 0),
                "reader_code": reader_code,
                "port_no": port_no,
                "duration_from_previous_seconds": float(
                    point.get("duration_from_previous_seconds") or 0.0
                ),
                "cumulative_time_seconds": float(
                    point.get("cumulative_time_seconds") or 0.0
                ),
            })

        runtime_timeline.sort(
            key=lambda row: (row["sequence"], row["reader_code"], row["port_no"])
        )
        if len(runtime_timeline) < 2:
            raise ValueError("published_reader_port_timeline_insufficient:%s" % lane_code)
        if [row["sequence"] for row in runtime_timeline] != list(range(1, len(runtime_timeline) + 1)):
            raise ValueError("published_reader_port_timeline_sequence_invalid:%s" % lane_code)

        source_sequences = lane.get("event_sequences") or {}
        if not isinstance(source_sequences, dict):
            raise ValueError("published_event_sequences_invalid:%s" % lane_code)
        runtime_sequences = {}
        for event_type in ("check_in", "check_out"):
            source_steps = source_sequences.get(event_type) or []
            if not isinstance(source_steps, list):
                raise ValueError("published_event_sequence_invalid:%s:%s" % (lane_code, event_type))
            runtime_steps = []
            seen_steps = set()
            for step in source_steps:
                if not isinstance(step, dict):
                    raise ValueError("published_event_sequence_step_invalid:%s:%s" % (lane_code, event_type))
                reader_code = str(step.get("reader_code") or "").strip().upper()
                try:
                    port_no = int(step.get("port_no") or 0)
                except (TypeError, ValueError) as exc:
                    raise ValueError("published_event_sequence_port_invalid:%s:%s" % (lane_code, event_type)) from exc
                ref = (reader_code, port_no)
                if ref not in timeline_refs:
                    raise ValueError(
                        "published_event_sequence_reader_port_not_found:%s:%s:%s"
                        % (lane_code, reader_code or "UNKNOWN", port_no)
                    )
                if ref in seen_steps:
                    raise ValueError(
                        "published_event_sequence_reader_port_duplicated:%s:%s:%s"
                        % (lane_code, reader_code, port_no)
                    )
                seen_steps.add(ref)
                runtime_steps.append({"reader_code": reader_code, "port_no": port_no})
            if runtime_steps and len(runtime_steps) < 2:
                raise ValueError("published_event_sequence_insufficient:%s:%s" % (lane_code, event_type))
            runtime_sequences[event_type] = runtime_steps
        if not any(runtime_sequences.values()):
            raise ValueError("published_lane_event_sequence_missing:%s" % lane_code)

        source_readers = lane.get("readers") or []
        if not isinstance(source_readers, list):
            raise ValueError("published_readers_invalid:%s" % lane_code)
        readers = []
        reader_codes = set()
        for reader in source_readers:
            if not isinstance(reader, dict):
                raise ValueError("published_reader_payload_invalid:%s" % lane_code)
            reader_code = str(reader.get("technical_code") or "").strip().upper()
            serial_number = str(reader.get("serial_number") or "").strip().upper()
            if not reader_code or not serial_number:
                raise ValueError("published_reader_identity_missing:%s" % lane_code)
            if reader_code in reader_codes:
                raise ValueError("published_reader_duplicated:%s" % reader_code)
            if serial_by_reader.get(reader_code) != serial_number:
                raise ValueError("published_reader_serial_mismatch:%s" % reader_code)
            reader_codes.add(reader_code)
            reader_parameters = reader.get("reader_parameters") or {}
            if not isinstance(reader_parameters, dict):
                raise ValueError("published_reader_parameters_invalid:%s" % reader_code)
            declared_ports = reader.get("ports") or []
            if not isinstance(declared_ports, list):
                raise ValueError("published_reader_ports_invalid:%s" % reader_code)
            declared_port_numbers = set()
            for port in declared_ports:
                if not isinstance(port, dict):
                    raise ValueError("published_reader_port_payload_invalid:%s" % reader_code)
                try:
                    port_no = int(port.get("port_no") or 0)
                except (TypeError, ValueError) as exc:
                    raise ValueError("published_reader_port_invalid:%s" % reader_code) from exc
                if port_no < 1 or port_no > 16:
                    raise ValueError("published_reader_port_invalid:%s:%s" % (reader_code, port_no))
                declared_port_numbers.add(port_no)
            expected_ports = ports_by_reader.get(reader_code, set())
            if declared_port_numbers != expected_ports:
                raise ValueError("published_reader_ports_mismatch:%s" % reader_code)
            readers.append({
                "technical_code": reader_code,
                "serial_number": serial_number,
                "reader_name": str(reader.get("reader_name") or serial_number).strip(),
                "physical_connection": reader.get("physical_connection") or False,
                "reader_parameters": {
                    "power_dbm": int(reader_parameters.get("power_dbm") or 0),
                    "read_interval_ms": int(reader_parameters.get("read_interval_ms") or 0),
                    "tid_start_address": int(reader_parameters.get("tid_start_address") or 0),
                    "tid_length": int(reader_parameters.get("tid_length") or 0),
                },
                "ports": [{"port_no": port_no} for port_no in sorted(expected_ports)],
            })

        missing_readers = sorted(set(ports_by_reader) - reader_codes)
        if missing_readers:
            raise ValueError("published_timeline_reader_missing:%s" % ",".join(missing_readers))

        tolerance = lane.get("timing_tolerance") or {}
        if not isinstance(tolerance, dict):
            raise ValueError("published_timing_tolerance_invalid:%s" % lane_code)
        return ({
            "lane_code": lane_code,
            "lane_name": str(lane.get("lane_name") or lane_code).strip(),
            "server_code": server_code,
            "controller_code": controller_code,
            "reader_port_timeline": runtime_timeline,
            "event_sequences": runtime_sequences,
            "timing_tolerance": {
                "type": str(tolerance.get("type") or "percent").strip().lower(),
                "value": float(tolerance.get("value") or 0.0),
            },
        }, readers, server_code, controller_code)

    @api.model
    def _published_gatekeeper_projection(self, edge):
        edge_code = str(edge.edge_server_code or "").strip().upper()
        if not edge_code:
            raise ValueError("edge_server_code_missing")

        Area = self.env["nsp.parking.area"].sudo()
        areas = Area.search([
            ("published_payload_json", "!=", False),
            ("published_edge_server_codes", "ilike", edge_code),
        ], order="code,id").filtered(
            lambda area: area.is_published_for_edge(edge_code)
        )

        area_payloads = []
        branch_ids = set()
        referenced_codes = set()
        expected_types = {}
        controller_map = {}
        reader_owner = {}

        def register_identity(code, device_type):
            previous_type = expected_types.get(code)
            if previous_type and previous_type != device_type:
                raise ValueError("published_device_role_conflict:%s" % code)
            expected_types[code] = device_type
            referenced_codes.add(code)

        for area in areas:
            payload = self._published_parking_payload_for_edge(area, edge_code)
            if not payload:
                continue
            if not area.branch_id:
                raise ValueError("published_parking_area_branch_missing:%s" % area.code)
            branch_ids.add(area.branch_id.id)
            runtime_lanes = []
            for lane in payload.get("lanes") or []:
                runtime_lane, readers, server_code, controller_code = (
                    self._runtime_lane_projection(lane)
                )
                if server_code != edge_code:
                    raise ValueError("published_server_does_not_match_edge:%s" % server_code)
                runtime_lanes.append(runtime_lane)
                register_identity(server_code, "SERVER")
                register_identity(controller_code, "CONTROLLER")
                controller = controller_map.setdefault(controller_code, {
                    "controller_code": controller_code,
                    "controller_name": controller_code,
                    "server_code": server_code,
                    "active": True,
                    "devices": {},
                })
                if controller["server_code"] != server_code:
                    raise ValueError("controller_published_on_multiple_servers:%s" % controller_code)
                for reader_payload in readers:
                    reader_code = reader_payload["technical_code"]
                    register_identity(reader_code, "RFID_READER")
                    owner = reader_owner.get(reader_code)
                    if owner and owner != controller_code:
                        raise ValueError("reader_published_on_multiple_controllers:%s" % reader_code)
                    reader_owner[reader_code] = controller_code
                    previous = controller["devices"].get(reader_code)
                    if previous and previous != reader_payload:
                        raise ValueError(
                            "reader_published_with_conflicting_configuration:%s" % reader_code
                        )
                    controller["devices"][reader_code] = reader_payload
            area_payloads.append({
                "parking_area_code": payload["parking_area_code"],
                "parking_area_name": payload["parking_area_name"],
                "branch_code": payload["branch_code"],
                "state": payload["state"],
                "published_revision": payload["published_revision"],
                "lanes": runtime_lanes,
            })

        Whitelist = self.env["nsp.device.whitelist"].sudo().with_context(active_test=False)
        identities = Whitelist.search([
            ("technical_code", "in", sorted(referenced_codes)),
        ], order="technical_code,id") if referenced_codes else Whitelist.browse()
        identity_by_code = {}
        for record in identities:
            code = str(record.technical_code or "").strip().upper()
            if code in identity_by_code:
                raise ValueError("published_device_identity_duplicated:%s" % code)
            identity_by_code[code] = record
        missing = sorted(referenced_codes - set(identity_by_code))
        if missing:
            raise ValueError("published_device_identity_missing:%s" % ",".join(missing))
        inactive = sorted(
            code for code, record in identity_by_code.items() if not record.active
        )
        if inactive:
            raise ValueError("published_device_identity_inactive:%s" % ",".join(inactive))
        type_mismatches = []
        for code in referenced_codes:
            actual_type = str(
                identity_by_code[code].device_type_code or "UNKNOWN"
            ).strip().upper()
            if actual_type != expected_types[code]:
                type_mismatches.append(
                    "%s:%s:%s" % (code, expected_types[code], actual_type)
                )
        type_mismatches.sort()
        if type_mismatches:
            raise ValueError(
                "published_device_identity_type_mismatch:%s"
                % ",".join(type_mismatches)
            )

        for controller_code, controller in controller_map.items():
            identity = identity_by_code[controller_code]
            controller["controller_name"] = identity.name or identity.technical_code
            controller["devices"] = sorted(
                controller["devices"].values(),
                key=lambda row: (
                    row.get("serial_number") or "",
                    row.get("technical_code") or "",
                ),
            )

        branches = self.env["nsp.branch"].sudo().browse(sorted(branch_ids))
        type_order = {"SERVER": 1, "CONTROLLER": 2, "RFID_READER": 3}
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
            } for branch in branches.sorted(
                key=lambda row: (row.code or "", row.id)
            )],
            "controllers": sorted(
                controller_map.values(),
                key=lambda row: row["controller_code"],
            ),
            "parking_areas": area_payloads,
            "device_whitelist": whitelist_payload,
        }

    @endpoint("NSP Edge Parking Runtime Snapshot", route_path="edge/parking-runtime/snapshot", methods="POST", code="nsp_edge_parking_runtime_snapshot")
    def api_parking_runtime_snapshot(self):
        data = self._payload()
        edge, error = self._auth_edge_server_sync(data)
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
        }, message="Published Parking runtime snapshot loaded.")

    @endpoint("NSP Edge Vehicle Reference Snapshot", route_path="edge/vehicle-reference/snapshot", methods="POST", code="nsp_edge_vehicle_reference_snapshot")
    def api_vehicle_reference_snapshot(self):
        data = self._payload()
        edge_server, error = self._auth_edge_snapshot_request(data)
        if error:
            return error
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
            **self._snapshot_meta(edge_server, "vehicle_reference"),
        }, message="Vehicle reference snapshot loaded.")

    @endpoint("NSP Edge RFID Assignments Snapshot", route_path="edge/rfid-assignments/snapshot", methods="POST", code="nsp_edge_rfid_assignments_snapshot")
    def api_rfid_assignments_snapshot(self):
        data = self._payload()
        edge_server, error = self._auth_edge_snapshot_request(data)
        if error:
            return error
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
            **self._snapshot_meta(edge_server, "rfid_assignments"),
        }, message="RFID assignments snapshot loaded.")

    @endpoint("NSP Edge Users Snapshot", route_path="edge/users/snapshot", methods="POST", code="nsp_edge_users_snapshot")
    def api_users_snapshot(self):
        data = self._payload()
        edge_server, error = self._auth_edge_snapshot_request(data)
        if error:
            return error
        User = self.env["nsp.user"].sudo()
        users = User.search([("user_code","!=",False),("user_code","!=","")], order="user_code,id")
        items = []
        for user in users:
            item = {
                "user_code": self._user_code(user),
                "name": user.name or user.display_name,
                "active": bool(user.active),
            }
            items.append(item)
        return self._ok({
            "items": items,
            **self._snapshot_meta(edge_server, "users"),
        }, message="Users snapshot loaded.")

    @endpoint("NSP Edge Vehicles Snapshot", route_path="edge/vehicles/snapshot", methods="POST", code="nsp_edge_vehicles_snapshot")
    def api_vehicles_snapshot(self):
        data = self._payload()
        edge_server, error = self._auth_edge_snapshot_request(data)
        if error:
            return error
        Vehicle = self.env["nsp.vehicle"].sudo()
        vehicles = Vehicle.search([], order="vehicle_code,id")
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
            "items": items,
            **self._snapshot_meta(edge_server, "vehicles"),
        }, message="Vehicles snapshot loaded.")

    @endpoint("NSP Edge Vehicle Borrows Snapshot", route_path="edge/vehicle-borrows/snapshot", methods="POST", code="nsp_edge_vehicle_borrows_snapshot")
    def api_vehicle_borrows_snapshot(self):
        data = self._payload()
        edge_server, error = self._auth_edge_snapshot_request(data)
        if error:
            return error
        if "nsp.vehicle.borrow" not in self.env.registry.models:
            return self._ok({
                "items": [],
                **self._snapshot_meta(edge_server, "vehicle_borrows"),
            }, message="Vehicle borrows snapshot loaded.")
        Borrow = self.env["nsp.vehicle.borrow"].sudo()
        records = Borrow.search([], order="borrow_code,id")
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
            "items": items,
            **self._snapshot_meta(edge_server, "vehicle_borrows"),
        }, message="Vehicle borrows snapshot loaded.")

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
            port_rows = [
                {"port_no": int(port.port_no or 0)}
                for port in line.reader_port_ids.sorted(key=lambda item: (item.port_no, item.id))
            ]
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
                "ports": port_rows,
            })
        vehicles = []
        targets = session._sync_vehicle_targets() if hasattr(session, "_sync_vehicle_targets") else [
            {"vehicle_tid": line.vehicle_tid, "vehicle": line.vehicle_id, "license_plate": line.license_plate}
            for line in session.target_line_ids
        ]
        for row in sorted(targets, key=lambda item: ((item.get("license_plate") or ""), item.get("vehicle_tid") or "")):
            vehicle = row.get("vehicle")
            item = {
                "vehicle_tid": row.get("vehicle_tid") or "",
                "vehicle_code": vehicle.vehicle_code or "" if vehicle else "",
                "license_plate": row.get("license_plate") or "",
            }
            owner_code = self._user_code(vehicle.owner_id) if vehicle and vehicle.owner_id else ""
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
            payload["port_summary"] = [
                {
                    **row,
                    "first_read_at": self._iso_datetime(row.get("first_read_at")),
                    "last_read_at": self._iso_datetime(row.get("last_read_at")),
                }
                for row in session._port_summary()
            ]
        return payload

    @api.model
    def _measurement_error_response(self, exc):
        text = str(exc)
        code = text.split(":", 1)[0].strip()
        status = 400
        if code.endswith("_not_found") or code in {"controller_not_found", "reader_port_not_found"}:
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

    @endpoint("NSP Edge Lane Calibration Snapshot", route_path="edge/lane-calibrations/snapshot", methods="POST", code="nsp_edge_lane_calibration_snapshot")
    def api_lane_calibration_snapshot(self):
        data = self._payload()
        edge_server, error = self._auth_edge_server_sync(data)
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
            }, message="Lane Calibration snapshot loaded.")
        except Exception as exc:
            return self._measurement_error_response(exc)

    @api.model
    def _measurement_event_values(
        self, session, item, allowed_reader_ports=None, accept_snapshot=False,
    ):
        allowed = {
            "event_uid", "serial_number", "port_no", "tid", "read_at", "rssi_dbm",
            "revision", "power_dbm", "read_interval_ms",
        }
        self._measurement_reject_unknown_fields(item, allowed)
        self._measurement_require_fields(item, ["event_uid", "serial_number", "port_no", "tid", "read_at"])
        event_uid = str(item.get("event_uid") or "").strip()
        serial_number = str(item.get("serial_number") or "").strip().upper()
        tid = self.env["nsp.rfid.tag"].sudo()._normalize_tid(item.get("tid"))
        try:
            port_no = int(item.get("port_no") or 0)
        except Exception:
            port_no = 0
        if port_no <= 0:
            raise ValueError("reader_port_not_found")
        reader_line = session._measurement_line_for_serial(serial_number)
        if allowed_reader_ports is None:
            allowed_reader_ports = session._allowed_reader_port_pairs()
        if (serial_number, port_no) not in allowed_reader_ports:
            raise ValueError("reader_port_not_found")
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
            "port_no": port_no,
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
            and int(event.port_no or 0) == int(values["port_no"] or 0)
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
        Event = self.env["nsp.measurement.event"].sudo()
        allowed_reader_ports = session._allowed_reader_port_pairs()
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
                    allowed_reader_ports=allowed_reader_ports,
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
                    and int(first["port_no"]) == int(values["port_no"])
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

    @endpoint("NSP Edge Lane Calibration Events", route_path="edge/lane-calibrations/events", methods="POST", code="nsp_edge_lane_calibration_events")
    def api_lane_calibration_events(self):
        data = self._payload()
        edge_server, error = self._auth_edge_server_sync(data)
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
            return self._ok(result, message="Lane Calibration Events synchronized.")
        except Exception as exc:
            return self._measurement_error_response(exc)

    @endpoint("NSP Edge Lane Calibration Status", route_path="edge/lane-calibrations/status", methods="POST", code="nsp_edge_lane_calibration_status")
    def api_lane_calibration_status(self):
        data = self._payload()
        edge_server, error = self._auth_edge_server_sync(data)
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
            return self._ok({"data": self._measurement_session_payload(session)}, message="Lane Calibration Status synchronized.")
        except Exception as exc:
            return self._measurement_error_response(exc)

    @api.model
    def _prepare_parking_transaction_sync_cache(self, edge_server, items):
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

    @endpoint("NSP Edge Parking Transactions", route_path="edge/parking-transactions", methods="POST", code="nsp_edge_parking_transactions")
    def api_parking_transactions(self):
        data = self._payload()
        edge_server, error = self._auth_edge_server_sync(data)
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
