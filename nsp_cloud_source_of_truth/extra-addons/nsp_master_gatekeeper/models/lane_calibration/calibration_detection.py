# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

from .calibration_session import _normalize_raw_tid_value


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
                ("session_id.status", "in", ["completed", "failed", "cancelled"]),
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
                ("status", "in", ["completed", "failed", "cancelled"]),
                ("ended_at", "!=", False),
                ("ended_at", "<", cutoff),
            ])
            empty_sessions = stale_sessions.filtered(lambda session: not session.event_ids)
            if empty_sessions:
                empty_sessions.with_context(measurement_sync=True).unlink()
        return count


class NspMeasurementEvent(models.Model):
    _name = "nsp.measurement.event"
    _description = "NSP Lane Calibration Observation"
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
    tid_addr = fields.Integer(string="TID Start Address (Words)", required=True, default=0)
    tid_len = fields.Integer(string="TID Length (Words)", required=True, default=4)

    # Presentation-only fields for the native Odoo Detection Timeline list.
    # They are computed from raw observations and do not change the sync contract.
    timeline_timestamp = fields.Char(
        string="Timestamp",
        compute="_compute_timeline_display",
        readonly=True,
    )
    timeline_reader = fields.Char(
        string="Reader",
        compute="_compute_timeline_display",
        readonly=True,
    )
    timeline_duration_ms = fields.Float(
        string="Duration (ms)",
        compute="_compute_timeline_display",
        digits=(16, 3),
        readonly=True,
    )

    @api.depends(
        "session_id",
        "revision",
        "read_at",
        "read_at_ms",
        "serial_number",
        "session_id.device_node_ids.reader_id.name",
        "session_id.device_node_ids.reader_id.serial_number",
    )
    def _compute_timeline_display(self):
        for event in self:
            event.timeline_timestamp = ""
            event.timeline_reader = event.serial_number or ""
            event.timeline_duration_ms = 0.0

        persisted = self.filtered(lambda event: event.id and event.session_id)
        if not persisted:
            return

        session_ids = persisted.mapped("session_id").ids
        requested_pairs = {
            (event.session_id.id, int(event.revision or 1))
            for event in persisted
        }

        reader_name_by_key = {}
        for session in persisted.mapped("session_id"):
            for node in session._reader_nodes():
                serial = str(node.reader_id.serial_number or "").strip().upper()
                if serial:
                    reader_name_by_key[(session.id, serial)] = (
                        node.reader_id.name or node.reader_id.serial_number or serial
                    )

        revisions = sorted({revision for _session_id, revision in requested_pairs})
        all_events = self.search(
            [("session_id", "in", session_ids), ("revision", "in", revisions)],
            order="session_id, revision, read_at asc, read_at_ms asc, id asc",
        )
        previous_seconds = {}
        duration_by_id = {}
        for event in all_events:
            pair = (event.session_id.id, int(event.revision or 1))
            if pair not in requested_pairs:
                continue
            observed_at = fields.Datetime.to_datetime(event.read_at)
            seconds = (
                observed_at.timestamp() + (int(event.read_at_ms or 0) / 1000.0)
                if observed_at
                else 0.0
            )
            previous = previous_seconds.get(pair)
            duration_by_id[event.id] = (
                max((seconds - previous) * 1000.0, 0.0)
                if previous is not None
                else 0.0
            )
            previous_seconds[pair] = seconds

        for event in persisted:
            base = fields.Datetime.to_string(event.read_at) if event.read_at else ""
            event.timeline_timestamp = (
                "%s.%03d" % (base, int(event.read_at_ms or 0))
                if base
                else ""
            )
            serial = str(event.serial_number or "").strip().upper()
            event.timeline_reader = reader_name_by_key.get(
                (event.session_id.id, serial), event.serial_number or ""
            )
            event.timeline_duration_ms = duration_by_id.get(event.id, 0.0)

    _sql_constraints = [
        ("measurement_event_uid_unique", "unique(event_uid)", "Measurement Event UID must be unique."),
        ("measurement_event_port_positive", "CHECK(port_no > 0)", "Reader Port must be greater than zero."),
        ("measurement_event_revision_positive", "CHECK(revision > 0)", "Lane Calibration Revision must be greater than zero."),
        ("measurement_event_ms_range", "CHECK(read_at_ms >= 0 AND read_at_ms <= 999)", "Measurement millisecond must be between 0 and 999."),
        ("measurement_event_read_interval_range", "CHECK(read_interval_ms > 0 AND read_interval_ms <= 60000)", "Read Interval must be between 1 and 60000 ms."),
        ("measurement_event_tid_addr_nonnegative", "CHECK(tid_addr >= 0)", "TID Start Address must not be negative."),
        ("measurement_event_tid_len_positive", "CHECK(tid_len > 0)", "TID Length must be greater than zero."),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for source in vals_list:
            vals = dict(source)
            vals["event_uid"] = str(vals.get("event_uid") or "").strip()
            vals["serial_number"] = str(vals.get("serial_number") or "").strip().upper()
            try:
                vals["tid"] = _normalize_raw_tid_value(vals.get("tid"))
            except ValueError as exc:
                raise ValidationError(_("Lane Calibration TID must contain hexadecimal characters only.")) from exc
            vals["revision"] = max(int(vals.get("revision") or 1), 1)
            vals["read_at_ms"] = max(0, min(int(vals.get("read_at_ms") or 0), 999))
            vals["read_interval_ms"] = max(1, min(int(vals.get("read_interval_ms") or 200), 60000))
            vals["tid_addr"] = max(int(vals.get("tid_addr") or 0), 0)
            vals["tid_len"] = max(int(vals.get("tid_len") or 4), 1)
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
            and fields.Datetime.to_datetime(self.read_at)
            == fields.Datetime.to_datetime(values["read_at"])
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
            and int(self.tid_addr or 0) == int(values["tid_addr"] or 0)
            and int(self.tid_len or 0) == int(values["tid_len"] or 0)
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
                raise ValidationError(_("Reader Port is not part of the Lane Calibration."))
            if event.tid not in session._allowed_target_tids():
                raise ValidationError(_("Only the active Calibration Tag may be stored in this Lane Calibration."))
