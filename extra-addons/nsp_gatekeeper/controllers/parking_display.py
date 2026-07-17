# -*- coding: utf-8 -*-
import json

from odoo import fields, http
from odoo.http import request


class NspParkingDisplayController(http.Controller):
    """Parking display support endpoints owned by NSP Gatekeeper.

    Gatekeeper owns gate configuration and the parking transaction source of
    truth. The realtime screen consumes parking monitor notifications from
    nsp_notification. The events endpoint below is retained for backward
    diagnostic comparison against nsp.parking.transaction.
    """

    def _json_response(self, payload, status=200):
        body = json.dumps(payload or {}, ensure_ascii=False, default=str)
        return request.make_response(
            body,
            headers=[("Content-Type", "application/json; charset=utf-8")],
            status=status,
        )

    def _int_param(self, value, default=0, minimum=None, maximum=None):
        try:
            number = int(value)
        except Exception:
            number = int(default or 0)
        if minimum is not None:
            number = max(number, minimum)
        if maximum is not None:
            number = min(number, maximum)
        return number

    @http.route(
        "/api/nsp_gatekeeper/v1/parking-display/gates",
        type="http",
        auth="user",
        methods=["GET"],
        csrf=False,
    )
    def parking_display_gates(self, **kw):
        Gate = request.env["nsp.gate"].sudo()
        gates = Gate.search(
            [
                ("gate_status", "=", "active"),
                ("operation_state", "=", "operational"),
            ],
            order="branch_id, name, code, id",
        )
        return self._json_response({
            "ok": True,
            "gates": [
                {
                    "id": gate.id,
                    "code": gate.code,
                    "name": gate.name,
                    "display_name": "%s%s" % (
                        (gate.branch_id.name + " / ") if gate.branch_id else "",
                        gate.name or gate.code or gate.id,
                    ),
                    "branch_id": gate.branch_id.id if gate.branch_id else False,
                    "branch_name": gate.branch_id.name if gate.branch_id else "",
                    "controllers": [{
                        "id": controller.id,
                        "controller_id": controller.controller_id,
                        "controller_name": controller.controller_name or "",
                    } for controller in gate.controller_ids],
                    "lanes": [{
                        "id": lane.id,
                        "code": lane.code,
                        "name": lane.name,
                        "direction": lane.direction,
                    } for lane in gate.lane_ids.filtered(lambda l: l.active)],
                }
                for gate in gates
            ],
            "server_time": fields.Datetime.to_string(fields.Datetime.now()),
        })

    @http.route(
        "/api/nsp_gatekeeper/v1/parking-display/events",
        type="http",
        auth="user",
        methods=["GET"],
        csrf=False,
    )
    def parking_display_events(self, **kw):
        gate_id = self._int_param(kw.get("gate_id"), default=0, minimum=0)
        limit = self._int_param(kw.get("limit"), default=30, minimum=1, maximum=100)
        since_id = self._int_param(kw.get("since_id"), default=0, minimum=0)
        direction = (kw.get("direction") or "").strip().lower()
        status_filter = (kw.get("status") or "").strip().lower()

        domain = []
        if gate_id:
            domain.append(("gate_id", "=", gate_id))
        if since_id:
            domain.append(("id", ">", since_id))
        if direction in ("entry", "exit"):
            domain.append(("direction", "=", direction))
        if status_filter in ("allowed", "denied"):
            domain.append(("status", "=", status_filter))

        Tx = request.env["nsp.parking.transaction"].sudo()
        records = Tx.search(domain, order="time_entered desc, id desc", limit=limit)
        records = records.sorted(key=lambda rec: (rec.time_entered or fields.Datetime.now(), rec.id))

        events = []
        for rec in records:
            gate = rec.gate_id
            branch = gate.branch_id if gate else request.env["nsp.branch"].browse()
            controller = rec.controller_id
            direction_label = "Vào" if rec.direction == "entry" else "Ra"
            status_label = "Được phép" if rec.status == "allowed" else "Từ chối"
            events.append({
                "id": rec.id,
                "event_id": rec.id,
                "event_time": fields.Datetime.to_string(rec.time_entered) if rec.time_entered else "",
                "direction": rec.direction,
                "direction_label": direction_label,
                "status": rec.status,
                "status_label": status_label,
                "vehicle": rec.vehicle_display or rec.license_plate or rec.vehicle_tid or "-",
                "license_plate": rec.license_plate or "",
                "vehicle_tid": rec.vehicle_tid or "",
                "gate": rec.gate_display or rec.gate_code or (gate.name if gate else ""),
                "gate_id": gate.id if gate else False,
                "gate_code": rec.gate_code or (gate.code if gate else ""),
                "gate_name": gate.name if gate else (rec.gate_display or ""),
                "lane_id": rec.lane_id.id if rec.lane_id else False,
                "lane_code": rec.lane_code or (rec.lane_id.code if rec.lane_id else ""),
                "lane_name": rec.lane_id.name if rec.lane_id else (rec.lane_display or ""),
                "lane": rec.lane_display or rec.lane_code or "",
                "branch_id": branch.id if branch else False,
                "branch_name": branch.name if branch else "",
                "controller_id": controller.id if controller else False,
                "controller_code": controller.controller_id if controller else "",
                "message": rec.error_message or "",
            })

        return self._json_response({
            "ok": True,
            "events": events,
            "count": len(events),
            "last_event_id": max([event["id"] for event in events], default=since_id),
            "source_model": "nsp.parking.transaction",
            "server_time": fields.Datetime.to_string(fields.Datetime.now()),
        })
