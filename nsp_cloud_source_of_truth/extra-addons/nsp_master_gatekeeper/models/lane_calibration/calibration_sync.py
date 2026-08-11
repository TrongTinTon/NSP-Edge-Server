# -*- coding: utf-8 -*-
from odoo import api, fields, models, _

from .calibration_status import CalibrationStatusPolicy


class NspMeasurementSessionSync(models.Model):
    _inherit = "nsp.measurement.session"

    def _node_device_id(self, node):
        self.ensure_one()
        if node.device_type == "server":
            return node.server_id.edge_server_code or ""
        if node.device_type == "controller":
            return node.controller_id.controller_id or ""
        if node.device_type == "reader":
            return node.reader_id.device_code or node.reader_id.serial_number or ""
        return ""

    def _sync_nodes(self, edge_server=False):
        """Return all nodes or one released Server branch for Edge-specific sync."""
        self.ensure_one()
        nodes = self.device_node_ids
        if not edge_server:
            return nodes
        roots = nodes.filtered(
            lambda node: node.device_type == "server" and node.server_id == edge_server
        )
        if not roots:
            return nodes.browse()
        selected = roots
        frontier = roots
        while frontier:
            children = nodes.filtered(lambda node: node.parent_id in frontier)
            if not children:
                break
            selected |= children
            frontier = children
        return selected

    def _calibration_sync_payload(self, edge_server=False):
        """Serialize a flat released Device Tree snapshot for Edge.

        Master identity and contextual topology are intentionally separate: each
        topology row carries ``node_id``, ``device_id`` and ``parent_node_id``.
        """
        self.ensure_one()
        nodes = self._sync_nodes(edge_server=edge_server).sorted(
            key=lambda node: (node.sequence, node.id)
        )
        node_ids = set(nodes.ids)

        servers = []
        controllers = []
        readers = []
        topology = []
        for node in nodes:
            parent_node_id = node.parent_id.id if node.parent_id.id in node_ids else None
            device_id = self._node_device_id(node)
            row = {
                "node_id": node.id,
                "device_type": node.device_type,
                "device_id": device_id,
                "parent_node_id": parent_node_id,
                "sequence": int(node.sequence or 10),
            }
            if node.device_type == "server":
                server = node.server_id
                servers.append({
                    "id": device_id,
                    "name": server.name or "",
                    "status": server.status or "",
                })
            elif node.device_type == "controller":
                controller = node.controller_id
                controllers.append({
                    "id": device_id,
                    "name": controller.controller_name or "",
                    "status": controller.status or "",
                })
            elif node.device_type == "reader":
                reader = node.reader_id
                reader_data = {
                    "id": device_id,
                    "name": reader.name or reader.serial_number or "",
                    "serial_number": reader.serial_number or "",
                    "status": reader.status or "",
                }
                readers.append(reader_data)
                row["configuration"] = {
                    "power_dbm": int(node.power_dbm or 0),
                    "read_interval_ms": int(node.read_interval_ms or 200),
                    "tid_addr": int(node.tid_addr or 0),
                    "tid_len": int(node.tid_len or 0),
                }
                row["ports"] = [
                    {
                        "port_no": int(port.port_no or 0),
                        "sequence": int(port.sequence or 10),
                    }
                    for port in node.reader_port_ids.sorted(
                        key=lambda port: (port.sequence, port.port_no, port.id)
                    )
                ]
            topology.append(row)

        calibration_tag = (
            {"tid": self.target_line_ids[:1].tid or ""}
            if self.target_line_ids else False
        )
        return {
            "schema_version": 4,
            "snapshot_id": "%s-R%s" % (self.measurement_code, int(self.revision or 1)),
            "lane_calibration_code": self.measurement_code,
            "status": self.status,
            "desired_state": "running" if self.status in ("ready", "running") else "stopped",
            "revision": int(self.revision or 1),
            "calibration_tag": calibration_tag,
            "devices": {
                "servers": servers,
                "controllers": controllers,
                "readers": readers,
            },
            "topology": {"nodes": topology},
        }

    def _target_coverage(self):
        self.ensure_one()
        targets = self.target_line_ids.sorted(key=lambda line: (line.tid or "", line.id))
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
        for target in targets:
            data = stats.get(target.tid, {})
            count = int(data.get("read_count") or 0)
            result.append({
                "id": target.id,
                "tid": target.tid or "",
                "detected": bool(count),
                "read_count": count,
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
        tags = session._target_coverage()
        detected_tag_count = sum(1 for tag in tags if tag["detected"])

        controllers = []
        for node in session._controller_nodes().sorted(
            key=lambda item: (item.controller_id.controller_id or "", item.id)
        ):
            server_node = node.parent_id if node.parent_id.device_type == "server" else False
            controllers.append({
                "node_id": node.id,
                "id": node.controller_id.id,
                "code": node.controller_id.controller_id or "",
                "name": node.controller_id.controller_name or "",
                "server_node_id": server_node.id if server_node else False,
                "edge_server_code": server_node.server_id.edge_server_code if server_node else "",
                "edge_status": server_node.server_id.status if server_node else "",
            })

        readers = []
        for node in session._reader_nodes().sorted(
            key=lambda item: (item.reader_id.name or "", item.reader_id.serial_number or "", item.id)
        ):
            reader = node.reader_id
            controller_node = node.parent_id if node.parent_id.device_type == "controller" else False
            controller = controller_node.controller_id if controller_node else self.env["nsp.controller"]
            readers.append({
                "reader_node_id": node.id,
                "id": reader.id,
                "name": reader.name or reader.serial_number or "",
                "serial_number": reader.serial_number or "",
                "controller_node_id": controller_node.id if controller_node else False,
                "controller_code": (controller.controller_id or "") if controller else "",
                "controller_name": (controller.controller_name or "") if controller else "",
                "status": reader.status or "",
                "runtime_power_dbm": int(reader.runtime_power_dbm or 0),
                "runtime_read_interval_ms": int(reader.runtime_read_interval_ms or 0),
                "power_dbm": int(node.power_dbm or 0),
                "read_interval_ms": int(node.read_interval_ms or 0),
                "firmware_version": reader.firmware_version or "",
                "ports": sorted(node.reader_port_ids.mapped("port_no")),
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
            "tags": tags,
            "tag_count": len(tags),
            "detected_tag_count": detected_tag_count,
            "coverage_percent": round((detected_tag_count * 100.0 / len(tags)), 1) if tags else 0.0,
            "readers": readers,
            "reader_count": len(readers),
            "reader_online_count": sum(1 for row in readers if row.get("status") in ("online", "degraded")),
            "reader_offline_count": sum(1 for row in readers if row.get("status") == "offline"),
            "configured_reader_port_count": len({
                (row["serial_number"], int(port_no or 0))
                for row in readers
                for port_no in row.get("ports", [])
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
        """Return Tree topology plus runtime health; Draft can include unassigned nodes."""
        try:
            record_id = int(session_id or 0)
        except (TypeError, ValueError):
            record_id = 0
        session = self.browse(record_id).exists()
        if not session:
            return {"found": False}
        session.check_access("read")

        def _reader_payload(node):
            reader = node.reader_id
            return {
                "reader_node_id": node.id,
                "id": reader.id,
                "name": reader.name or reader.serial_number or "RFID Reader",
                "serial_number": reader.serial_number or "",
                "status": reader.status or "offline",
                "last_seen": fields.Datetime.to_string(reader.last_seen) if reader.last_seen else None,
                "firmware_version": reader.firmware_version or "",
                "configured_power_dbm": int(node.power_dbm or 0),
                "configured_read_interval_ms": int(node.read_interval_ms or 0),
                "ports": [
                    {"port_no": int(port.port_no or 0)}
                    for port in node.reader_port_ids.sorted(key=lambda row: (row.port_no, row.id))
                ],
            }

        controller_nodes = session._controller_nodes()
        reader_nodes = session._reader_nodes()
        edges = []
        for server_node in session._server_nodes().sorted(
            key=lambda node: (node.server_id.edge_server_code or "", node.id)
        ):
            server = server_node.server_id
            controllers = []
            for controller_node in controller_nodes.filtered(
                lambda node: node.parent_id == server_node
            ).sorted(key=lambda node: (node.controller_id.controller_id or "", node.id)):
                controller = controller_node.controller_id
                controllers.append({
                    "node_id": controller_node.id,
                    "id": controller.id,
                    "code": controller.controller_id or "",
                    "name": controller.controller_name or "",
                    "status": controller.status or "offline",
                    "readers": [
                        _reader_payload(reader_node)
                        for reader_node in reader_nodes.filtered(
                            lambda node: node.parent_id == controller_node
                        ).sorted(key=lambda node: (node.reader_id.name or "", node.id))
                    ],
                })
            edges.append({
                "node_id": server_node.id,
                "id": server.id,
                "code": server.edge_server_code or "",
                "name": server.name or "",
                "status": server.status or "offline",
                "controllers": controllers,
            })

        unassigned_controllers = controller_nodes.filtered(
            lambda node: not node.parent_id or node.parent_id.device_type != "server"
        )
        unassigned_readers = reader_nodes.filtered(
            lambda node: not node.parent_id or node.parent_id.device_type != "controller"
        )
        warnings = []
        for node in unassigned_controllers:
            warnings.append({
                "severity": "warning",
                "code": "controller_unassigned",
                "message": _("Controller %s is not assigned to a Server.") % node.device_name,
                "node_id": node.id,
            })
        for node in unassigned_readers:
            warnings.append({
                "severity": "warning",
                "code": "reader_unassigned",
                "message": _("Reader %s is not assigned to a Controller.") % node.device_name,
                "node_id": node.id,
            })

        return {
            "found": True,
            "session_id": session.id,
            "measurement_code": session.measurement_code or "",
            "revision": int(session.revision or 1),
            "status": session.status,
            "editable": session.status == "draft",
            "server_time": fields.Datetime.to_string(fields.Datetime.now()),
            "summary": {
                "edge_total": len(session._server_nodes()),
                "edge_online": len(session._server_nodes().filtered(lambda node: node.server_id.status == "online")),
                "controller_total": len(controller_nodes),
                "controller_online": len(controller_nodes.filtered(lambda node: node.controller_id.status == "online")),
                "reader_total": len(reader_nodes),
                "reader_connected": len(reader_nodes.filtered(lambda node: node.reader_id.status in ("online", "degraded"))),
                "reader_offline": len(reader_nodes.filtered(lambda node: node.reader_id.status == "offline")),
                "unassigned_controller_total": len(unassigned_controllers),
                "unassigned_reader_total": len(unassigned_readers),
            },
            "edges": edges,
            "unassigned_controllers": [
                {
                    "node_id": node.id,
                    "id": node.controller_id.id,
                    "code": node.controller_id.controller_id or "",
                    "name": node.device_name,
                    "status": node.controller_id.status or "offline",
                }
                for node in unassigned_controllers
            ],
            "unassigned_readers": [_reader_payload(node) for node in unassigned_readers],
            "readers": [_reader_payload(node) for node in reader_nodes],
            "warnings": warnings,
        }

    def _apply_runtime_status(self, status, occurred_at=False, message=False, revision=False):
        self.ensure_one()
        current_revision = max(int(self.revision or 1), 1)
        if revision in (False, None, ""):
            raise ValueError("invalid_lane_calibration_revision")
        try:
            incoming_revision = int(revision)
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
