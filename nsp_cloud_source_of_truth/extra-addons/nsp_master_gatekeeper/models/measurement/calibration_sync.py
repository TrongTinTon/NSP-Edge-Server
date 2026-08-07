# -*- coding: utf-8 -*-
from odoo import api, fields, models, _

from ...services.calibration_status_policy import CalibrationStatusPolicy


class NspMeasurementSessionSync(models.Model):
    _inherit = "nsp.measurement.session"

    def _target_coverage(self):
        """Return Vehicle-level RFID detection coverage for the current revision."""
        self.ensure_one()
        targets = self.target_line_ids.sorted(
            key=lambda line: ((line.license_plate or ""), (line.vehicle_tid or ""), line.id)
        )
        rows = self.env["nsp.measurement.event"].sudo()._read_group(
            [("session_id", "=", self.id), ("revision", "=", self.revision)],
            ["tid"],
            ["__count", "read_at:min", "read_at:max"],
            order="tid",
        )
        stats = {
            tid: {
                "read_count": int(count or 0),
                "first_read_at": first_read,
                "last_read_at": last_read,
            }
            for tid, count, first_read, last_read in rows
        }
        result = []
        for line in targets:
            data = stats.get(line.vehicle_tid, {})
            read_count = int(data.get("read_count") or 0)
            result.append({
                "id": line.id,
                "tag_id": line.tag_id.id,
                "vehicle_tid": line.vehicle_tid or "",
                "vehicle_id": line.vehicle_id.id,
                "license_plate": line.license_plate or "",
                "owner_id": line.vehicle_id.owner_id.id if line.vehicle_id.owner_id else False,
                "owner_name": line.vehicle_id.owner_id.display_name if line.vehicle_id.owner_id else "",
                "detected": bool(read_count),
                "read_count": read_count,
                "first_read_at": fields.Datetime.to_string(data.get("first_read_at"))
                if data.get("first_read_at") else None,
                "last_read_at": fields.Datetime.to_string(data.get("last_read_at"))
                if data.get("last_read_at") else None,
            })
        return result

    @api.model
    def get_live_snapshot(self, session_id, last_event_id=0, limit=2000):
        try:
            record_id = int(session_id or 0)
            limit = min(max(int(limit or 2000), 100), 5000)
        except (TypeError, ValueError):
            record_id, limit = 0, 2000
        session = self.browse(record_id).exists()
        if not session:
            return {"found": False}
        session.check_access("read")
        events = self.env["nsp.measurement.event"].search(
            [("session_id", "=", session.id), ("revision", "=", session.revision)],
            order="read_at asc, read_at_ms asc, id asc",
            limit=limit,
        )
        steps = session._build_detection_steps(events)
        vehicles = session._target_coverage()
        detected_vehicle_count = sum(1 for vehicle in vehicles if vehicle["detected"])
        controllers = []
        for controller in session.reader_line_ids.mapped("controller_id").sorted(
            key=lambda item: ((item.controller_id or ""), item.id)
        ):
            edge_code = ""
            edge_status = ""
            line = session.reader_line_ids.filtered(lambda row: row.controller_id == controller)[:1]
            if line and line.edge_server_id:
                edge_code = line.edge_server_id.edge_server_code or ""
                edge_status = line.edge_server_id.status or ""
            controllers.append({
                "id": controller.id,
                "code": controller.controller_id or "",
                "name": controller.controller_name or "",
                "edge_server_code": edge_code,
                "edge_status": edge_status,
            })
        readers = []
        for line in session.reader_line_ids.sorted(
            key=lambda item: (
                (item.controller_id.controller_id or ""),
                (item.reader_id.name or ""),
                (item.reader_id.serial_number or ""),
                item.id,
            )
        ):
            reader = line.reader_id
            controller = line.controller_id
            readers.append({
                "reader_line_id": line.id,
                "id": reader.id,
                "name": reader.name or reader.serial_number or "",
                "serial_number": reader.serial_number or "",
                "controller_code": controller.controller_id or "",
                "controller_name": controller.controller_name or "",
                "status": reader.status or "",
                "runtime_power_dbm": int(reader.runtime_power_dbm or 0),
                "runtime_read_interval_ms": int(reader.runtime_read_interval_ms or 0),
                "reader_power_dbm": int(line.reader_power_dbm or 0),
                "read_interval_ms": int(line.read_interval_ms or 0),
                "firmware_version": reader.firmware_version or "",
                "ports": sorted(line.reader_port_ids.mapped("port_no")),
            })
        return {
            "found": True,
            "session_id": session.id,
            "deployment_role": session._deployment_role(),
            "measurement_code": session.measurement_code,
            "revision": int(session.revision or 1),
            "status": session.status,
            "controllers": controllers,
            "controller_count": len(controllers),
            "edge_server_codes": session._edge_server_codes(),
            "vehicles": vehicles,
            "vehicle_count": len(vehicles),
            "vehicle_tag_count": int(session.target_tag_count or 0),
            "detected_vehicle_count": detected_vehicle_count,
            "coverage_percent": round((detected_vehicle_count * 100.0 / len(vehicles)), 1) if vehicles else 0.0,
            "readers": readers,
            "reader_count": len(readers),
            "reader_online_count": sum(1 for reader in readers if reader.get("status") in ("online", "degraded")),
            "reader_offline_count": sum(1 for reader in readers if reader.get("status") == "offline"),
            "configured_reader_port_count": len({
                (reader["serial_number"], int(port_no or 0))
                for reader in readers
                for port_no in reader.get("ports", [])
            }),
            "started_at": fields.Datetime.to_string(session.started_at) if session.started_at else None,
            "ended_at": fields.Datetime.to_string(session.ended_at) if session.ended_at else None,
            "applied_at": fields.Datetime.to_string(session.applied_at) if session.applied_at else None,
            "raw_event_count": int(session.event_count or 0),
            "detection_count": len(steps),
            "unique_reader_ports": len({(step["serial_number"], step["port_no"]) for step in steps}),
            "unique_readers": len({step["serial_number"] for step in steps}),
            "unique_controllers": len({step["controller_code"] for step in steps}),
            "first_detection": steps[0] if steps else False,
            "last_detection": steps[-1] if steps else False,
            "timeline_total_duration": round(
                float(steps[-1].get("elapsed_from_start") or 0.0), 3
            ) if steps else 0.0,
            "steps": steps,
            "last_event_id": max(events.ids or [int(last_event_id or 0)]),
            "server_time": fields.Datetime.to_string(fields.Datetime.now()),
        }

    @api.model
    def get_infrastructure_scope_snapshot(self, session_id):
        """Return the Lane Calibration infrastructure topology and live health.

        Configuration ownership comes from ``nsp.measurement.reader.line``.
        Runtime health comes from the Edge Server, Controller and Reader
        heartbeat mirrors.  RFID activity is derived only from raw calibration
        observations for the current revision; it is not an Antenna health
        assertion.
        """
        try:
            record_id = int(session_id or 0)
        except (TypeError, ValueError):
            record_id = 0
        session = self.browse(record_id).exists()
        if not session:
            return {"found": False}
        session.check_access("read")

        now = fields.Datetime.now()
        parameter = self.env["ir.config_parameter"].sudo().get_param(
            "nsp_master_gatekeeper.lane_calibration_reader_silent_after_sec",
            "60",
        )
        try:
            silent_after_sec = int(parameter or "60")
        except (TypeError, ValueError):
            silent_after_sec = 60
        silent_after_sec = min(max(silent_after_sec, 15), 3600)

        lines = session.reader_line_ids.sorted(
            key=lambda line: (
                line.edge_server_id.edge_server_code or "",
                line.controller_id.controller_id or "",
                line.reader_id.name or "",
                line.reader_id.serial_number or "",
                line.id,
            )
        )
        # The dialog polls every two seconds. Aggregate in PostgreSQL instead
        # of materializing every raw observation into the ORM cache.
        self.env.cr.execute(
            """
            SELECT UPPER(BTRIM(serial_number)) AS serial_number,
                   port_no,
                   COUNT(*) AS detection_count,
                   MIN(read_at) AS first_detection,
                   MAX(read_at) AS last_detection
              FROM nsp_measurement_event
             WHERE session_id = %s
               AND revision = %s
             GROUP BY UPPER(BTRIM(serial_number)), port_no
            """,
            (session.id, int(session.revision or 1)),
        )
        port_stats = {
            (str(serial or "").strip().upper(), int(port_no or 0)): {
                "count": int(detection_count or 0),
                "first_detection": first_detection,
                "last_detection": last_detection,
            }
            for serial, port_no, detection_count, first_detection, last_detection
            in self.env.cr.fetchall()
            if serial and int(port_no or 0) > 0
        }

        connection_labels = dict(
            self.env["nsp.device"]._fields["connection_type"].selection
        )
        edge_map = {}
        warnings = []
        readers_flat = []
        active_runtime = session.status in ("ready", "running")

        def _dt(value):
            return fields.Datetime.to_string(value) if value else None

        def _seconds_since(value):
            if not value:
                return None
            parsed = fields.Datetime.to_datetime(value)
            return max(0, int((now - parsed).total_seconds())) if parsed else None

        def _warning(severity, code, message, happened_at=None, reader_line_id=False):
            warnings.append({
                "severity": severity,
                "code": code,
                "message": message,
                "happened_at": _dt(happened_at) if happened_at else None,
                "reader_line_id": int(reader_line_id or 0),
            })

        for line in lines:
            edge = line.edge_server_id
            controller = line.controller_id
            reader = line.reader_id
            edge_node = edge_map.setdefault(edge.id, {
                "id": edge.id,
                "code": edge.edge_server_code or "",
                "name": edge.name or edge.edge_server_code or "Edge Server",
                "status": edge.status or "offline",
                "last_heartbeat": _dt(edge.timestamp),
                "controllers": {},
            })
            controller_node = edge_node["controllers"].setdefault(controller.id, {
                "id": controller.id,
                "code": controller.controller_id or "",
                "name": controller.controller_name or controller.controller_id or "Controller",
                "status": controller.status or "offline",
                "last_heartbeat": _dt(controller.timestamp),
                "runtime_mode": "Lane Calibration" if active_runtime else "Inactive",
                "readers": [],
            })

            serial_aliases = {
                str(value or "").strip().upper()
                for value in (reader.serial_number, reader.runtime_detected_serial_number)
                if str(value or "").strip()
            }
            configured_ports = sorted(set(line.reader_port_ids.mapped("port_no")))
            observed_ports = sorted({
                port_no for event_serial, port_no in port_stats
                if event_serial in serial_aliases
            })
            status_last_detection = reader.runtime_last_detection_at
            if (
                status_last_detection
                and session.started_at
                and status_last_detection < session.started_at
            ):
                # Do not reuse a detection from a previous runtime session.
                status_last_detection = False
            status_last_port = int(reader.runtime_last_detection_port_no or 0)
            all_ports = sorted(
                set(configured_ports)
                | set(observed_ports)
                | ({status_last_port} if status_last_port > 0 else set())
            )
            ports = []
            reader_detection_count = 0
            reader_last_detection = None
            silent_port_count = 0
            for port_no in all_ports:
                matching_stats = [
                    port_stats.get((serial_alias, port_no), {})
                    for serial_alias in serial_aliases
                    if port_stats.get((serial_alias, port_no))
                ]
                detection_count = sum(int(item.get("count") or 0) for item in matching_stats)
                first_values = [item.get("first_detection") for item in matching_stats if item.get("first_detection")]
                last_values = [item.get("last_detection") for item in matching_stats if item.get("last_detection")]
                first_detection = min(first_values) if first_values else None
                last_detection = max(last_values) if last_values else None
                if (
                    status_last_detection
                    and status_last_port == port_no
                    and (not last_detection or status_last_detection > last_detection)
                ):
                    # edge/status can arrive before the raw event forwarding retry.
                    last_detection = status_last_detection
                reader_detection_count += detection_count
                if last_detection and (
                    not reader_last_detection or last_detection > reader_last_detection
                ):
                    reader_last_detection = last_detection
                configured = port_no in configured_ports
                last_age = _seconds_since(last_detection)
                if last_detection and (last_age is None or last_age <= silent_after_sec):
                    activity = "active"
                elif configured and active_runtime and reader.status in ("online", "degraded"):
                    activity = "silent"
                    silent_port_count += 1
                elif detection_count:
                    activity = "historical"
                else:
                    activity = "unknown"
                ports.append({
                    "port_no": port_no,
                    "configured": configured,
                    "activity": activity,
                    "detection_count": detection_count,
                    "first_detection": _dt(first_detection),
                    "last_detection": _dt(last_detection),
                })
                if not configured and detection_count:
                    _warning(
                        "warning",
                        "port_outside_scope",
                        _("Reader %(reader)s reported Port %(port)s outside the configured Infrastructure Scope.") % {
                            "reader": reader.display_name,
                            "port": port_no,
                        },
                        last_detection,
                        line.id,
                    )

            if (
                status_last_detection
                and (not reader_last_detection or status_last_detection > reader_last_detection)
            ):
                reader_last_detection = status_last_detection
            last_detection_age = _seconds_since(reader_last_detection)
            if reader.status == "offline" and reader_last_detection and (
                last_detection_age is None or last_detection_age <= silent_after_sec
            ):
                # A fresh data-plane detection is stronger evidence than a delayed
                # status mirror.  edge/status will reconcile the persisted status.
                activity_status = "active"
            elif reader.status == "offline":
                activity_status = "offline"
            elif reader.status == "degraded":
                activity_status = "degraded"
            elif reader.status == "online" and reader_last_detection and (
                last_detection_age is None or last_detection_age <= silent_after_sec
            ):
                activity_status = "active"
            elif reader.status == "online" and active_runtime:
                activity_status = "silent"
            elif reader.status == "online":
                activity_status = "connected"
            else:
                activity_status = "unknown"

            effective_reader_status = reader.status or "offline"
            if activity_status == "active" and effective_reader_status == "offline":
                effective_reader_status = "online"

            reader_node = {
                "reader_line_id": line.id,
                "id": reader.id,
                "name": reader.name or reader.serial_number or "RFID Reader",
                "serial_number": reader.serial_number or "",
                "detected_serial_number": reader.runtime_detected_serial_number or "",
                "status": effective_reader_status,
                "activity_status": activity_status,
                "last_seen": _dt(reader.last_seen),
                "last_detection": _dt(reader_last_detection),
                "detection_count": reader_detection_count,
                "firmware_version": reader.firmware_version or "",
                "connection_type": reader.connection_type or "",
                "connection_label": connection_labels.get(reader.connection_type, "") if reader.connection_type else "",
                "runtime_power_dbm": int(reader.runtime_power_dbm or 0),
                "runtime_read_interval_ms": int(reader.runtime_read_interval_ms or 0),
                "configured_power_dbm": int(line.reader_power_dbm or 0),
                "configured_read_interval_ms": int(line.read_interval_ms or 0),
                "ports": ports,
            }
            controller_node["readers"].append(reader_node)
            readers_flat.append(reader_node)

            if edge.status != "online":
                _warning(
                    "danger" if edge.status in ("error", "revoked", "block") else "warning",
                    "edge_not_online",
                    _("Edge Server %(edge)s is %(status)s.") % {
                        "edge": edge.display_name,
                        "status": edge.status or "offline",
                    },
                    edge.timestamp,
                )
            if controller.status != "online":
                _warning(
                    "danger" if controller.status in ("error", "revoked", "block") else "warning",
                    "controller_not_online",
                    _("Controller %(controller)s is %(status)s.") % {
                        "controller": controller.display_name,
                        "status": controller.status or "offline",
                    },
                    controller.timestamp,
                )
            if effective_reader_status == "offline":
                _warning(
                    "danger",
                    "reader_offline",
                    _("Reader %(reader)s is offline.") % {"reader": reader.display_name},
                    reader.last_seen,
                    line.id,
                )
            elif effective_reader_status == "degraded":
                _warning(
                    "warning",
                    "reader_degraded",
                    _("Reader %(reader)s reported a degraded runtime state.") % {
                        "reader": reader.display_name,
                    },
                    reader.last_seen,
                    line.id,
                )
            if effective_reader_status in ("online", "degraded") and not reader.firmware_version:
                _warning(
                    "info",
                    "firmware_unknown",
                    _("Reader %(reader)s is connected but firmware information is unavailable.") % {
                        "reader": reader.display_name,
                    },
                    reader.last_seen,
                    line.id,
                )
            if active_runtime and effective_reader_status in ("online", "degraded") and not reader_last_detection:
                _warning(
                    "warning",
                    "reader_no_detection",
                    _("Reader %(reader)s is connected but has not produced a detection in this calibration.") % {
                        "reader": reader.display_name,
                    },
                    reader.last_seen,
                    line.id,
                )
            elif active_runtime and effective_reader_status in ("online", "degraded") and (
                last_detection_age is not None and last_detection_age > silent_after_sec
            ):
                _warning(
                    "warning",
                    "reader_silent",
                    _("Reader %(reader)s has not produced a detection in the last %(seconds)s seconds.") % {
                        "reader": reader.display_name,
                        "seconds": silent_after_sec,
                    },
                    reader_last_detection,
                    line.id,
                )
            if silent_port_count:
                for port in ports:
                    if port["activity"] == "silent":
                        _warning(
                            "warning",
                            "port_silent",
                            _("Reader %(reader)s Port %(port)s has not produced a recent detection.") % {
                                "reader": reader.display_name,
                                "port": port["port_no"],
                            },
                            fields.Datetime.to_datetime(port["last_detection"]) if port["last_detection"] else reader.last_seen,
                            line.id,
                        )

        edges = []
        for edge in sorted(edge_map.values(), key=lambda item: (item["code"], item["id"])):
            controllers = list(edge.pop("controllers").values())
            controllers.sort(key=lambda item: (item["code"], item["id"]))
            for controller in controllers:
                controller["readers"].sort(
                    key=lambda item: (item["name"], item["serial_number"], item["reader_line_id"])
                )
            edge["controllers"] = controllers
            edges.append(edge)

        # The same Edge/Controller warning can occur once per Reader line.
        unique_warnings = []
        seen_warning_keys = set()
        for warning in warnings:
            key = (
                warning["severity"], warning["code"], warning["message"],
                warning.get("reader_line_id") or 0,
            )
            if key in seen_warning_keys:
                continue
            seen_warning_keys.add(key)
            unique_warnings.append(warning)

        edge_nodes = [edge for edge in edges]
        controller_nodes = [
            controller for edge in edges for controller in edge["controllers"]
        ]
        reader_total = len(readers_flat)
        active_count = sum(1 for item in readers_flat if item["activity_status"] == "active")
        silent_count = sum(1 for item in readers_flat if item["activity_status"] == "silent")
        offline_count = sum(1 for item in readers_flat if item["activity_status"] == "offline")
        degraded_count = sum(1 for item in readers_flat if item["activity_status"] == "degraded")
        connected_count = sum(
            1 for item in readers_flat
            if item["status"] in ("online", "degraded")
        )

        return {
            "found": True,
            "session_id": session.id,
            "measurement_code": session.measurement_code or "",
            "revision": int(session.revision or 1),
            "status": session.status,
            "editable": session.status == "draft",
            "runtime_active": active_runtime,
            "silent_after_sec": silent_after_sec,
            "server_time": fields.Datetime.to_string(now),
            "summary": {
                "edge_total": len(edge_nodes),
                "edge_online": sum(1 for item in edge_nodes if item["status"] == "online"),
                "controller_total": len(controller_nodes),
                "controller_online": sum(1 for item in controller_nodes if item["status"] == "online"),
                "reader_total": reader_total,
                "reader_connected": connected_count,
                "reader_active": active_count,
                "reader_silent": silent_count,
                "reader_offline": offline_count,
                "reader_degraded": degraded_count,
            },
            "edges": edges,
            "readers": readers_flat,
            "warnings": unique_warnings,
        }

    def _apply_runtime_status(self, status, occurred_at=False, message=False, revision=False):
        self.ensure_one()
        current_revision = max(int(self.revision or 1), 1)
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

        target = str(status or "").strip().lower()
        result = CalibrationStatusPolicy.classify_runtime_status(
            self.status,
            target,
            incoming_revision,
            current_revision,
        )
        if result["outcome"] != "duplicate":
            if result["outcome"].startswith("ignored_"):
                return result

        current = str(self.status or "draft")
        when = occurred_at or fields.Datetime.now()
        values = {}
        if target != current:
            values["status"] = target
            result["outcome"] = "applied"
            result["current_status"] = target
        if target == "running" and not self.started_at:
            values["started_at"] = when
        if target in ("completed", "failed", "cancelled") and not self.ended_at:
            values["ended_at"] = when
        if values:
            if target != current:
                extra_values = dict(values)
                extra_values.pop("status", None)
                self._apply_status_transition(target, extra_values)
            else:
                self.with_context(measurement_sync=True).write(values)
        if message and result["outcome"] == "applied":
            self.message_post(body=str(message))
        return result
