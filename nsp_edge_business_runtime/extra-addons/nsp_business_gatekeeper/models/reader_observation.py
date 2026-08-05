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

    def port_numbers(self):
        self.ensure_one()
        try:
            values = json.loads(self.ports_json or "[]")
        except Exception:
            values = []
        return sorted({int(value) for value in values if not isinstance(value, bool) and int(value) > 0})
