# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class NspMeasurementSessionDetection(models.Model):
    _inherit = "nsp.measurement.session"

    @api.model
    def cron_cleanup_expired_measurements(self):
        role = self._deployment_role()
        param = (
            "nsp_master_gatekeeper.measurement_retention_days"
            if role == "cloud"
            else "nsp_business_gatekeeper.measurement_retention_days"
        )
        value = self.env["ir.config_parameter"].sudo().get_param(param, "7")
        try:
            retention_days = max(int(value), 1)
        except (TypeError, ValueError):
            retention_days = 7
        cutoff = fields.Datetime.now() - timedelta(days=retention_days)
        events = self.env["nsp.measurement.event"].sudo().search(
            [
                ("session_id.status", "in", ["completed", "applied", "failed", "cancelled"]),
                ("read_at", "<", cutoff),
            ],
            limit=5000,
        )
        count = len(events)
        if events and "nsp.sync.record" in self.env.registry.models:
            self.env["nsp.sync.record"].sudo().search([
                ("record_model", "=", "nsp.measurement.event"),
                ("record_key", "in", events.mapped("event_uid")),
            ]).unlink()
        events.unlink()
        if role != "cloud":
            stale_sessions = self.sudo().search([
                ("status", "in", ["completed", "applied", "failed", "cancelled"]),
                ("ended_at", "!=", False),
                ("ended_at", "<", cutoff),
            ])
            empty_sessions = stale_sessions.filtered(lambda session: not session.event_ids)
            if empty_sessions:
                empty_sessions.with_context(measurement_sync=True).unlink()
        return count


class NspMeasurementEvent(models.Model):
    _name = "nsp.measurement.event"
    _description = "NSP Measurement Observation"
    _rec_name = "event_uid"
    _order = "read_at desc, id desc"

    event_uid = fields.Char(required=True, copy=False, index=True)
    session_id = fields.Many2one(
        "nsp.measurement.session",
        required=True,
        ondelete="cascade",
        index=True,
    )
    revision = fields.Integer(required=True, default=1, index=True)
    serial_number = fields.Char(required=True, index=True)
    port_no = fields.Integer(required=True, index=True)
    tid = fields.Char(required=True, index=True)
    read_at = fields.Datetime(required=True, index=True)
    read_at_ms = fields.Integer(string="Millisecond", required=True, default=0)
    rssi_dbm = fields.Float()
    power_dbm = fields.Integer(string="Reader Power (dBm)")
    read_interval_ms = fields.Integer(string="Read Interval ms", required=True, default=200)

    _sql_constraints = [
        ("measurement_event_uid_unique", "unique(event_uid)", "Measurement Event UID must be unique."),
        ("measurement_event_port_positive", "CHECK(port_no > 0)", "Reader Port must be greater than zero."),
        ("measurement_event_revision_positive", "CHECK(revision > 0)", "Measurement Revision must be greater than zero."),
        ("measurement_event_ms_range", "CHECK(read_at_ms >= 0 AND read_at_ms <= 999)", "Measurement millisecond must be between 0 and 999."),
        ("measurement_event_read_interval_range", "CHECK(read_interval_ms > 0 AND read_interval_ms <= 60000)", "Read Interval must be between 1 and 60000 ms."),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for source in vals_list:
            vals = dict(source)
            vals["event_uid"] = str(vals.get("event_uid") or "").strip()
            vals["serial_number"] = str(vals.get("serial_number") or "").strip().upper()
            vals["tid"] = self.env["nsp.rfid.tag"]._normalize_tid(vals.get("tid"))
            vals["revision"] = max(int(vals.get("revision") or 1), 1)
            vals["read_at_ms"] = max(0, min(int(vals.get("read_at_ms") or 0), 999))
            vals["read_interval_ms"] = max(1, min(int(vals.get("read_interval_ms") or 200), 60000))
            prepared.append(vals)
        return super().create(prepared)

    def matches_measurement_values(self, values):
        self.ensure_one()
        return (
            self.session_id.id == values["session_id"]
            and int(self.revision or 1) == int(values["revision"] or 1)
            and self.serial_number == values["serial_number"]
            and int(self.port_no or 0) == int(values["port_no"] or 0)
            and self.tid == values["tid"]
            and fields.Datetime.to_string(self.read_at)
            == fields.Datetime.to_string(values["read_at"])
            and int(self.read_at_ms or 0) == int(values["read_at_ms"] or 0)
            and (False if self.rssi_dbm in (False, None) else float(self.rssi_dbm))
            == (
                False
                if values["rssi_dbm"] in (False, None)
                else float(values["rssi_dbm"])
            )
            and int(self.power_dbm or 0) == int(values["power_dbm"] or 0)
            and int(self.read_interval_ms or 0)
            == int(values["read_interval_ms"] or 0)
        )

    def init(self):
        self.env.cr.execute(
            """
            CREATE INDEX IF NOT EXISTS nsp_measurement_event_session_revision_read_idx
                ON nsp_measurement_event (session_id, revision, read_at, read_at_ms, id)
            """
        )
        self.env.cr.execute(
            """
            CREATE INDEX IF NOT EXISTS nsp_measurement_event_session_revision_tid_idx
                ON nsp_measurement_event (session_id, revision, tid, read_at, id)
            """
        )

    @api.constrains("session_id", "serial_number", "port_no", "tid")
    def _check_event_scope(self):
        for event in self:
            session = event.session_id
            key = (event.serial_number, int(event.port_no or 0))
            if key not in session._allowed_reader_port_pairs():
                raise ValidationError(_("Measurement observation Reader Port is not part of the Lane Calibration."))
            if event.tid not in session._allowed_target_tids():
                raise ValidationError(_("Only selected Vehicle RFID Tags may be stored in this Lane Calibration."))
