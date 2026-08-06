# -*- coding: utf-8 -*-
import json
import logging
from datetime import datetime, timezone

from odoo import api, fields, models
from odoo.addons.t4_coreapi.utils import endpoint, get_body

_logger = logging.getLogger(__name__)

_LANE_CALIBRATION_CLOUD_STATUSES = frozenset({"draft", "ready", "applied"})
_LANE_CALIBRATION_RUNTIME_STATUSES = frozenset({"running", "completed", "failed", "cancelled"})
_LANE_CALIBRATION_ALL_STATUSES = (
    _LANE_CALIBRATION_CLOUD_STATUSES | _LANE_CALIBRATION_RUNTIME_STATUSES
)
_LANE_CALIBRATION_RUNTIME_TRANSITIONS = {
    "draft": frozenset({"cancelled"}),
    "ready": frozenset({"running", "completed", "failed", "cancelled"}),
    "running": frozenset({"completed", "failed", "cancelled"}),
    "completed": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
    "applied": frozenset(),
}
_LANE_CALIBRATION_STALE_RUNTIME_TARGETS = {
    "completed": frozenset({"running"}),
    "failed": frozenset({"running"}),
    "cancelled": frozenset({"running"}),
}


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
            "status": "online",
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
    def _normalize_observation_ports(self, values):
        if values is None:
            return []
        if not isinstance(values, list):
            raise ValueError("ports must be an array")
        result = []
        seen = set()
        for value in values:
            if isinstance(value, bool):
                raise ValueError("invalid_port_number")
            try:
                port_no = int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError("invalid_port_number") from exc
            if port_no <= 0 or port_no > 16 or port_no in seen:
                raise ValueError("invalid_port_number")
            seen.add(port_no)
            result.append(port_no)
        return sorted(result)

    @api.model
    def _apply_reader_observation(self, controller, item, cache=None):
        """Cache a physical observation exactly as reported by Controller.

        No whitelist, Parking Layout, Lane Calibration or assignment decision is
        made here. Unknown SDK SerialNumbers are valid physical observations.
        """
        if not isinstance(item, dict):
            raise ValueError("invalid_payload")
        allowed_fields = {
            "serial_number", "endpoint", "ports", "status",
            "last_seen_at", "firmware_version", "power_dbm", "read_interval_ms",
        }
        unsupported = sorted(set(item) - allowed_fields)
        if unsupported:
            raise ValueError("unsupported_field:%s" % ",".join(unsupported))

        serial_number = str(item.get("serial_number") or "").strip().upper()
        if not serial_number:
            raise ValueError("serial_number is required")
        status = str(item.get("status") or "offline").strip().lower()
        if status not in ("online", "offline", "degraded"):
            raise ValueError("invalid_status")

        ports = self._normalize_observation_ports(item.get("ports"))
        last_seen_at = self._safe_datetime_value(item.get("last_seen_at"), default_now=False)
        now = fields.Datetime.now()
        values = {
            "endpoint": str(item.get("endpoint") or "").strip().upper() or False,
            "status": status,
            "last_reported_at": now,
            "ports_json": json.dumps(ports, separators=(",", ":")),
        }
        if last_seen_at:
            values["last_seen_at"] = last_seen_at
        elif status == "online":
            values["last_seen_at"] = now
        if item.get("firmware_version") not in (None, ""):
            values["firmware_version"] = str(item.get("firmware_version"))
        if item.get("power_dbm") not in (None, ""):
            power = int(item.get("power_dbm"))
            if power < 0 or power > 40:
                raise ValueError("invalid_power_dbm")
            values["power_dbm"] = power
        if item.get("read_interval_ms") not in (None, ""):
            interval = int(item.get("read_interval_ms"))
            if interval <= 0 or interval > 60000:
                raise ValueError("invalid_read_interval_ms")
            values["read_interval_ms"] = interval

        Observation = self.env["nsp.reader.observation"].sudo()
        observation = None
        if cache is not None:
            observation = cache.get(serial_number)
        if not observation:
            observation = Observation.search([
                ("controller_id", "=", controller.id),
                ("serial_number", "=", serial_number),
            ], limit=1)
        if observation:
            observation.write(values)
        else:
            values.update({
                "controller_id": controller.id,
                "serial_number": serial_number,
            })
            observation = Observation.create(values)
        if cache is not None:
            cache[serial_number] = observation
        return observation

    @api.model
    def _touch_reader_activity_from_detections(
        self, controller, items, timestamp_field, power_field=False, interval_field=False,
    ):
        """Update physical Reader activity from raw data-plane evidence.

        This function deliberately does not decide whether a tag, port, lane or
        calibration event is valid.  It only records that a Controller reported a
        physical detection from one SDK SerialNumber.
        """
        if not controller or not isinstance(items, list):
            return 0
        try:
            freshness_sec = int(
                self.env["ir.config_parameter"].sudo().get_param(
                    "nsp_business_gatekeeper.reader_detection_freshness_sec",
                    "300",
                ) or "300"
            )
        except Exception:
            freshness_sec = 300
        freshness_sec = min(max(freshness_sec, 30), 3600)

        latest_by_serial = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            serial = str(item.get("serial_number") or "").strip().upper()
            if not serial:
                continue
            detected_at = self._safe_datetime_value(
                item.get(timestamp_field), default_now=False
            )
            if not detected_at:
                continue
            try:
                port_no = int(item.get("port_no") or 0)
            except (TypeError, ValueError):
                port_no = 0
            if port_no < 1 or port_no > 16:
                continue
            parsed_at = fields.Datetime.to_datetime(detected_at)
            previous = latest_by_serial.get(serial)
            if previous and previous["detected_at"] > parsed_at:
                continue
            latest_by_serial[serial] = {
                "detected_at": parsed_at,
                "port_no": port_no,
                "power_dbm": item.get(power_field) if power_field else None,
                "read_interval_ms": item.get(interval_field) if interval_field else None,
            }

        Observation = self.env["nsp.reader.observation"].sudo()
        touched = 0
        for serial, values in latest_by_serial.items():
            Observation.touch_detection(
                controller,
                serial,
                detected_at=values["detected_at"],
                port_no=values["port_no"],
                power_dbm=values.get("power_dbm"),
                read_interval_ms=values.get("read_interval_ms"),
                freshness_sec=freshness_sec,
            )
            touched += 1
        return touched

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

        serials = {
            str(item.get("serial_number") or "").strip().upper()
            for item in items if isinstance(item, dict) and item.get("serial_number")
        }
        observations = self.env["nsp.reader.observation"].sudo().search([
            ("controller_id", "=", controller.id),
            ("serial_number", "in", list(serials)),
        ]) if serials else self.env["nsp.reader.observation"].browse()
        cache = {record.serial_number: record for record in observations}

        results = []
        processed = failed = 0
        for index, item in enumerate(items):
            key = str(item.get("serial_number") or "").strip().upper() if isinstance(item, dict) else ""
            try:
                observation = self._apply_reader_observation(controller, item, cache=cache)
                processed += 1
                results.append({
                    "index": index,
                    "record_key": observation.serial_number,
                    "status": "processed",
                    "message": "Observation cached",
                })
            except Exception as exc:
                failed += 1
                results.append({
                    "index": index,
                    "record_key": key,
                    "status": "rejected",
                    "message": str(exc),
                })
        return self._ok({
            "received": len(items),
            "processed": processed,
            "failed": failed,
            "results": results,
        }, message="Physical Reader observations cached.")

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
        Device = self.env["nsp.device"].sudo()
        devices = Device.browse()
        lanes = self.env["nsp.parking.lane"].sudo().search([
            ("active", "=", True),
            ("controller_id", "=", controller.id),
            ("parking_area_id.state", "in", ["operational", "maintenance", "blocked"]),
        ])
        devices |= lanes.mapped("timeline_line_ids.reader_id")
        calibration_lines = self.env["nsp.measurement.reader.line"].sudo().search([
            ("controller_id", "=", controller.id),
            ("session_id.status", "in", ["ready", "running"]),
        ])
        devices |= calibration_lines.mapped("reader_id")
        devices = devices.filtered(lambda rec: rec.active and not rec.cloud_removed).sorted(
            key=lambda rec: (rec.serial_number or "", rec.id)
        )

        parking_layouts = []
        for parking_area in lanes.mapped("parking_area_id").sorted(
            key=lambda rec: ((rec.code or "").casefold(), rec.id)
        ):
            controller_lanes = lanes.filtered(
                lambda lane: lane.parking_area_id == parking_area
            ).sorted(key=lambda lane: ((lane.code or "").casefold(), lane.id))
            parking_layouts.append({
                "parking_area_code": parking_area.code or "",
                "parking_area_name": parking_area.name or "",
                "state": parking_area.state or "",
                "published_revision": int(parking_area.published_revision or 0),
                "lanes": [
                    {
                        "lane_code": lane.code or "",
                        "lane_name": lane.name or "",
                    }
                    for lane in controller_lanes
                ],
            })

        return self._ok({
            "controller_code": controller.controller_id,
            "devices": [device._build_config_payload() for device in devices],
            "parking_layouts": parking_layouts,
            "server_time": self._iso_datetime(fields.Datetime.now()),
        }, message="Controller runtime configuration loaded.")

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
            raise ValueError("missing_lane_calibration_code")
        session = self.env["nsp.measurement.session"].sudo().search(
            [("measurement_code", "=", code)], limit=1
        )
        if not session:
            raise ValueError("lane_calibration_not_found")
        return session

    @api.model
    def _measurement_config_payload(self, session, controller=False):
        lines = session.reader_line_ids
        if controller:
            lines = lines.filtered(lambda line: line.controller_id == controller)
        readers = []
        for line in lines.sorted(
            key=lambda item: ((item.reader_id.serial_number or ""), item.id)
        ):
            readers.append({
                "serial_number": line.reader_id.serial_number or "",
                "power_dbm": int(line.reader_power_dbm or 0),
                "read_interval_ms": int(line.read_interval_ms or 200),
            })
        return {
            "lane_calibration_code": session.measurement_code,
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
        """Apply an external Lane Calibration runtime status safely.

        State ownership is intentionally asymmetric:
        - Cloud owns ``draft``, ``ready`` and ``applied`` (displayed as Configured).
        - Edge/Controller owns ``running``, ``completed``, ``failed`` and ``cancelled``.

        Cloud-owned states received through a runtime status endpoint are
        acknowledged but never applied. Once Cloud marks a revision Configured,
        every delayed runtime status for that same revision is stale and must be
        ACKed instead of returning ``invalid_status_transition``.
        """
        target = str(status or "").strip().lower()
        if target not in _LANE_CALIBRATION_ALL_STATUSES:
            raise ValueError("invalid_lane_calibration_status")

        current = str(session.status or "draft")
        current_revision = max(int(session.revision or 1), 1)
        try:
            incoming_revision = (
                current_revision
                if revision in (False, None, "")
                else int(revision)
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid_lane_calibration_revision") from exc
        if incoming_revision <= 0:
            raise ValueError("invalid_lane_calibration_revision")

        result = {
            "outcome": "duplicate",
            "incoming_status": target,
            "current_status": current,
            "incoming_revision": incoming_revision,
            "current_revision": current_revision,
            "status_owner": (
                "cloud" if target in _LANE_CALIBRATION_CLOUD_STATUSES else "runtime"
            ),
        }
        if incoming_revision < current_revision:
            result["outcome"] = "ignored_stale_revision"
            return result
        if incoming_revision > current_revision:
            raise ValueError("lane_calibration_revision_ahead")

        # Runtime clients may still retry legacy Ready/Applied records. ACK them
        # without allowing an Edge or Controller to drive Cloud-owned lifecycle.
        if target in _LANE_CALIBRATION_CLOUD_STATUSES and target != current:
            result["outcome"] = "ignored_cloud_owned_status"
            return result

        # Configured is the authoritative Cloud final state for this revision.
        # Any late Running/Completed/Failed/Cancelled status is superseded.
        if current == "applied" and target in _LANE_CALIBRATION_RUNTIME_STATUSES:
            result["outcome"] = "ignored_after_configured"
            return result

        if target != current:
            if target in _LANE_CALIBRATION_STALE_RUNTIME_TARGETS.get(current, frozenset()):
                result["outcome"] = "ignored_stale_status"
                return result
            if target not in _LANE_CALIBRATION_RUNTIME_TRANSITIONS.get(current, frozenset()):
                raise ValueError("invalid_status_transition")

        when = occurred_at or fields.Datetime.now()
        vals = {}
        if target != current:
            vals["status"] = target
            result["outcome"] = "applied"
            result["current_status"] = target
        if target == "running" and not session.started_at:
            vals["started_at"] = when
        if target in ("completed", "failed", "cancelled") and not session.ended_at:
            vals["ended_at"] = when
        if vals:
            session.with_context(measurement_sync=True).write(vals)
        if message and result["outcome"] == "applied":
            session.message_post(body=str(message))
        return result

    @endpoint(
        "NSP Controller Lane Calibration Pull",
        route_path="controller/lane-calibrations/pull",
        methods="POST",
        code="nsp_controller_lane_calibration_pull",
    )
    def api_controller_lane_calibration_pull(self):
        data = self._payload()
        controller, error = self._auth_controller(data)
        if error:
            return error
        try:
            self._measurement_reject_unknown_fields(
                data, {"controller_code", "current_lane_calibration_code"}
            )
            current_code = str(
                data.get("current_lane_calibration_code") or ""
            ).strip().upper()
            Session = self.env["nsp.measurement.session"].sudo()
            session = Session.browse()

            # The released Reader line is the authoritative runtime snapshot for
            # Controller scope. Do not derive scope again from the mutable Reader
            # ownership relation, which may temporarily lag behind sync/rebinding.
            if current_code:
                session = Session.search([
                    ("measurement_code", "=", current_code),
                    ("reader_line_ids.controller_id", "=", controller.id),
                    ("status", "in", ["ready", "running"]),
                ], limit=1)
            if not session:
                session = Session.search([
                    ("reader_line_ids.controller_id", "=", controller.id),
                    ("status", "in", ["ready", "running"]),
                ], order="id desc", limit=1)
            if not session:
                _logger.info(
                    "No active Lane Calibration for Controller: controller=%s current_code=%s",
                    controller.controller_id,
                    current_code or "<empty>",
                )
                return self._ok(
                    {"data": {
                        "lane_calibration_available": False,
                        "controller_code": controller.controller_id,
                        "reason": "no_active_session_for_controller",
                    }},
                    message="No Lane Calibration is available.",
                )
            return self._ok(
                {
                    "data": {
                        "lane_calibration_available": True,
                        **self._measurement_config_payload(session, controller=controller),
                    }
                },
                message="Lane Calibration configuration loaded.",
            )
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
            lines = session.reader_line_ids.filtered(lambda line: line.controller_id == controller)
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
                ):
                    results[index] = {
                        "index": index,
                        "record_key": key,
                        "status": "ignored",
                        "message": "Stale Lane Calibration revision ignored",
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
        processed = sum(1 for row in final_results if row["status"] == "processed")
        duplicates = sum(1 for row in final_results if row["status"] == "duplicate")
        ignored = sum(1 for row in final_results if row["status"] == "ignored")
        failed = sum(1 for row in final_results if row["status"] == "rejected")
        return {
            "received": len(items),
            "processed": processed,
            "duplicates": duplicates,
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

    @endpoint(
        "NSP Controller Lane Calibration Events",
        route_path="controller/lane-calibrations/events",
        methods="POST",
        code="nsp_controller_lane_calibration_events",
    )
    def api_controller_lane_calibration_events(self):
        """Accept raw detections and acknowledge the batch at HTTP level.

        Controller is an acquisition client. Edge owns duplicate detection,
        session/revision checks and Reader Port filtering. Per-item decisions
        are retained in Edge logs only and are not returned to Controller.
        """
        data = self._payload()
        controller, error = self._auth_controller(data)
        if error:
            return error
        try:
            self._measurement_reject_unknown_fields(
                data, {"controller_code", "lane_calibration_code", "events"}
            )
            self._measurement_require_fields(data, ["lane_calibration_code", "events"])
            session = self._measurement_session(data.get("lane_calibration_code"))
            if controller not in session.reader_line_ids.mapped("controller_id"):
                raise ValueError("controller_not_in_scope")
            items = data.get("events")
            if not isinstance(items, list) or not items or len(items) > 100:
                raise ValueError("invalid_payload")
            activity_touched = self._touch_reader_activity_from_detections(
                controller, items, "read_at",
                power_field="power_dbm",
                interval_field="read_interval_ms",
            )
            result, records = self._measurement_process_event_batch(
                session,
                items,
                accept_snapshot=True,
                enforce_current_snapshot=True,
                controller=controller,
            )
            if records and session.status == "ready":
                self._measurement_set_status(session, "running", fields.Datetime.now())
                self._forward_measurement_status_now(session)
            self._forward_measurement_events_now(records)
            decision_rows = [
                row for row in result.get("results", [])
                if row.get("status") in ("ignored", "rejected")
            ]
            if decision_rows:
                for row in decision_rows:
                    _logger.warning(
                        "Lane Calibration raw event decision: code=%s event_uid=%s "
                        "status=%s reason=%s",
                        session.measurement_code,
                        row.get("record_key") or "<missing>",
                        row.get("status") or "unknown",
                        row.get("message") or row.get("error_code") or "unspecified",
                    )
            _logger.info(
                "Lane Calibration raw batch processed: "
                "code=%s received=%s stored=%s duplicates=%s ignored=%s rejected=%s",
                session.measurement_code,
                result.get("received", 0),
                result.get("processed", 0),
                result.get("duplicates", 0),
                result.get("ignored", 0),
                result.get("failed", 0),
            )
            return self._ok(
                {
                    "data": {
                        "lane_calibration_code": session.measurement_code,
                        "received": int(result.get("received", 0)),
                        "stored": int(result.get("processed", 0)),
                        "duplicates": int(result.get("duplicates", 0)),
                        "ignored": int(result.get("ignored", 0)),
                        "rejected": int(result.get("failed", 0)),
                        "reader_activity_touched": int(activity_touched or 0),
                    }
                },
                message="Lane Calibration events received.",
            )
        except Exception as exc:
            _logger.exception("Lane Calibration raw batch processing failed")
            return self._error(
                "Lane Calibration event processing failed",
                500,
                error_code="lane_calibration_processing_failed",
                details={"reason": str(exc)},
            )

    @endpoint(
        "NSP Controller Lane Calibration Status",
        route_path="controller/lane-calibrations/status",
        methods="POST",
        code="nsp_controller_lane_calibration_status",
    )
    def api_controller_lane_calibration_status(self):
        data = self._payload()
        controller, error = self._auth_controller(data)
        if error:
            return error
        try:
            self._measurement_reject_unknown_fields(
                data,
                {
                    "controller_code", "lane_calibration_code", "status",
                    "revision", "occurred_at", "message",
                },
            )
            self._measurement_require_fields(
                data, ["lane_calibration_code", "status", "occurred_at"]
            )
            session = self._measurement_session(data.get("lane_calibration_code"))
            if controller not in session.reader_line_ids.mapped("controller_id"):
                raise ValueError("controller_not_in_scope")
            occurred_at = self._measurement_datetime(
                data.get("occurred_at"), required=True
            )
            status_result = self._measurement_set_status(
                session,
                data.get("status"),
                occurred_at,
                data.get("message"),
                revision=data.get("revision"),
            )
            if status_result["outcome"] == "applied":
                self._forward_measurement_status_now(session)
            return self._ok(
                {"status_sync": status_result},
                message="Lane Calibration status received.",
            )
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

        item_fields = {
            "event_uid", "serial_number", "port_no", "detected_at", "tid",
            "rssi_dbm", "rssi",
        }
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

            raw_rssi = item.get("rssi_dbm")
            if raw_rssi in (None, ""):
                raw_rssi = item.get("rssi")
            try:
                rssi_dbm = float(raw_rssi or 0.0)
            except (TypeError, ValueError):
                rssi_dbm = 0.0
            payload = {
                "event_uid": event_uid,
                "serial_number": serial_number,
                "port_no": port_no,
                "detected_at": detected_at,
                "tid": tid,
                "rssi_dbm": rssi_dbm,
            }
            normalized.append(payload)
            tids.add(tid)

        self._touch_reader_activity_from_detections(
            controller, normalized, "detected_at"
        )

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

