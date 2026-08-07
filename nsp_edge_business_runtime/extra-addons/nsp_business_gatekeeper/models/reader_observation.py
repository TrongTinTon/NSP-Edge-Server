# -*- coding: utf-8 -*-
import json

from odoo import api, fields, models


class NspReaderObservation(models.Model):
    _name = "nsp.reader.observation"
    _description = "Physical Reader Observation"
    _order = "controller_id, serial_number, id"

    controller_id = fields.Many2one(
        "nsp.controller", required=True, ondelete="cascade", index=True,
    )
    serial_number = fields.Char(required=True, index=True, copy=False)
    endpoint = fields.Char(index=True)
    status = fields.Selection([
        ("online", "Online"),
        ("offline", "Offline"),
        ("degraded", "Degraded"),
    ], required=True, default="offline", index=True)
    last_seen_at = fields.Datetime(index=True)
    last_reported_at = fields.Datetime(required=True, default=fields.Datetime.now, index=True)
    last_detection_at = fields.Datetime(index=True)
    last_detection_port_no = fields.Integer()
    firmware_version = fields.Char()
    power_dbm = fields.Integer()
    read_interval_ms = fields.Integer()
    ports_json = fields.Text(default="[]")

    _sql_constraints = [
        (
            "controller_serial_unique",
            "unique(controller_id, serial_number)",
            "A physical Reader observation must be unique per Controller and SDK SerialNumber.",
        ),
    ]

    @api.model
    def _normalize_serial(self, value):
        return str(value or "").strip().upper()

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for source in vals_list:
            values = dict(source)
            values["serial_number"] = self._normalize_serial(values.get("serial_number"))
            values["endpoint"] = str(values.get("endpoint") or "").strip().upper() or False
            values["last_reported_at"] = values.get("last_reported_at") or fields.Datetime.now()
            prepared.append(values)
        return super().create(prepared)

    def write(self, vals):
        values = dict(vals)
        if "serial_number" in values:
            values["serial_number"] = self._normalize_serial(values.get("serial_number"))
        if "endpoint" in values:
            values["endpoint"] = str(values.get("endpoint") or "").strip().upper() or False
        return super().write(values)


    @api.model
    def touch_detection(
        self, controller, serial_number, detected_at=None, port_no=0,
        power_dbm=None, read_interval_ms=None, freshness_sec=300,
    ):
        """Record data-plane evidence without making a business decision.

        A raw RFID detection proves that the physical Reader produced data.  This
        cache is therefore updated even when the event is later ignored by Lane
        Calibration or Parking business validation.  Historical outbox replays
        update ``last_detection_at`` but do not incorrectly mark a Reader online.
        """
        if not controller:
            return self.browse()
        serial = self._normalize_serial(serial_number)
        if not serial:
            return self.browse()
        try:
            port = int(port_no or 0)
        except (TypeError, ValueError):
            port = 0
        if port < 1 or port > 16:
            port = 0

        now = fields.Datetime.now()
        detected = fields.Datetime.to_datetime(detected_at) if detected_at else now
        if not detected:
            detected = now
        # Protect status freshness from a clock that is far in the future.
        if detected > now:
            detected = now
        try:
            freshness = min(max(int(freshness_sec or 300), 30), 3600)
        except (TypeError, ValueError):
            freshness = 300
        is_fresh = (now - detected).total_seconds() <= freshness

        observation = self.sudo().search([
            ("controller_id", "=", controller.id),
            ("serial_number", "=", serial),
        ], limit=1)
        values = {}
        if not observation or not observation.last_detection_at or detected >= observation.last_detection_at:
            values["last_detection_at"] = detected
            if port:
                values["last_detection_port_no"] = port
        if not observation or not observation.last_seen_at or detected >= observation.last_seen_at:
            values["last_seen_at"] = detected

        existing_ports = set(observation.port_numbers()) if observation else set()
        if port:
            existing_ports.add(port)
        values["ports_json"] = json.dumps(sorted(existing_ports), separators=(",", ":"))

        if power_dbm not in (None, ""):
            try:
                power = int(power_dbm)
                if 0 <= power <= 40:
                    values["power_dbm"] = power
            except (TypeError, ValueError):
                pass
        if read_interval_ms not in (None, ""):
            try:
                interval = int(read_interval_ms)
                if 0 < interval <= 60000:
                    values["read_interval_ms"] = interval
            except (TypeError, ValueError):
                pass

        if is_fresh:
            values.update({
                "status": "online",
                "last_reported_at": now,
            })
        elif not observation:
            values.update({
                "status": "offline",
                "last_reported_at": detected,
            })

        if observation:
            observation.write(values)
            return observation
        values.update({
            "controller_id": controller.id,
            "serial_number": serial,
        })
        return self.sudo().create(values)

    def port_numbers(self):
        self.ensure_one()
        try:
            values = json.loads(self.ports_json or "[]")
        except (TypeError, json.JSONDecodeError):
            values = []
        return sorted({int(value) for value in values if not isinstance(value, bool) and int(value) > 0})
