# -*- coding: utf-8 -*-
import json

from odoo import fields, http
from odoo.http import request

class NspParkingDisplayController(http.Controller):
    """Parking display support endpoints owned by NSP Gatekeeper.

    The server owns parking operation topology and the parking transaction
    source of truth. The screen reads directly from nsp.parking.transaction.
    """

    def _has_parking_access(self):
        return request.env.user.has_group("nsp_core.group_nsp_operator")

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
        "/api/nsp_gatekeeper/v1/parking-display/areas",
        type="http",
        auth="user",
        methods=["GET"],
        csrf=False,
    )
    def parking_display_areas(self, **kw):
        if not self._has_parking_access():
            return self._json_response({"ok": False, "error": "forbidden"}, status=403)
        ParkingArea = request.env["nsp.parking.area"].sudo()
        parking_areas = ParkingArea.search(
            [
                ("status", "=", "active"),
                ("operation_state", "=", "operational"),
            ],
            order="branch_id, name, code, id",
        )
        return self._json_response({
            "ok": True,
            "parking_areas": [
                {
                    "id": parking_area.id,
                    "code": parking_area.code,
                    "name": parking_area.name,
                    "display_name": "%s%s" % (
                        (parking_area.branch_id.name + " / ") if parking_area.branch_id else "",
                        parking_area.name or parking_area.code or parking_area.id,
                    ),
                    "branch_id": parking_area.branch_id.id if parking_area.branch_id else False,
                    "branch_name": parking_area.branch_id.name if parking_area.branch_id else "",
                    "controllers": [{
                        "id": controller.id,
                        "controller_id": controller.controller_id,
                        "controller_name": controller.controller_name or "",
                    } for controller in parking_area.controller_ids],
                    "lanes": [{
                        "id": lane.id,
                        "code": lane.code,
                        "name": lane.name,
                        "direction": lane.direction,
                    } for lane in parking_area.lane_ids.filtered(lambda l: l.active)],
                }
                for parking_area in parking_areas
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
        if not self._has_parking_access():
            return self._json_response({"ok": False, "error": "forbidden"}, status=403)
        parking_area_id = self._int_param(kw.get("parking_area_id"), default=0, minimum=0)
        limit = self._int_param(kw.get("limit"), default=30, minimum=1, maximum=100)
        since_id = self._int_param(kw.get("since_id"), default=0, minimum=0)
        direction = (kw.get("direction") or "").strip().lower()
        status_filter = (kw.get("status") or "").strip().lower()

        domain = []
        if parking_area_id:
            domain.append(("lane_id.parking_area_id", "=", parking_area_id))
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
            parking_area = rec.parking_area_id
            branch = parking_area.branch_id if parking_area else request.env["nsp.branch"].browse()
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
                "parking_area": rec.parking_area_display or (parking_area.name if parking_area else ""),
                "parking_area_id": parking_area.id if parking_area else False,
                "parking_area_code": parking_area.code if parking_area else "",
                "parking_area_name": parking_area.name if parking_area else (rec.parking_area_display or ""),
                "lane_id": rec.lane_id.id if rec.lane_id else False,
                "lane_code": rec.lane_id.code if rec.lane_id else "",
                "lane_name": rec.lane_id.name if rec.lane_id else "",
                "lane": rec.lane_display or "",
                "branch_id": branch.id if branch else False,
                "branch_name": branch.name if branch else "",
                "controller_id": controller.id if controller else False,
                "controller_code": controller.controller_id if controller else "",
                "message": rec.error_message or "",
                "reader_serial_number": rec.serial_number or "",
                "antenna_no": int(rec.antenna_no or 0),
            })

        return self._json_response({
            "ok": True,
            "events": events,
            "count": len(events),
            "last_event_id": max([event["id"] for event in events], default=since_id),
            "source_model": "nsp.parking.transaction",
            "server_time": fields.Datetime.to_string(fields.Datetime.now()),
        })
