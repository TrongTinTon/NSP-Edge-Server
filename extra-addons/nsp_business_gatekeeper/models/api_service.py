# -*- coding: utf-8 -*-
import json
import logging
from datetime import datetime, timezone

from odoo import api, fields, models
from odoo.addons.t4_coreapi.utils import endpoint, get_body

_logger = logging.getLogger(__name__)

class NspBusinessGatekeeperApiService(models.AbstractModel):
    _name = 'nsp.business.gatekeeper.api.service'
    _description = 'NspBusinessGatekeeperApiService'

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

    @endpoint("NSP Gatekeeper Health", route_path="health", methods="GET", code="nsp_gatekeeper_health")
    def api_health(self):
        return self._ok({
            "service": "nsp_business_gatekeeper",
            "status": "running",
            "server_time": self._iso_datetime(fields.Datetime.now()),
        }, message="NSP Business Gatekeeper is running.")

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
              JOIN nsp_device_type AS device_type
                ON device_type.id = whitelist.device_type_id
             WHERE device.controller_id = %s
               AND whitelist.active = TRUE
               AND device_type.code = 'RFID_READER'
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
            ("active", "=", True),
            ("device_type_code", "=", "RFID_READER"),
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
        normalized_ports = None
        if reported_ports is not None:
            if not isinstance(reported_ports, list):
                raise ValueError("ports must be an array")
            normalized_ports = []
            seen_ports = set()
            for value in reported_ports:
                if isinstance(value, bool):
                    raise ValueError("invalid_port_number")
                try:
                    port_no = int(value)
                except (TypeError, ValueError) as exc:
                    raise ValueError("invalid_port_number") from exc
                if port_no < 1 or port_no > 16 or port_no in seen_ports:
                    raise ValueError("invalid_port_number")
                seen_ports.add(port_no)
                normalized_ports.append(port_no)
            normalized_ports.sort()

        last_seen_at = self._safe_datetime_value(
            item.get("last_seen_at"), default_now=False
        )
        vals = {"status": status}
        if last_seen_at:
            vals["last_seen"] = last_seen_at
        elif status == "online":
            vals["last_seen"] = fields.Datetime.now()
        if normalized_ports is not None:
            vals["runtime_ports_json"] = json.dumps(
                normalized_ports, separators=(",", ":")
            )
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
        devices = self._whitelisted_devices(controller.device_ids.filtered("active")).sorted(
            key=lambda rec: (rec.serial_number or "", rec.id)
        )
        return self._ok({
            "controller_code": controller.controller_id,
            "devices": [device._build_config_payload() for device in devices],
            "server_time": self._iso_datetime(fields.Datetime.now()),
        }, message="Controller device configuration loaded.")

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
    def _measurement_config_payload(self, session, controller=False):
        lines = session.reader_line_ids
        if controller:
            lines = lines.filtered(
                lambda line: line.reader_id.controller_id == controller
            )
        readers = []
        for line in lines.sorted(
            key=lambda item: ((item.reader_id.serial_number or ""), item.id)
        ):
            readers.append({
                "serial_number": line.reader_id.serial_number or "",
                "power_dbm": int(line.reader_power_dbm or 0),
                "read_interval_ms": int(line.read_interval_ms or 200),
                "ports": sorted(line.reader_port_ids.mapped("port_no")),
            })
        return {
            "measurement_code": session.measurement_code,
            "controller_code": controller.controller_id if controller else "",
            "status": session.status,
            "desired_state": (
                "running" if session.status in ("ready", "running") else "stopped"
            ),
            "revision": int(session.revision or 1),
            "readers": readers,
        }


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
                    ("reader_line_ids.reader_id.controller_id", "=", controller.id),
                ], limit=1)
                if current and current.status in ("completed", "failed", "cancelled"):
                    session = current
            if not session:
                session = self.env["nsp.measurement.session"].sudo().search([
                    ("reader_line_ids.reader_id.controller_id", "=", controller.id),
                    ("status", "in", ["ready", "running"]),
                ], order="id asc", limit=1)
            if not session:
                return self._ok({"data": {"measurement_available": False}}, message="No Measurement Session is available.")
            return self._ok({"data": {"measurement_available": True, **self._measurement_config_payload(session, controller=controller)}}, message="Measurement configuration loaded.")
        except Exception as exc:
            return self._measurement_error_response(exc)

    @api.model
    def _measurement_event_values(
        self, session, item, allowed_reader_ports=None, accept_snapshot=False,
        allow_historical_scope=False,
    ):
        allowed = {
            "event_uid", "serial_number", "port_no", "tid", "read_at", "rssi_dbm",
            "revision", "power_dbm", "read_interval_ms",
        }
        self._measurement_reject_unknown_fields(item, allowed)
        self._measurement_require_fields(
            item, ["event_uid", "serial_number", "port_no", "tid", "read_at"]
        )
        event_uid = str(item.get("event_uid") or "").strip()
        serial_number = str(item.get("serial_number") or "").strip().upper()
        tid = self.env["nsp.rfid.runtime.assignment"].sudo()._normalize_tid(item.get("tid"))
        try:
            port_no = int(item.get("port_no") or 0)
        except Exception:
            port_no = 0
        if port_no < 1 or port_no > 16:
            raise ValueError("reader_port_not_found")
        reader_line = session._measurement_line_for_serial(serial_number)
        if allow_historical_scope:
            reader = self.env["nsp.device"].sudo().search([
                ("serial_number", "=", serial_number),
            ], limit=1)
            if not reader:
                raise ValueError("reader_not_in_scope")
        else:
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
            except Exception as exc:
                raise ValueError("invalid_rssi") from exc
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
            and fields.Datetime.to_string(event.read_at)
            == fields.Datetime.to_string(values["read_at"])
            and int(event.read_at_ms or 0) == int(values["read_at_ms"] or 0)
            and (
                False if event.rssi_dbm in (False, None)
                else float(event.rssi_dbm)
            )
            == (
                False if values["rssi_dbm"] in (False, None)
                else float(values["rssi_dbm"])
            )
            and int(event.power_dbm or 0) == int(values["power_dbm"] or 0)
            and int(event.read_interval_ms or 0)
            == int(values["read_interval_ms"] or 0)
        )


    @api.model
    def _measurement_process_event_batch(
        self, session, items, allow_final=False, accept_snapshot=False,
        enforce_current_snapshot=False, allow_historical_scope=False, controller=False,
    ):
        """Store only selected RFID targets, idempotently, with bounded queries."""
        Event = self.env["nsp.measurement.event"].sudo()
        if controller:
            lines = session.reader_line_ids.filtered(lambda line: line.reader_id.controller_id == controller)
            allowed_reader_ports = {
                ((line.reader_id.serial_number or "").strip().upper(), int(port.port_no or 0))
                for line in lines
                for port in line.reader_port_ids
            }
        else:
            allowed_reader_ports = session._allowed_reader_port_pairs()
        target_tids = session._allowed_target_tids()
        prepared = []
        results = [None] * len(items)

        for index, item in enumerate(items):
            key = str(item.get("event_uid") or "") if isinstance(item, dict) else ""
            try:
                if not isinstance(item, dict):
                    raise ValueError("invalid_payload")
                incoming_tid = self.env["nsp.rfid.runtime.assignment"].sudo()._normalize_tid(item.get("tid"))
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
                    allow_historical_scope=allow_historical_scope,
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

    @api.model
    def _forward_measurement_events_now(self, events):
        if not events or "nsp.sync.job" not in self.env.registry.models:
            return False
        try:
            return self.env["nsp.sync.job"].sudo().push_lane_calibration_events_now(events)
        except Exception:
            _logger.exception("Immediate Lane Calibration Event forwarding failed; fallback retry will handle it.")
            return False

    @api.model
    def _forward_measurement_status_now(self, session):
        if not session or "nsp.sync.job" not in self.env.registry.models:
            return False
        try:
            return self.env["nsp.sync.job"].sudo().push_lane_calibration_status_now(session)
        except Exception:
            _logger.exception("Immediate Lane Calibration Status forwarding failed; fallback retry will handle it.")
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
            if controller not in session.reader_line_ids.mapped("reader_id.controller_id"):
                raise ValueError("controller_not_in_scope")
            items = data.get("events")
            if not isinstance(items, list) or not items or len(items) > 100:
                raise ValueError("invalid_payload")
            result, records = self._measurement_process_event_batch(
                session, items, accept_snapshot=True, enforce_current_snapshot=True,
                controller=controller,
            )
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
            if controller not in session.reader_line_ids.mapped("reader_id.controller_id"):
                raise ValueError("controller_not_in_scope")
            occurred_at = self._measurement_datetime(data.get("occurred_at"), required=True)
            self._measurement_set_status(session, data.get("status"), occurred_at, data.get("message"))
            self._forward_measurement_status_now(session)
            return self._ok({"data": self._measurement_session_payload(session)}, message="Measurement status recorded.")
        except Exception as exc:
            return self._measurement_error_response(exc)

    @endpoint("NSP Controller Parking Detection Push", route_path="parking/detections/push", methods="POST", code="nsp_controller_parking_detection_push")
    def api_parking_detection_push(self):
        """Accept a batch of raw TID detections from one authenticated Controller.

        The Controller only reports physical detections. One batch may contain
        detections from multiple Reader ports owned by that Controller.
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

        item_fields = {"event_uid", "serial_number", "port_no", "detected_at", "tid"}
        normalized = []
        tids = set()
        RuntimeAssignment = self.env["nsp.rfid.runtime.assignment"].sudo()

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
            tid = RuntimeAssignment._normalize_tid(item.get("tid"))
            detected_at = self._safe_datetime_value(item.get("detected_at"), default_now=False)
            try:
                port_no = int(item.get("port_no") or 0)
            except (TypeError, ValueError):
                port_no = 0

            missing = []
            if not event_uid:
                missing.append("event_uid")
            if not serial_number:
                missing.append("serial_number")
            if port_no < 1 or port_no > 16:
                missing.append("port_no")
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
                "port_no": port_no,
                "detected_at": detected_at,
                "tid": tid,
            }
            normalized.append(payload)
            tids.add(tid)

        assignments = RuntimeAssignment.search([
            ("tid", "in", list(tids)),
        ]) if tids else RuntimeAssignment.browse()
        assignment_by_tid = {
            assignment.tid: assignment
            for assignment in assignments
            if (assignment.user_id and assignment.user_id.active)
            or (assignment.vehicle_id and assignment.vehicle_id.active)
        }
        accepted = [
            (payload, assignment_by_tid[payload["tid"]])
            for payload in normalized
            if payload["tid"] in assignment_by_tid
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

