# -*- coding: utf-8 -*-
import uuid
from collections import defaultdict
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


def _new_measurement_code():
    return "MSR-%s" % uuid.uuid4().hex[:16].upper()


class NspMeasurementSession(models.Model):
    """One measurement plan executed by one Controller.

    The session owns only the selected physical antennas and its runtime state.
    Raw detections are stored as ``nsp.measurement.event`` records.  Runs,
    commands and stored summary tables are intentionally not used.
    """

    _name = "nsp.measurement.session"
    _description = "NSP Measurement Session"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _rec_name = "measurement_code"
    _order = "create_date desc, id desc"

    measurement_code = fields.Char(
        required=True,
        copy=False,
        index=True,
        tracking=True,
        default=lambda self: _new_measurement_code(),
    )
    controller_id = fields.Many2one(
        "nsp.controller",
        required=True,
        ondelete="restrict",
        index=True,
        tracking=True,
        help="Controller that manages every Reader selected for this session.",
    )
    planned_start_at = fields.Datetime(tracking=True)
    planned_end_at = fields.Datetime(tracking=True)
    started_at = fields.Datetime(readonly=True, copy=False)
    ended_at = fields.Datetime(readonly=True, copy=False)
    note = fields.Text()
    status = fields.Selection(
        [
            ("draft", "Draft"),
            ("ready", "Ready"),
            ("running", "Running"),
            ("completed", "Completed"),
            ("failed", "Failed"),
            ("cancelled", "Cancelled"),
        ],
        required=True,
        default="draft",
        index=True,
        tracking=True,
    )
    antenna_ids = fields.Many2many(
        "nsp.device.antenna",
        "nsp_measurement_session_antenna_rel",
        "session_id",
        "antenna_id",
        string="Measurement Antennas",
        help="Physical Reader antennas included in this measurement session.",
    )
    event_ids = fields.One2many(
        "nsp.measurement.event",
        "session_id",
        string="Measurement Events",
        readonly=True,
    )
    event_count = fields.Integer(compute="_compute_event_count")

    _sql_constraints = [
        (
            "measurement_code_unique",
            "unique(measurement_code)",
            "Measurement Code must be unique.",
        ),
    ]

    @api.depends("event_ids")
    def _compute_event_count(self):
        counts = self.env["nsp.measurement.event"].sudo()._read_group(
            [("session_id", "in", self.ids)],
            ["session_id"],
            ["__count"],
        ) if self.ids else []
        by_session = {session.id: count for session, count in counts}
        for session in self:
            session.event_count = by_session.get(session.id, 0)

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for source in vals_list:
            vals = dict(source)
            vals["measurement_code"] = str(
                vals.get("measurement_code") or _new_measurement_code()
            ).strip().upper()
            if not self.env.context.get("measurement_sync"):
                vals["status"] = "draft"
            prepared.append(vals)
        return super().create(prepared)

    def write(self, vals):
        values = dict(vals)
        configuration_fields = {
            "measurement_code",
            "controller_id",
            "planned_start_at",
            "planned_end_at",
            "note",
            "antenna_ids",
        }
        if configuration_fields.intersection(values) and not self.env.context.get("measurement_sync"):
            if self.filtered(lambda session: session.status != "draft"):
                raise ValidationError(_("Only draft Measurement Sessions can be edited."))
        if "measurement_code" in values:
            values["measurement_code"] = str(values.get("measurement_code") or "").strip().upper()
        return super().write(values)

    @api.constrains("planned_start_at", "planned_end_at")
    def _check_planned_time(self):
        for session in self:
            if (
                session.planned_start_at
                and session.planned_end_at
                and session.planned_end_at <= session.planned_start_at
            ):
                raise ValidationError(_("Planned end time must be later than planned start time."))

    @api.constrains("controller_id", "antenna_ids")
    def _check_antenna_scope(self):
        for session in self:
            invalid = session.antenna_ids.filtered(
                lambda antenna: antenna.device_id.controller_id != session.controller_id
            )
            if invalid:
                raise ValidationError(
                    _("Every Measurement Antenna must belong to the selected Controller.")
                )

    def _configuration_payload(self):
        self.ensure_one()
        grouped = defaultdict(list)
        for antenna in self.antenna_ids.sorted(
            key=lambda item: (
                item.device_id.serial_number or "",
                item.antenna_id or 0,
                item.id,
            )
        ):
            grouped[antenna.device_id.serial_number].append(int(antenna.antenna_id))
        return {
            "measurement_code": self.measurement_code,
            "controller_code": self.controller_id.controller_id,
            "planned_start_at": self.planned_start_at,
            "planned_end_at": self.planned_end_at,
            "note": self.note or None,
            "measurement_antennas": [
                {
                    "serial_number": serial_number,
                    "antennas": sorted(set(antenna_numbers)),
                }
                for serial_number, antenna_numbers in sorted(grouped.items())
            ],
        }

    def action_ready(self):
        for session in self:
            if session.status == "ready":
                continue
            if session.status != "draft":
                raise ValidationError(_("Only draft Measurement Sessions can be released."))
            if not session.antenna_ids:
                raise ValidationError(_("Select at least one Measurement Antenna."))
            session._check_antenna_scope()
            session.with_context(measurement_sync=True).write({"status": "ready"})
        return True

    def action_complete(self):
        for session in self:
            if session.status == "completed":
                continue
            if session.status != "running":
                raise ValidationError(_("Only running Measurement Sessions can be completed."))
            session.with_context(measurement_sync=True).write(
                {"status": "completed", "ended_at": fields.Datetime.now()}
            )
        return True

    def action_cancel(self):
        for session in self:
            if session.status in ("completed", "failed", "cancelled"):
                raise ValidationError(_("Completed, failed, or cancelled sessions cannot be cancelled."))
            session.with_context(measurement_sync=True).write(
                {"status": "cancelled", "ended_at": fields.Datetime.now()}
            )
        return True

    def action_view_events(self):
        self.ensure_one()
        action = self.env.ref("nsp_gatekeeper.action_nsp_measurement_event").read()[0]
        action["domain"] = [("session_id", "=", self.id)]
        action["context"] = {"search_default_session_id": self.id}
        return action

    def action_open_live(self):
        self.ensure_one()
        return {
            "type": "ir.actions.client",
            "name": _("Live Measurement"),
            "tag": "nsp_measurement_live",
            "params": {"session_id": self.id},
        }

    @api.model
    def get_live_snapshot(self, session_id, last_event_id=0, limit=100):
        """Return only new Events plus current aggregate values for 1-second UI polling."""
        session = self.sudo().browse(int(session_id or 0)).exists()
        if not session:
            return {"found": False}
        try:
            last_event_id = max(int(last_event_id or 0), 0)
            limit = min(max(int(limit or 100), 1), 500)
        except Exception:
            last_event_id, limit = 0, 100
        Event = self.env["nsp.measurement.event"].sudo()
        if last_event_id:
            new_events = Event.search(
                [("session_id", "=", session.id), ("id", ">", last_event_id)],
                order="id asc",
                limit=limit,
            )
        else:
            new_events = Event.search(
                [("session_id", "=", session.id)],
                order="id desc",
                limit=limit,
            ).sorted(key=lambda event: event.id)
        latest = max(new_events.ids or [last_event_id])
        return {
            "found": True,
            "measurement_code": session.measurement_code,
            "controller_code": session.controller_id.controller_id,
            "status": session.status,
            "event_count": session.event_count,
            "last_event_id": latest,
            "events": [
                {
                    "id": event.id,
                    "event_uid": event.event_uid,
                    "serial_number": event.serial_number,
                    "antenna_no": event.antenna_no,
                    "tid": event.tid,
                    "read_at": fields.Datetime.to_string(event.read_at),
                    "rssi_dbm": event.rssi_dbm,
                }
                for event in new_events
            ],
            "antenna_summary": [
                {
                    **row,
                    "first_read_at": fields.Datetime.to_string(row["first_read_at"])
                    if row.get("first_read_at") else None,
                    "last_read_at": fields.Datetime.to_string(row["last_read_at"])
                    if row.get("last_read_at") else None,
                }
                for row in (session._antenna_summary() if not last_event_id else [])
            ],
        }

    def _antenna_summary(self):
        """Build current antenna statistics without storing derived rows."""
        self.ensure_one()
        rows = self.env["nsp.measurement.event"].sudo()._read_group(
            [("session_id", "=", self.id)],
            ["serial_number", "antenna_no"],
            [
                "__count",
                "rssi_dbm:count",
                "rssi_dbm:min",
                "rssi_dbm:avg",
                "rssi_dbm:max",
                "read_at:min",
                "read_at:max",
            ],
            order="serial_number, antenna_no",
        )
        return [
            {
                "serial_number": serial_number,
                "antenna_no": int(antenna_no or 0),
                "read_count": int(count or 0),
                "rssi_sample_count": int(rssi_count or 0),
                "min_rssi_dbm": min_rssi,
                "average_rssi_dbm": avg_rssi,
                "max_rssi_dbm": max_rssi,
                "first_read_at": first_read,
                "last_read_at": last_read,
            }
            for (
                serial_number,
                antenna_no,
                count,
                rssi_count,
                min_rssi,
                avg_rssi,
                max_rssi,
                first_read,
                last_read,
            ) in rows
        ]

    def _transition_summary(self):
        """Calculate antenna-to-antenna detection intervals from raw Events."""
        self.ensure_one()
        self.env.cr.execute(
            """
            WITH ordered AS (
                SELECT
                    serial_number,
                    antenna_no,
                    tid,
                    read_at,
                    LAG(serial_number) OVER (PARTITION BY tid ORDER BY read_at, id) AS from_serial_number,
                    LAG(antenna_no) OVER (PARTITION BY tid ORDER BY read_at, id) AS from_antenna_no,
                    LAG(read_at) OVER (PARTITION BY tid ORDER BY read_at, id) AS previous_read_at
                FROM nsp_measurement_event
                WHERE session_id = %s
            )
            SELECT
                from_serial_number,
                from_antenna_no,
                serial_number,
                antenna_no,
                COUNT(*)::integer,
                MIN(EXTRACT(EPOCH FROM (read_at - previous_read_at)) * 1000)::bigint,
                AVG(EXTRACT(EPOCH FROM (read_at - previous_read_at)) * 1000)::bigint,
                MAX(EXTRACT(EPOCH FROM (read_at - previous_read_at)) * 1000)::bigint
            FROM ordered
            WHERE previous_read_at IS NOT NULL
              AND (from_serial_number, from_antenna_no) IS DISTINCT FROM (serial_number, antenna_no)
            GROUP BY from_serial_number, from_antenna_no, serial_number, antenna_no
            ORDER BY from_serial_number, from_antenna_no, serial_number, antenna_no
            """,
            (self.id,),
        )
        return [
            {
                "from_serial_number": row[0],
                "from_antenna_no": int(row[1] or 0),
                "to_serial_number": row[2],
                "to_antenna_no": int(row[3] or 0),
                "sample_count": int(row[4] or 0),
                "min_interval_ms": int(row[5] or 0),
                "average_interval_ms": int(row[6] or 0),
                "max_interval_ms": int(row[7] or 0),
            }
            for row in self.env.cr.fetchall()
        ]

    @api.model
    def cron_cleanup_expired_measurements(self):
        value = self.env["ir.config_parameter"].sudo().get_param(
            "nsp_gatekeeper.measurement_retention_days", "7"
        )
        try:
            retention_days = max(int(value), 1)
        except Exception:
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
        return count


class NspMeasurementEvent(models.Model):
    """One RFID detection captured during a Measurement Session."""

    _name = "nsp.measurement.event"
    _description = "NSP Measurement Event"
    _rec_name = "event_uid"
    _order = "read_at desc, id desc"

    event_uid = fields.Char(required=True, copy=False, index=True)
    session_id = fields.Many2one(
        "nsp.measurement.session",
        required=True,
        ondelete="cascade",
        index=True,
    )
    serial_number = fields.Char(required=True, index=True)
    antenna_no = fields.Integer(required=True, index=True)
    tid = fields.Char(required=True, index=True)
    read_at = fields.Datetime(required=True, index=True)
    rssi_dbm = fields.Float()

    _sql_constraints = [
        (
            "measurement_event_uid_unique",
            "unique(event_uid)",
            "Measurement Event UID must be unique.",
        ),
        (
            "measurement_event_antenna_positive",
            "CHECK(antenna_no > 0)",
            "Antenna number must be greater than zero.",
        ),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for source in vals_list:
            vals = dict(source)
            vals["event_uid"] = str(vals.get("event_uid") or "").strip()
            vals["serial_number"] = str(vals.get("serial_number") or "").strip().upper()
            vals["tid"] = str(vals.get("tid") or "").strip().upper()
            prepared.append(vals)
        return super().create(prepared)

    @api.constrains("session_id", "serial_number", "antenna_no")
    def _check_event_scope(self):
        for event in self:
            matched = event.session_id.antenna_ids.filtered(
                lambda antenna: (
                    antenna.device_id.serial_number == event.serial_number
                    and antenna.antenna_id == event.antenna_no
                )
            )
            if not matched:
                raise ValidationError(
                    _("Measurement Event antenna is not part of the Measurement Session.")
                )
