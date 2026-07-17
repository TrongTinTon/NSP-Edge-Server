# -*- coding: utf-8 -*-
import json

from odoo import fields, http
from odoo.http import request


class NspParkingMonitorNotificationController(http.Controller):
    """Parking monitor notification endpoints.

    Architecture:
    - NSP Gatekeeper owns the business event and source data in nsp.parking.transaction.
    - NSP Notification creates/delivers monitor notifications that reference the transaction.
    - The realtime parking screen consumes this notification layer, not a separate monitor domain.
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
        "/api/nsp_notification/v1/parking-monitor/events",
        type="http",
        auth="user",
        methods=["GET"],
        csrf=False,
    )
    def parking_monitor_events(self, **kw):
        gate_id = self._int_param(kw.get("gate_id"), default=0, minimum=0)
        branch_id = self._int_param(kw.get("branch_id"), default=0, minimum=0)
        lane_id = self._int_param(kw.get("lane_id"), default=0, minimum=0)
        lane_code = (kw.get("lane_code") or "").strip()
        limit = self._int_param(kw.get("limit"), default=30, minimum=1, maximum=100)
        since_id = self._int_param(kw.get("since_id"), default=0, minimum=0)
        direction = (kw.get("direction") or "").strip().lower()
        status_filter = (kw.get("status") or "").strip().lower()

        domain = [
            ("active", "=", True),
            ("monitor_channel", "=", "parking_monitor"),
            ("parking_transaction_id", "!=", False),
        ]
        if since_id:
            domain.append(("id", ">", since_id))
        if gate_id:
            domain.append(("gate_id", "=", gate_id))
        if branch_id:
            domain.append(("branch_id", "=", branch_id))
        if lane_id:
            domain.append(("parking_transaction_id.lane_id", "=", lane_id))
        if lane_code:
            domain.append(("parking_transaction_id.lane_code", "=ilike", lane_code))
        if direction in ("entry", "exit"):
            domain.append(("parking_transaction_id.direction", "=", direction))
        if status_filter in ("allowed", "denied"):
            domain.append(("parking_transaction_id.status", "=", status_filter))

        Notification = request.env["nsp.notification"].sudo()
        records = Notification.search(domain, order="event_time desc, id desc", limit=limit)
        records = records.sorted(key=lambda rec: (rec.event_time or fields.Datetime.now(), rec.id))
        events = [rec._parking_monitor_payload() for rec in records]

        return self._json_response({
            "ok": True,
            "events": events,
            "count": len(events),
            "last_event_id": max([event["id"] for event in events], default=since_id),
            "source_model": "nsp.notification",
            "business_source_model": "nsp.parking.transaction",
            "server_time": fields.Datetime.to_string(fields.Datetime.now()),
        })
