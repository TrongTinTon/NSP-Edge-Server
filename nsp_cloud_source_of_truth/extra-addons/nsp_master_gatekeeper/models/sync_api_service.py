from datetime import datetime, timezone
import logging

from odoo import api, fields, models
from odoo.addons.t4_coreapi.utils import endpoint, get_body

from .state_policy import classify_idempotent_replay, compare_revision
from .lane_calibration.calibration_session import _normalize_raw_tid_value

_logger = logging.getLogger(__name__)

_DURATION_EPSILON_SECONDS = 0.001


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
            _logger.debug("Unable to decode Core API request body", exc_info=True)
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
        except (TypeError, ValueError):
            try:
                parsed = fields.Datetime.to_datetime(text)
            except (TypeError, ValueError):
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
        current_status = str(data.get("status") or "online").strip().lower()
        if current_status not in ("online", "offline", "error", "block", "revoked"):
            raise ValueError("invalid_payload")
        last_seen_at = self._safe_datetime_value(data.get("last_seen_at"), default_now=False) or fields.Datetime.now()
        values = {"timestamp": last_seen_at}
        if parent.status != current_status:
            values["status"] = current_status
        parent.with_context(tracking_disable=True, mail_notrack=True).write(values)
        return parent

    @api.model
    def _edge_runtime_status_scope(self, edge_server):
        """Build status scope from published contextual mappings.

        Reader inventory has no Controller owner. Parking Lane and Lane
        Calibration mappings explicitly define which Controller may report each
        Reader in the current runtime context. Reader Code remains the stable
        management identity.
        """
        edge_code = str(edge_server.edge_server_code or "").strip().upper()
        Controller = self.env["nsp.controller"].sudo().with_context(active_test=False)
        Device = self.env["nsp.device"].sudo().with_context(active_test=False)
        controller_codes = set()
        reader_codes_by_controller_code = {}
        direct_device_by_scope = {}

        def register_reader(controller_code, reader_code, device=False):
            normalized_controller = str(controller_code or "").strip().upper()
            normalized_reader = str(reader_code or "").strip().upper()
            if not normalized_controller or not normalized_reader:
                return
            controller_codes.add(normalized_controller)
            reader_codes_by_controller_code.setdefault(
                normalized_controller, set()
            ).add(normalized_reader)
            if device:
                direct_device_by_scope[(normalized_controller, normalized_reader)] = device

        Area = self.env["nsp.parking.area"].sudo()
        areas = Area.search([
            ("published_payload_json", "!=", False),
            ("published_edge_server_codes", "ilike", edge_code),
        ], order="code,id").filtered(lambda area: area.is_published_for_edge(edge_code))
        for area in areas:
            payload = self._published_parking_payload_for_edge(area, edge_code)
            if not payload:
                continue
            for lane in payload.get("lanes") or []:
                controller_code = str(
                    lane.get("controller_code") or ""
                ).strip().upper()
                if controller_code:
                    controller_codes.add(controller_code)
                for reader in lane.get("readers") or []:
                    register_reader(
                        reader.get("controller_code") or controller_code,
                        reader.get("technical_code") or reader.get("reader_code"),
                    )
                # Lane Controller + Reader is an explicit runtime association,
                # not a permanent Reader ownership relation.

        ReaderLine = self.env["nsp.measurement.reader.line"].sudo()
        calibration_lines = ReaderLine.search([
            ("edge_server_id", "=", edge_server.id),
            ("session_id.status", "in", ["ready", "running"]),
        ])
        for line in calibration_lines:
            register_reader(
                line.controller_id.controller_id,
                line.reader_id.device_code,
                device=line.reader_id,
            )

        controllers = Controller.search([
            ("controller_id", "in", sorted(controller_codes)),
        ]) if controller_codes else Controller.browse()
        controller_by_code = {
            str(record.controller_id or "").strip().upper(): record
            for record in controllers
        }

        scoped_reader_codes = sorted({
            code
            for codes in reader_codes_by_controller_code.values()
            for code in codes
        })
        candidate_devices = Device.search([
            ("device_code", "in", scoped_reader_codes),
        ]) if scoped_reader_codes else Device.browse()
        candidates_by_code = {}
        for device in candidate_devices:
            code = str(device.device_code or "").strip().upper()
            candidates_by_code.setdefault(code, Device.browse())
            candidates_by_code[code] |= device

        device_by_key = {}
        devices_by_controller = {}
        for controller_code, reader_codes in reader_codes_by_controller_code.items():
            controller = controller_by_code.get(controller_code)
            if not controller:
                continue
            for reader_code in sorted(reader_codes):
                device = direct_device_by_scope.get((controller_code, reader_code))
                if not device:
                    candidates = candidates_by_code.get(reader_code, Device.browse())
                    device = candidates[:1] if len(candidates) == 1 else Device.browse()
                if not device:
                    continue
                device_by_key[(controller.id, reader_code)] = device
                current = devices_by_controller.get(controller.id, Device.browse())
                devices_by_controller[controller.id] = current | device

        return {
            "controller_by_code": controller_by_code,
            "device_by_key": device_by_key,
            "devices_by_controller": devices_by_controller,
        }

    @api.model
    def _apply_device_status(self, controller, item, cache):
        if not isinstance(item, dict):
            raise ValueError("invalid_payload")
        allowed_fields = {
            "reader_code", "serial_number", "detected_serial_number", "status",
            "last_seen_at", "last_detection_at", "last_detection_port_no",
            "firmware_version", "power_dbm", "read_interval_ms",
        }
        unsupported = sorted(set(item) - allowed_fields)
        if unsupported:
            raise ValueError("unsupported_field:%s" % ",".join(unsupported))
        reader_code = str(item.get("reader_code") or "").strip().upper()
        if not reader_code:
            raise ValueError("reader_code is required")
        serial_number = str(item.get("serial_number") or "").strip().upper()
        if not serial_number:
            raise ValueError("serial_number is required")

        device = cache.get("device_by_key", {}).get((controller.id, reader_code))
        if not device:
            raise ValueError("reader_not_managed_by_runtime")
        if not device.active:
            raise ValueError("device_inactive")

        status = str(item.get("status") or "offline").strip().lower()
        if status not in ("online", "offline", "degraded"):
            raise ValueError("invalid_status")
        last_seen_at = self._safe_datetime_value(item.get("last_seen_at"), default_now=False)
        last_detection_at = self._safe_datetime_value(
            item.get("last_detection_at"), default_now=False
        )
        values = {"status": status}
        seen_candidates = [
            value for value in (last_seen_at, last_detection_at) if value
        ]
        if seen_candidates:
            values["last_seen"] = max(
                fields.Datetime.to_datetime(value) for value in seen_candidates
            )
        elif status == "online":
            values["last_seen"] = fields.Datetime.now()

        detected_serial = str(
            item.get("detected_serial_number") or ""
        ).strip().upper()
        if not detected_serial and serial_number != str(device.serial_number or "").strip().upper():
            detected_serial = serial_number
        if detected_serial:
            values["runtime_detected_serial_number"] = detected_serial
        if last_detection_at:
            values["runtime_last_detection_at"] = last_detection_at
        if item.get("last_detection_port_no") not in (None, ""):
            port_no = int(item.get("last_detection_port_no") or 0)
            if port_no < 0 or port_no > 16:
                raise ValueError("invalid_last_detection_port_no")
            values["runtime_last_detection_port_no"] = port_no
        if item.get("firmware_version") not in (None, ""):
            values["firmware_version"] = str(item.get("firmware_version"))
        if item.get("power_dbm") not in (None, ""):
            power = int(item.get("power_dbm"))
            if power < 0 or power > 40:
                raise ValueError("invalid_power_dbm")
            values["runtime_power_dbm"] = power
        if item.get("read_interval_ms") not in (None, ""):
            interval = int(item.get("read_interval_ms"))
            if interval <= 0 or interval > 60000:
                raise ValueError("invalid_read_interval_ms")
            values["runtime_read_interval_ms"] = interval

        # Reader Code and the released runtime scope own the Cloud record.  The
        # inventory ownership is not used; the explicit contextual mapping does.
        device.write(values)
        return device

    @endpoint("NSP Edge Status", route_path="edge/status", methods="POST", code="nsp_edge_status")
    def api_edge_status(self):
        data = self._payload()
        edge_server, error = self._auth_edge_server_sync(data)
        if error:
            return error
        heartbeat_data = dict(data)
        heartbeat_data["_heartbeat_received"] = True
        heartbeat_data.setdefault("status", "online")
        self._update_edge_server_status_from_payload(edge_server, heartbeat_data)

        controller_items = data.get("controllers") or []
        if not isinstance(controller_items, list):
            return self._error(
                "controllers must be an array", 400, error_code="invalid_payload",
                details={"field": "controllers"},
            )

        runtime_scope = self._edge_runtime_status_scope(edge_server)
        controller_by_code = runtime_scope["controller_by_code"]
        device_cache = runtime_scope
        controllers = self.env["nsp.controller"].sudo().browse(
            [record.id for record in controller_by_code.values()]
        )

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
                    raise ValueError("controller_not_managed_by_runtime")
                if not controller.active:
                    raise ValueError("controller_inactive")
                if controller.status in ("block", "revoked"):
                    raise ValueError("controller_blocked")

                controller_status = str(controller_item.get("status") or "offline").strip().lower()
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
                reported_reader_codes = {
                    str(item.get("reader_code") or "").strip().upper()
                    for item in devices if isinstance(item, dict)
                    and str(item.get("reader_code") or "").strip()
                }
                for device_index, device_item in enumerate(devices):
                    reader_code = str(
                        device_item.get("reader_code") or ""
                    ).strip().upper() if isinstance(device_item, dict) else ""
                    try:
                        device = self._apply_device_status(controller, device_item, cache=device_cache)
                        device_count += 1
                        results.append({
                            "controller_index": controller_index,
                            "device_index": device_index,
                            "controller_code": controller_code,
                            "record_key": device.device_code,
                            "status": "processed",
                            "message": "Processed",
                        })
                    except Exception as exc:
                        failed += 1
                        results.append({
                            "controller_index": controller_index,
                            "device_index": device_index,
                            "controller_code": controller_code,
                            "record_key": reader_code,
                            "status": "rejected",
                            "message": str(exc),
                        })

                managed_devices = device_cache.get("devices_by_controller", {}).get(
                    controller.id, self.env["nsp.device"].browse()
                )
                missing_devices = managed_devices.filtered(
                    lambda record: str(record.device_code or "").strip().upper()
                    not in reported_reader_codes
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

        missing_controllers = controllers.filtered(
            lambda record: record.active
            and record.id not in reported_controller_ids
            and record.status not in ("offline", "block", "revoked")
        )
        if missing_controllers:
            controllers_marked_offline = len(missing_controllers)
            missing_controllers.write({"status": "offline"})
            missing_devices = self.env["nsp.device"].browse()
            for controller in missing_controllers:
                missing_devices |= device_cache.get(
                    "devices_by_controller", {}
                ).get(controller.id, self.env["nsp.device"].browse())
            missing_devices = missing_devices.filtered(
                lambda record: record.status != "offline"
            )
            if missing_devices:
                devices_marked_offline += len(missing_devices)
                missing_devices.write({"status": "offline"})

        return self._ok({
            "edge_server_code": edge_server.edge_server_code,
            "status": edge_server.status,
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
        """Normalize one published Lane without consulting inventory ownership.

        Server, Controller and Reader are independent identities. Their runtime
        association is carried by this Lane payload only.
        """
        lane_code = str(lane.get("lane_code") or "").strip().upper()
        server_code = str(lane.get("server_code") or "").strip().upper()
        controller_code = str(lane.get("controller_code") or "").strip().upper()
        if not lane_code:
            raise ValueError("published_lane_identity_missing")
        if not server_code:
            raise ValueError("published_lane_server_identity_missing:%s" % lane_code)
        if not controller_code:
            raise ValueError("published_lane_controller_identity_missing:%s" % lane_code)

        source_sequence = lane.get("antenna_sequence") or []
        if not isinstance(source_sequence, list):
            raise ValueError("published_antenna_sequence_invalid:%s" % lane_code)

        runtime_sequence = []
        ports_by_reader = {}
        serial_by_reader = {}
        sequence_refs = set()
        for point in source_sequence:
            if not isinstance(point, dict):
                raise ValueError("published_antenna_sequence_point_invalid:%s" % lane_code)
            reader_code = str(point.get("reader_code") or "").strip().upper()
            reader_serial = str(point.get("reader_serial_number") or "").strip().upper()
            try:
                sequence = int(point.get("sequence") or 0)
                port_no = int(point.get("port_no") or 0)
                duration = float(point.get("duration_from_previous_seconds") or 0.0)
            except (TypeError, ValueError) as exc:
                raise ValueError("published_antenna_sequence_value_invalid:%s" % lane_code) from exc
            if not reader_code or not reader_serial or not 1 <= port_no <= 16:
                raise ValueError("published_antenna_sequence_identity_missing:%s" % lane_code)
            previous_serial = serial_by_reader.get(reader_code)
            if previous_serial and previous_serial != reader_serial:
                raise ValueError("published_reader_serial_conflict:%s" % reader_code)
            serial_by_reader[reader_code] = reader_serial
            ref = (reader_code, port_no)
            if ref in sequence_refs:
                raise ValueError("published_antenna_sequence_duplicated:%s:%s" % ref)
            sequence_refs.add(ref)
            ports_by_reader.setdefault(reader_code, set()).add(port_no)
            runtime_sequence.append({
                "sequence": sequence,
                "reader_code": reader_code,
                "port_no": port_no,
                "duration_from_previous_seconds": duration,
            })

        runtime_sequence.sort(key=lambda row: (row["sequence"], row["reader_code"], row["port_no"]))
        if len(runtime_sequence) < 2:
            raise ValueError("published_antenna_sequence_insufficient:%s" % lane_code)
        if [row["sequence"] for row in runtime_sequence] != list(range(1, len(runtime_sequence) + 1)):
            raise ValueError("published_antenna_sequence_order_invalid:%s" % lane_code)
        if float(runtime_sequence[0]["duration_from_previous_seconds"] or 0.0) != 0.0:
            raise ValueError("published_antenna_sequence_first_duration_invalid:%s" % lane_code)
        if any(float(row["duration_from_previous_seconds"] or 0.0) <= 0.0 for row in runtime_sequence[1:]):
            raise ValueError("published_antenna_sequence_duration_invalid:%s" % lane_code)

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
                if not 1 <= port_no <= 16:
                    raise ValueError("published_reader_port_invalid:%s:%s" % (reader_code, port_no))
                declared_port_numbers.add(port_no)
            expected_ports = ports_by_reader.get(reader_code, set())
            if declared_port_numbers != expected_ports:
                raise ValueError("published_reader_ports_mismatch:%s" % reader_code)
            try:
                power_dbm = int(reader_parameters.get("power_dbm") or 0)
                read_interval_ms = int(reader_parameters.get("read_interval_ms") or 0)
                tid_start_address = int(reader_parameters.get("tid_start_address") or 0)
                tid_length = int(reader_parameters.get("tid_length") or 0)
            except (TypeError, ValueError) as exc:
                raise ValueError("published_reader_parameters_invalid:%s" % reader_code) from exc
            if not 0 <= power_dbm <= 40 or not 1 <= read_interval_ms <= 60000 or tid_start_address < 0 or tid_length <= 0:
                raise ValueError("published_reader_parameters_out_of_range:%s" % reader_code)
            readers.append({
                "technical_code": reader_code,
                "serial_number": serial_number,
                "reader_name": str(reader.get("reader_name") or serial_number).strip(),
                "physical_connection": reader.get("physical_connection") or False,
                "reader_parameters": {
                    "power_dbm": power_dbm,
                    "read_interval_ms": read_interval_ms,
                    "tid_start_address": tid_start_address,
                    "tid_length": tid_length,
                },
                "ports": [{"port_no": port_no} for port_no in sorted(expected_ports)],
            })

        missing_readers = sorted(set(ports_by_reader) - reader_codes)
        if missing_readers:
            raise ValueError("published_sequence_reader_missing:%s" % ",".join(missing_readers))

        tolerance = lane.get("timing_tolerance") or {}
        if not isinstance(tolerance, dict):
            raise ValueError("published_timing_tolerance_invalid:%s" % lane_code)
        return ({
            "lane_code": lane_code,
            "lane_name": str(lane.get("lane_name") or lane_code).strip(),
            "server_code": server_code,
            "controller_code": controller_code,
            "readers": readers,
            "antenna_sequence": runtime_sequence,
            "timing_tolerance": {
                "type": str(tolerance.get("type") or "percent").strip().lower(),
                "value": float(tolerance.get("value") or 0.0),
            },
        }, readers, server_code, controller_code)

    @api.model
    def _published_gatekeeper_projection(self, edge):
        """Build Edge projection from contextual Lane mappings only."""
        edge_code = str(edge.edge_server_code or "").strip().upper()
        if not edge_code:
            raise ValueError("edge_server_code_missing")

        Area = self.env["nsp.parking.area"].sudo()
        areas = Area.search([
            ("published_payload_json", "!=", False),
            ("published_edge_server_codes", "ilike", edge_code),
        ], order="code,id").filtered(lambda area: area.is_published_for_edge(edge_code))

        area_payloads = []
        branch_ids = set()
        referenced_codes = set()
        expected_types = {}

        def register_identity(code, device_type):
            normalized = str(code or "").strip().upper()
            previous_type = expected_types.get(normalized)
            if previous_type and previous_type != device_type:
                raise ValueError("published_device_role_conflict:%s" % normalized)
            expected_types[normalized] = device_type
            referenced_codes.add(normalized)

        for area in areas:
            payload = self._published_parking_payload_for_edge(area, edge_code)
            if not payload:
                continue
            if not area.branch_id:
                raise ValueError("published_parking_area_branch_missing:%s" % area.code)
            branch_ids.add(area.branch_id.id)
            runtime_lanes = []
            for lane in payload.get("lanes") or []:
                runtime_lane, readers, server_code, controller_code = self._runtime_lane_projection(lane)
                if server_code != edge_code:
                    raise ValueError("published_server_does_not_match_edge:%s" % server_code)
                runtime_lanes.append(runtime_lane)
                register_identity(server_code, "SERVER")
                register_identity(controller_code, "CONTROLLER")
                for reader in readers:
                    register_identity(reader["technical_code"], "RFID_READER")
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
        inactive = sorted(code for code, record in identity_by_code.items() if not record.active)
        if inactive:
            raise ValueError("published_device_identity_inactive:%s" % ",".join(inactive))
        mismatches = []
        for code in referenced_codes:
            actual = str(identity_by_code[code].device_type_code or "UNKNOWN").strip().upper()
            if actual != expected_types[code]:
                mismatches.append("%s:%s:%s" % (code, expected_types[code], actual))
        if mismatches:
            raise ValueError("published_device_identity_type_mismatch:%s" % ",".join(sorted(mismatches)))

        branches = self.env["nsp.branch"].sudo().browse(sorted(branch_ids))
        type_order = {"SERVER": 1, "CONTROLLER": 2, "RFID_READER": 3}
        whitelist_payload = [
            record._prepare_sync_payload()
            for record in identities.sorted(
                key=lambda row: (type_order.get(row.device_type_code, 9), row.technical_code or "", row.id)
            )
        ]
        return {
            "branches": [{
                "branch_code": branch.code,
                "branch_name": branch.name,
                "timezone": branch.timezone or "Asia/Ho_Chi_Minh",
                "active": branch.status == "active",
            } for branch in branches.sorted(key=lambda row: (row.code or "", row.id))],
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

    @endpoint("NSP Edge RFID Runtime Assignments Snapshot", route_path="edge/rfid-assignments/snapshot", methods="POST", code="nsp_edge_rfid_assignments_snapshot")
    def api_rfid_runtime_assignments_snapshot(self):
        data = self._payload()
        edge_server, error = self._auth_edge_snapshot_request(data)
        if error:
            return error
        projection = self.env["nsp.rfid.tag.assignment"].prepare_runtime_projection()
        projection.update(self._snapshot_meta(edge_server, "rfid_runtime_assignments"))
        return self._ok(projection, message="RFID runtime assignment snapshot loaded.")

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
        except (TypeError, ValueError):
            try:
                parsed = fields.Datetime.to_datetime(text)
            except (TypeError, ValueError) as exc:
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
        except (TypeError, ValueError):
            return 0

    @api.model
    def _measurement_session(self, measurement_code):
        code = str(measurement_code or "").strip().upper()
        if not code:
            raise ValueError("missing_lane_calibration_code")
        session = self.env["nsp.measurement.session"].sudo().search(
            [("measurement_code", "=", code)], limit=1
        )
        if not session:
            raise ValueError("lane_calibration_not_found")
        return session

    @api.model
    def _measurement_session_in_local_scope(self, session, edge_server):
        return bool(session.reader_line_ids.filtered(
            lambda line: line.edge_server_id == edge_server
        ))

    @api.model
    def _measurement_config_payload(self, session, edge_server=False):
        return session._calibration_sync_payload(edge_server=edge_server)

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
            "lane_calibration_not_editable",
            "invalid_status_transition",
            "lane_calibration_revision_ahead",
            "event_uid_conflict",
            "lane_calibration_not_running",
        }:
            status = 409
        return self._error(text.replace("_", " "), status, error_code=code, details={})

    @api.model
    def _measurement_set_status(
        self, session, status, occurred_at=False, message=False, revision=False,
    ):
        return session._apply_runtime_status(
            status,
            occurred_at=occurred_at,
            message=message,
            revision=revision,
        )

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
        try:
            tid = _normalize_raw_tid_value(item.get("tid"))
        except ValueError as exc:
            raise ValueError("invalid_measurement_tid") from exc
        try:
            port_no = int(item.get("port_no") or 0)
        except (TypeError, ValueError):
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
        return event.matches_measurement_values(values)

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
                try:
                    incoming_tid = _normalize_raw_tid_value(item.get("tid"))
                except ValueError as exc:
                    raise ValueError("invalid_measurement_tid") from exc
                if incoming_tid not in target_tids:
                    results[index] = {
                        "index": index,
                        "record_key": key,
                        "status": "ignored",
                        "message": "Raw TID does not match the active Lane Calibration Tag",
                    }
                    continue
                values = self._measurement_event_values(
                    session,
                    item,
                    allowed_reader_ports=allowed_reader_ports,
                    accept_snapshot=accept_snapshot,
                )
                if enforce_current_snapshot and (
                    compare_revision(values["revision"] or 1, session.revision or 1) != "current"
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
                replay = classify_idempotent_replay(
                    uid, self._measurement_event_matches(existing, values)
                )
                if replay == "conflict":
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
                    "error_code": "lane_calibration_not_running", "message": "lane_calibration_not_running",
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
                _logger.exception(
                    "Bulk Lane Calibration event create failed; isolating rows with savepoints"
                )
                created_records = Event.browse()
                failed_attempts = {}
                for uid, (index, key, values) in pending_by_uid.items():
                    try:
                        with self.env.cr.savepoint():
                            event = Event.create(values)
                        created_records |= event
                        results[index] = {
                            "index": index, "record_key": key, "status": "processed",
                            "message": "Processed",
                        }
                    except Exception as exc:
                        failed_attempts[uid] = (index, key, values, exc)

                concurrent_by_uid = {
                    event.event_uid: event
                    for event in Event.search([
                        ("event_uid", "in", list(failed_attempts)),
                    ])
                } if failed_attempts else {}
                for uid, (index, key, values, exc) in failed_attempts.items():
                    existing = concurrent_by_uid.get(uid)
                    if existing and self._measurement_event_matches(existing, values):
                        results[index] = {
                            "index": index, "record_key": key, "status": "duplicate",
                            "message": "Already processed",
                        }
                        continue
                    error = ValueError("event_uid_conflict") if existing else exc
                    results[index] = {
                        "index": index, "record_key": key, "status": "rejected",
                        "error_code": str(error).split(":", 1)[0],
                        "message": str(error),
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
            self._measurement_reject_unknown_fields(
                data, {"edge_server_code", "lane_calibration_code", "events"}
            )
            self._measurement_require_fields(data, ["lane_calibration_code", "events"])
            session = self._measurement_session(data.get("lane_calibration_code"))
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
            self._measurement_reject_unknown_fields(
                data,
                {
                    "edge_server_code", "lane_calibration_code", "status",
                    "revision", "occurred_at", "message",
                },
            )
            self._measurement_require_fields(
                data, ["lane_calibration_code", "status", "occurred_at"]
            )
            session = self._measurement_session(data.get("lane_calibration_code"))
            if not self._measurement_session_in_local_scope(session, edge_server):
                raise ValueError("edge_server_not_in_scope")
            occurred_at = self._measurement_datetime(data.get("occurred_at"), required=True)
            status_result = self._measurement_set_status(
                session,
                data.get("status"),
                occurred_at,
                data.get("message"),
                revision=data.get("revision"),
            )
            return self._ok(
                {
                    "data": self._measurement_session_payload(session),
                    "status_sync": status_result,
                },
                message="Lane Calibration Status synchronized.",
            )
        except Exception as exc:
            return self._measurement_error_response(exc)

    @api.model
    def _prepare_parking_transaction_sync_cache(self, edge_server, items):
        rows = [item for item in (items or []) if isinstance(item, dict)]
        controller_codes = {
            str(item.get("controller_code") or "").strip().upper()
            for item in rows
        }
        area_codes = {str(item.get("parking_area_code") or "").strip().upper() for item in rows}
        lane_codes = {str(item.get("lane_code") or "").strip().upper() for item in rows}
        serials = {str(item.get("serial_number") or "").strip().upper() for item in rows}
        vehicle_codes = {str(item.get("vehicle_code") or "").strip().upper() for item in rows}
        user_codes = {str(item.get("user_code") or "").strip().upper() for item in rows}
        borrow_codes = {str(item.get("borrow_uid") or "").strip() for item in rows}
        uids = {str(item.get("transaction_uid") or "").strip() for item in rows}
        for values in (controller_codes, area_codes, lane_codes, serials, vehicle_codes, user_codes, borrow_codes, uids):
            values.discard("")

        Controller = self.env["nsp.controller"].sudo().with_context(active_test=False)
        controllers = Controller.search([
            ("controller_id", "in", list(controller_codes)),
        ]) if controller_codes else Controller.browse()
        controller_by_code = {
            str(record.controller_id or "").strip().upper(): record
            for record in controllers
        }

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
        device_by_serial = {
            str(record.serial_number or "").strip().upper(): record
            for record in devices if record.serial_number
        }

        Vehicle = self.env["nsp.vehicle"].sudo().with_context(active_test=False)
        vehicles = Vehicle.search([
            ("vehicle_code", "in", list(vehicle_codes)),
        ]) if vehicle_codes else Vehicle.browse()
        User = self.env["nsp.user"].sudo().with_context(active_test=False)
        users = User.search([
            ("user_code", "in", list(user_codes)),
        ]) if user_codes else User.browse()
        Borrow = self.env["nsp.vehicle.borrow"].sudo()
        borrows = Borrow.search([
            ("borrow_code", "in", list(borrow_codes)),
        ]) if borrow_codes else Borrow.browse()

        Transaction = self.env["nsp.parking.transaction"].sudo()
        existing = Transaction.search([("transaction_uid", "in", list(uids))]) if uids else Transaction.browse()
        return {
            "controller_by_code": controller_by_code,
            "area_by_code": area_by_code,
            "lane_by_key": lane_by_key,
            "device_by_serial": device_by_serial,
            "vehicle_by_code": {record.vehicle_code: record for record in vehicles},
            "user_by_code": {record.user_code: record for record in users},
            "borrow_by_code": {record.borrow_code: record for record in borrows},
            "transaction_by_uid": {record.transaction_uid: record for record in existing},
        }

    @api.model
    def _upsert_parking_transaction_sync(self, edge_server, item, cache=None):
        if not isinstance(item, dict):
            raise ValueError("invalid_payload")
        allowed_fields = {
            "record_key", "transaction_uid", "controller_code", "parking_area_code", "lane_code",
            "layout_revision", "sequence_path", "observed_duration_seconds",
            "allowed_duration_seconds", "serial_number", "port_no", "event_type", "event_time",
            "vehicle_tid", "vehicle_code", "license_plate", "user_tid", "user_code",
            "observed_user_tids", "observed_user_codes", "borrow_uid",
            "decision", "decision_reason_code",
            "decision_message",
        }
        unsupported = sorted(set(item) - allowed_fields)
        if unsupported:
            raise ValueError(
                "invalid_payload: unsupported field(s): %s" % ", ".join(unsupported)
            )

        uid = str(item.get("transaction_uid") or "").strip()
        record_key = str(item.get("record_key") or uid).strip()
        existing_transaction = ((cache or {}).get("transaction_by_uid") or {}).get(uid)
        if not existing_transaction and cache is None and uid:
            existing_transaction = self.env["nsp.parking.transaction"].sudo().search([
                ("transaction_uid", "=", uid),
            ], limit=1)
        if record_key != uid:
            raise ValueError("record_key_transaction_uid_mismatch")
        controller_code = str(item.get("controller_code") or "").strip().upper()
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
            layout_revision = int(item.get("layout_revision") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid_layout_revision") from exc
        if layout_revision <= 0:
            raise ValueError("invalid_layout_revision")
        sequence_path = str(item.get("sequence_path") or "").strip()
        if not sequence_path:
            raise ValueError("missing_sequence_path")
        try:
            observed_duration_seconds = float(item.get("observed_duration_seconds") or 0.0)
            allowed_duration_seconds = float(item.get("allowed_duration_seconds") or 0.0)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid_sequence_duration") from exc
        if observed_duration_seconds < 0 or allowed_duration_seconds <= 0:
            raise ValueError("invalid_sequence_duration")
        if observed_duration_seconds > allowed_duration_seconds + _DURATION_EPSILON_SECONDS:
            raise ValueError("invalid_duration_snapshot")
        if observed_duration_seconds > allowed_duration_seconds:
            observed_duration_seconds = allowed_duration_seconds
        observed_duration_seconds = round(observed_duration_seconds, 6)
        allowed_duration_seconds = round(allowed_duration_seconds, 6)
        try:
            port_no = int(item.get("port_no") or 0)
        except Exception as exc:
            raise ValueError("invalid_port_no") from exc
        if port_no < 1 or port_no > 16:
            raise ValueError("invalid_port_no")

        use_cache = cache is not None
        cache = cache or {}
        if use_cache:
            controller = cache["controller_by_code"].get(controller_code)
        else:
            controller = self.env["nsp.controller"].sudo().with_context(active_test=False).search([
                ("controller_id", "=", controller_code),
            ], limit=1)
        # A retry of an already accepted immutable UID must remain idempotent.
        # Server/Controller/Reader are independent identities; the immutable
        # published Lane mapping is the only authority for Edge routing.
        if (
            controller
            and not existing_transaction
            and not self._parking_transaction_matches_published_route(
                edge_server, item, cache
            )
        ):
            raise ValueError("controller_not_in_edge_scope")

        if use_cache:
            parking_area = cache["area_by_code"].get(parking_area_code)
        else:
            parking_area = self.env["nsp.parking.area"].sudo().search([
                ("code", "=", parking_area_code),
            ], limit=1)

        lane = self.env["nsp.parking.lane"].browse()
        device = self.env["nsp.device"].browse()
        if controller and parking_area:
            if use_cache:
                lane = cache["lane_by_key"].get((controller.id, parking_area_code, lane_code)) or lane
            else:
                lane = self.env["nsp.parking.lane"].sudo().with_context(active_test=False).search([
                    ("parking_area_id", "=", parking_area.id),
                    ("controller_id", "=", controller.id),
                    ("code", "=", lane_code),
                ], limit=1)
        if use_cache:
            device = cache["device_by_serial"].get(serial_number) or device
        else:
            device = self.env["nsp.device"].sudo().search([
                ("serial_number", "=", serial_number),
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
        vehicle_code = str(item.get("vehicle_code") or "").strip().upper()
        user_tid = str(item.get("user_tid") or "").strip()
        user_code = str(item.get("user_code") or "").strip().upper()
        observed_user_tids = ",".join(sorted({
            value.strip().upper()
            for value in str(item.get("observed_user_tids") or "").split(",")
            if value.strip()
        }))
        observed_user_codes = ",".join(sorted({
            value.strip().upper()
            for value in str(item.get("observed_user_codes") or "").split(",")
            if value.strip()
        }))
        borrow_code = str(item.get("borrow_uid") or "").strip()
        if event_type == "check_in" and (
            user_tid or user_code or observed_user_tids or observed_user_codes or borrow_code
        ):
            raise ValueError("check_in_cannot_have_user_identity")
        if event_type == "check_out" and decision == "allowed":
            if not user_tid or not user_code:
                raise ValueError("allowed_check_out_requires_user_identity")
            if observed_user_tids != user_tid or observed_user_codes != user_code:
                raise ValueError("allowed_check_out_identity_snapshot_mismatch")
        if use_cache:
            vehicle = cache["vehicle_by_code"].get(vehicle_code) or self.env["nsp.vehicle"].browse()
            user = cache["user_by_code"].get(user_code) or self.env["nsp.user"].browse()
            borrow = cache["borrow_by_code"].get(borrow_code) or self.env["nsp.vehicle.borrow"].browse()
        else:
            vehicle = self.env["nsp.vehicle"].sudo().with_context(active_test=False).search([
                ("vehicle_code", "=", vehicle_code),
            ], limit=1) if vehicle_code else self.env["nsp.vehicle"].browse()
            user = self.env["nsp.user"].sudo().with_context(active_test=False).search([
                ("user_code", "=", user_code),
            ], limit=1) if user_code else self.env["nsp.user"].browse()
            borrow = self.env["nsp.vehicle.borrow"].sudo().search([
                ("borrow_code", "=", borrow_code),
            ], limit=1) if borrow_code else self.env["nsp.vehicle.borrow"].browse()
        if vehicle_code and not vehicle:
            raise ValueError("vehicle_not_found")
        if user_code and not user:
            raise ValueError("user_not_found")
        if borrow and vehicle and borrow.vehicle_id != vehicle:
            raise ValueError("borrow_vehicle_mismatch")
        if borrow and user and borrow.borrower_id != user:
            raise ValueError("borrow_user_mismatch")
        reason_code = Transaction._normalize_error_code(
            item.get("decision_reason_code"), item.get("decision_message")
        )
        if decision == "denied" and not reason_code:
            reason_code = "unknown"
        if reason_code == "multiple_user_tags" and len(observed_user_tids.split(",")) < 2:
            raise ValueError("multiple_user_tags_requires_identity_snapshot")
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
            "layout_revision": layout_revision,
            "sequence_path": sequence_path,
            "observed_duration_seconds": observed_duration_seconds,
            "allowed_duration_seconds": allowed_duration_seconds,
            "reader_id": device.id if device else False,
            "serial_number": serial_number,
            "port_no": port_no,
            "event_type": event_type,
            "status": decision,
            "error_code": reason_code or False,
            "error_message": str(item.get("decision_message") or "").strip() or False,
            "vehicle_id": vehicle.id if vehicle else False,
            "vehicle_code": vehicle_code or False,
            "license_plate": str(item.get("license_plate") or (vehicle.license_plate if vehicle else "")).strip() or False,
            "vehicle_tid": vehicle_tid or False,
            "user_id": user.id if user else False,
            "user_code": user_code or False,
            "user_tid": user_tid or False,
            "observed_user_codes": observed_user_codes or False,
            "observed_user_tids": observed_user_tids or False,
            "borrow_id": borrow.id if borrow else False,
            "borrow_code": borrow_code or False,
        }
        return Transaction.create_idempotent(
            vals, existing_by_uid=cache.get("transaction_by_uid")
        )

    @api.model
    def _parking_transaction_matches_published_route(self, edge_server, item, cache):
        """Validate routing against the immutable published Layout snapshot."""
        if not isinstance(item, dict):
            return False
        area_code = str(item.get("parking_area_code") or "").strip().upper()
        lane_code = str(item.get("lane_code") or "").strip().upper()
        controller_code = str(item.get("controller_code") or "").strip().upper()
        area = ((cache or {}).get("area_by_code") or {}).get(area_code)
        if not area and area_code:
            area = self.env["nsp.parking.area"].sudo().search([
                ("code", "=", area_code),
            ], limit=1)
        try:
            incoming_revision = int(item.get("layout_revision") or 0)
        except (TypeError, ValueError):
            return False
        if (
            not area
            or incoming_revision <= 0
            or incoming_revision != int(area.published_revision or 0)
        ):
            return False
        try:
            payload = self._published_parking_payload_for_edge(
                area, edge_server.edge_server_code
            )
        except Exception:
            _logger.exception(
                "Failed to validate published Parking Layout payload",
                extra={
                    "parking_area_code": area.code if area else area_code,
                    "incoming_revision": incoming_revision,
                },
            )
            return False
        if not payload or int(payload.get("published_revision") or 0) != incoming_revision:
            return False
        return any(
            str(lane.get("lane_code") or "").strip().upper() == lane_code
            and str(lane.get("controller_code") or "").strip().upper() == controller_code
            and str(lane.get("server_code") or "").strip().upper()
                == str(edge_server.edge_server_code or "").strip().upper()
            for lane in (payload.get("lanes") or [])
            if isinstance(lane, dict)
        )

    @api.model
    def _parking_transaction_sync_preflight(self, edge_server, item, cache):
        """Classify stale or invalid queued transactions before immutable upsert."""
        if not isinstance(item, dict):
            return False
        uid = str(item.get("transaction_uid") or "").strip()
        if not uid:
            return False
        if (cache.get("transaction_by_uid") or {}).get(uid):
            return False

        try:
            observed = float(item.get("observed_duration_seconds") or 0.0)
            allowed = float(item.get("allowed_duration_seconds") or 0.0)
        except (TypeError, ValueError):
            return False
        if allowed > 0 and observed > allowed + _DURATION_EPSILON_SECONDS:
            return {
                "status": "ignored",
                "error_code": "invalid_duration_snapshot",
                "message": (
                    "Transaction ignored because observed duration %.6fs exceeds "
                    "the published allowed duration %.6fs." % (observed, allowed)
                ),
            }

        controller_code = str(item.get("controller_code") or "").strip().upper()
        controller = (cache.get("controller_by_code") or {}).get(controller_code)
        if (
            controller
            and not self._parking_transaction_matches_published_route(
                edge_server, item, cache
            )
        ):
            area_code = str(item.get("parking_area_code") or "").strip().upper()
            area = (cache.get("area_by_code") or {}).get(area_code)
            try:
                incoming_revision = int(item.get("layout_revision") or 0)
            except (TypeError, ValueError):
                incoming_revision = 0
            current_revision = int(area.published_revision or 0) if area else 0
            if current_revision and incoming_revision and incoming_revision < current_revision:
                return {
                    "status": "ignored",
                    "error_code": "stale_controller_route",
                    "message": (
                        "Historical transaction ignored because its Layout revision "
                        "predates the current Controller-to-Edge route."
                    ),
                }
            return {
                "status": "rejected",
                "error_code": "controller_not_in_edge_scope",
                "message": (
                    "Controller %s is not assigned to authenticated Edge Server %s."
                    % (controller_code, edge_server.edge_server_code)
                ),
            }
        return False

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
        processed = failed = ignored = 0
        for idx, item in enumerate(incoming):
            key = str(item.get("transaction_uid") or "").strip() if isinstance(item, dict) else ""
            legacy_reason = (
                str(item.get("decision_reason_code") or "").strip().lower()
                if isinstance(item, dict) else ""
            )
            if legacy_reason in ("continuity_duplicate", "check_out_without_check_in"):
                ignored += 1
                message = (
                    "Legacy duplicate movement ignored"
                    if legacy_reason == "continuity_duplicate"
                    else "Legacy Check-out without a previous Check-in ignored"
                )
                results.append({
                    "index": idx,
                    "record_key": key,
                    "status": "ignored",
                    "message": message,
                })
                continue
            disposition = self._parking_transaction_sync_preflight(
                edge_server, item, cache
            )
            if disposition:
                results.append({"index": idx, "record_key": key, **disposition})
                if disposition["status"] == "ignored":
                    ignored += 1
                else:
                    failed += 1
                continue
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
                message = str(exc)
                error_code = message.split(":", 1)[0].strip() or "rejected"
                results.append({
                    "index": idx,
                    "record_key": key,
                    "status": "rejected",
                    "error_code": error_code,
                    "message": message,
                })
        return self._ok({
            "received": len(incoming),
            "processed": processed,
            "ignored": ignored,
            "failed": failed,
            "results": results,
        }, message="Parking transactions synced.")
