# -*- coding: utf-8 -*-
import uuid
from collections import defaultdict
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

def _new_uid(prefix):
    return "%s-%s" % (prefix, uuid.uuid4().hex.upper())

class NspMeasurementSession(models.Model):
    _name = "nsp.measurement.session"
    _description = "NSP Measurement Session"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _rec_name = "measurement_code"
    _order = "create_date desc, id desc"

    measurement_session_uid = fields.Char(
        required=True, copy=False, readonly=True, index=True,
        default=lambda self: _new_uid("MSR"), tracking=True,
    )
    measurement_code = fields.Char(
        required=True, copy=False, index=True, tracking=True,
        default=lambda self: _new_uid("MSR")[:24],
    )
    controller_id = fields.Many2one(
        "nsp.controller", required=True, ondelete="restrict", index=True, tracking=True,
        help="Controller that owns all RFID readers selected for this measurement session.",
    )
    planned_start_at = fields.Datetime(tracking=True)
    planned_end_at = fields.Datetime(tracking=True)
    note = fields.Text()

    measurement_status = fields.Selection([
        ("draft", "Draft"),
        ("ready", "Ready"),
        ("measuring", "Measuring"),
        ("completed", "Completed"),
        ("cancelled", "Cancelled"),
    ], required=True, default="draft", index=True, tracking=True)

    started_at = fields.Datetime(readonly=True, copy=False)
    completed_at = fields.Datetime(readonly=True, copy=False)
    cancelled_at = fields.Datetime(readonly=True, copy=False)

    antenna_ids = fields.One2many(
        "nsp.measurement.antenna", "session_id", string="Measurement Antennas"
    )
    run_ids = fields.One2many(
        "nsp.measurement.run", "session_id", string="Measurement Runs", readonly=True
    )
    command_ids = fields.One2many(
        "nsp.measurement.command", "session_id", string="Commands", readonly=True
    )
    event_ids = fields.One2many(
        "nsp.measurement.event", "session_id", string="Measurement Events", readonly=True
    )
    antenna_summary_ids = fields.One2many(
        "nsp.measurement.antenna.summary", "session_id", string="Antenna Summary", readonly=True
    )
    pair_summary_ids = fields.One2many(
        "nsp.measurement.pair.summary", "session_id", string="Antenna Pair Summary", readonly=True
    )
    run_count = fields.Integer(compute="_compute_run_count")
    event_count = fields.Integer(default=0, readonly=True, copy=False)

    _sql_constraints = [
        ("measurement_session_uid_unique", "unique(measurement_session_uid)", "Measurement Session UID must be unique."),
        ("measurement_code_unique", "unique(measurement_code)", "Measurement Code must be unique."),
    ]

    @api.depends("run_ids")
    def _compute_run_count(self):
        for rec in self:
            rec.run_count = len(rec.run_ids)

    @api.constrains("planned_start_at", "planned_end_at")
    def _check_planned_time(self):
        for rec in self:
            if rec.planned_start_at and rec.planned_end_at and rec.planned_end_at <= rec.planned_start_at:
                raise ValidationError(_("Planned end time must be later than planned start time."))

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for values in vals_list:
            vals = dict(values)
            vals["measurement_session_uid"] = str(vals.get("measurement_session_uid") or _new_uid("MSR")).strip().upper()
            vals["measurement_code"] = str(vals.get("measurement_code") or vals["measurement_session_uid"][:24]).strip().upper()
            if not self.env.context.get("measurement_sync"):
                vals["measurement_status"] = "draft"
            prepared.append(vals)
        return super().create(prepared)

    def write(self, vals):
        config_fields = {
            "measurement_code", "controller_id", "planned_start_at", "planned_end_at",
            "note", "antenna_ids",
        }
        if config_fields.intersection(vals) and not self.env.context.get("measurement_sync"):
            blocked = self.filtered(lambda rec: rec.measurement_status != "draft")
            if blocked:
                raise ValidationError(_("Only draft Measurement Sessions can be edited."))
        if vals.get("measurement_code"):
            vals = dict(vals)
            vals["measurement_code"] = str(vals["measurement_code"]).strip().upper()
        return super().write(vals)

    def _canonical_config(self):
        self.ensure_one()
        grouped = defaultdict(list)
        for line in self.antenna_ids.sorted(
            key=lambda rec: (rec.serial_number or "", rec.antenna_no or 0, rec.id)
        ):
            grouped[line.serial_number].append(int(line.antenna_no))
        return {
            "measurement_session_uid": self.measurement_session_uid,
            "measurement_code": self.measurement_code,
            "controller_code": self.controller_id.controller_id,
            "planned_start_at": fields.Datetime.to_string(self.planned_start_at) if self.planned_start_at else None,
            "planned_end_at": fields.Datetime.to_string(self.planned_end_at) if self.planned_end_at else None,
            "note": self.note or None,
            "measurement_antennas": [
                {"serial_number": serial_number, "antennas": antenna_numbers}
                for serial_number, antenna_numbers in sorted(grouped.items())
            ],
        }

    def _validate_ready(self):
        self.ensure_one()
        if self.measurement_status != "draft":
            raise ValidationError(_("Only draft Measurement Sessions can be released."))
        if not self.antenna_ids:
            raise ValidationError(_("Configure at least one Measurement Antenna."))
        # Release validates configuration ownership only. Reader connectivity,
        # device active state and antenna availability are verified by the
        # Controller when it applies the released configuration.
        self.antenna_ids._check_antenna_scope()

    def action_ready(self):
        for rec in self:
            if rec.measurement_status == "ready":
                continue
            rec._validate_ready()
            rec.with_context(measurement_sync=True).write({"measurement_status": "ready"})
        return True

    def _build_summaries(self):
        AntennaSummary = self.env["nsp.measurement.antenna.summary"].sudo()
        PairSummary = self.env["nsp.measurement.pair.summary"].sudo()
        for session in self:
            session.antenna_summary_ids.unlink()
            session.pair_summary_ids.unlink()
            events = session.event_ids.sorted(key=lambda rec: (rec.run_id.id, rec.tid or "", rec.read_at, rec.id))

            by_antenna = defaultdict(list)
            for event in events:
                by_antenna[(event.serial_number, event.antenna_no)].append(event)
            antenna_vals = []
            for (serial_number, antenna_no), rows in sorted(by_antenna.items()):
                rssis = [float(row.rssi_dbm) for row in rows if row.rssi_dbm not in (False, None)]
                antenna_vals.append({
                    "session_id": session.id,
                    "serial_number": serial_number,
                    "antenna_no": antenna_no,
                    "read_count": len(rows),
                    "min_rssi_dbm": min(rssis) if rssis else False,
                    "max_rssi_dbm": max(rssis) if rssis else False,
                    "average_rssi_dbm": (sum(rssis) / len(rssis)) if rssis else False,
                    "first_read_at": min(row.read_at for row in rows),
                    "last_read_at": max(row.read_at for row in rows),
                })
            if antenna_vals:
                AntennaSummary.create(antenna_vals)

            pair_values = defaultdict(list)
            by_run_tid = defaultdict(list)
            for event in events:
                by_run_tid[(event.run_id.id, event.tid)].append(event)
            for rows in by_run_tid.values():
                ordered = sorted(rows, key=lambda rec: (rec.read_at, rec.id))
                previous = False
                for current in ordered:
                    if previous and (previous.serial_number, previous.antenna_no) != (current.serial_number, current.antenna_no):
                        delta = fields.Datetime.to_datetime(current.read_at) - fields.Datetime.to_datetime(previous.read_at)
                        delta_ms = max(int(delta.total_seconds() * 1000), 0)
                        key = (
                            previous.serial_number, previous.antenna_no,
                            current.serial_number, current.antenna_no,
                        )
                        pair_values[key].append(delta_ms)
                    previous = current
            pair_vals = []
            for key, intervals in sorted(pair_values.items()):
                pair_vals.append({
                    "session_id": session.id,
                    "from_serial_number": key[0],
                    "from_antenna_no": key[1],
                    "to_serial_number": key[2],
                    "to_antenna_no": key[3],
                    "sample_count": len(intervals),
                    "min_interval_ms": min(intervals),
                    "max_interval_ms": max(intervals),
                    "average_interval_ms": int(sum(intervals) / len(intervals)),
                })
            if pair_vals:
                PairSummary.create(pair_vals)

    def action_complete(self):
        for rec in self:
            if rec.measurement_status == "completed":
                continue
            if rec.measurement_status != "measuring":
                raise ValidationError(_("Only measuring sessions can be completed."))
            if rec.run_ids.filtered(lambda run: run.run_status in ("pending", "starting", "running", "stopping")):
                raise ValidationError(_("Stop all Measurement Runs before completing the session."))
            if rec.event_count <= 0:
                raise ValidationError(_("The Measurement Session has no measured data."))
            expected_count = sum(rec.run_ids.mapped("measurement_count"))
            if rec.event_count < expected_count:
                raise ValidationError(_("Measurement data is still pending synchronization."))
            rec._build_summaries()
            rec.with_context(measurement_sync=True).write({
                "measurement_status": "completed",
                "completed_at": fields.Datetime.now(),
            })
        return True

    def action_cancel(self):
        for rec in self:
            if rec.measurement_status in ("completed", "cancelled"):
                raise ValidationError(_("Completed or cancelled sessions cannot be cancelled again."))
            if rec.run_ids.filtered(lambda run: run.run_status in ("starting", "running", "stopping")):
                raise ValidationError(_("Stop the active Measurement Run before cancelling the session."))
            pending_runs = rec.run_ids.filtered(lambda run: run.run_status == "pending")
            pending_commands = rec.command_ids.filtered(
                lambda command: command.run_id in pending_runs and command.command_status == "pending"
            )
            pending_commands.sudo().write({
                "command_status": "failed",
                "effective_at": fields.Datetime.now(),
                "error_code": "measurement_session_cancelled",
                "error_message": "Measurement Session was cancelled before the command was executed.",
            })
            pending_runs.sudo().write({"run_status": "failed"})
            rec.with_context(measurement_sync=True).write({
                "measurement_status": "cancelled",
                "cancelled_at": fields.Datetime.now(),
            })
        return True

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
        events = self.env["nsp.measurement.event"].sudo().search([
            ("session_id.measurement_status", "in", ["completed", "cancelled"]),
            ("session_id.write_date", "<", cutoff),
            ("sync_state", "=", "synced"),
        ], limit=5000)
        count = len(events)
        events.with_context(retention_cleanup=True).unlink()
        return count

class NspMeasurementAntenna(models.Model):
    _name = "nsp.measurement.antenna"
    _description = "NSP Measurement Antenna"
    _order = "session_id, antenna_ref_id, id"

    session_id = fields.Many2one(
        "nsp.measurement.session", required=True, ondelete="cascade", index=True
    )
    antenna_ref_id = fields.Many2one(
        "nsp.device.antenna", required=True, ondelete="restrict", index=True
    )
    serial_number = fields.Char(
        related="antenna_ref_id.device_id.serial_number", readonly=True
    )
    antenna_no = fields.Integer(
        related="antenna_ref_id.antenna_id", readonly=True
    )

    _sql_constraints = [
        ("measurement_antenna_unique", "unique(session_id, antenna_ref_id)", "The antenna is already included in this Measurement Session."),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        sessions = self.env["nsp.measurement.session"].browse([
            vals.get("session_id") for vals in vals_list if vals.get("session_id")
        ]).exists()
        if not self.env.context.get("measurement_sync") and sessions.filtered(lambda rec: rec.measurement_status != "draft"):
            raise ValidationError(_("Measurement Antennas can only be added while the session is draft."))
        return super().create(vals_list)

    @api.constrains("session_id", "antenna_ref_id")
    def _check_antenna_scope(self):
        for rec in self:
            device = rec.antenna_ref_id.device_id
            if not device or device.controller_id != rec.session_id.controller_id:
                raise ValidationError(_("Measurement Antenna must belong to the selected Controller."))

    def write(self, vals):
        if not self.env.context.get("measurement_sync") and self.filtered(lambda rec: rec.session_id.measurement_status != "draft"):
            raise ValidationError(_("Measurement Antennas can only be edited while the session is draft."))
        return super().write(vals)

    def unlink(self):
        if not self.env.context.get("measurement_sync") and self.filtered(lambda rec: rec.session_id.measurement_status != "draft"):
            raise ValidationError(_("Measurement Antennas can only be removed while the session is draft."))
        return super().unlink()

class NspMeasurementRun(models.Model):
    _name = "nsp.measurement.run"
    _description = "NSP Measurement Run"
    _rec_name = "measurement_run_uid"
    _order = "create_date desc, id desc"

    measurement_run_uid = fields.Char(
        required=True, copy=False, readonly=True, index=True,
        default=lambda self: _new_uid("RUN"),
    )
    session_id = fields.Many2one(
        "nsp.measurement.session", required=True, ondelete="cascade", index=True
    )
    run_status = fields.Selection([
        ("pending", "Pending"),
        ("starting", "Starting"),
        ("running", "Running"),
        ("stopping", "Stopping"),
        ("stopped", "Stopped"),
        ("failed", "Failed"),
    ], required=True, default="pending", index=True)
    started_at = fields.Datetime(readonly=True)
    stopped_at = fields.Datetime(readonly=True)
    measurement_count = fields.Integer(default=0, readonly=True)

    _sql_constraints = [
        ("measurement_run_uid_unique", "unique(measurement_run_uid)", "Measurement Run UID must be unique."),
    ]

class NspMeasurementCommand(models.Model):
    _name = "nsp.measurement.command"
    _description = "NSP Measurement Command"
    _rec_name = "command_uid"
    _order = "create_date asc, id asc"

    command_uid = fields.Char(
        required=True, copy=False, readonly=True, index=True,
        default=lambda self: _new_uid("CMD"),
    )
    session_id = fields.Many2one(
        "nsp.measurement.session", required=True, ondelete="cascade", index=True
    )
    run_id = fields.Many2one(
        "nsp.measurement.run", required=True, ondelete="cascade", index=True
    )
    command_type = fields.Selection([
        ("start_measurement", "Start Measurement"),
        ("stop_measurement", "Stop Measurement"),
    ], required=True, index=True)
    command_status = fields.Selection([
        ("pending", "Pending"),
        ("succeeded", "Succeeded"),
        ("failed", "Failed"),
    ], required=True, default="pending", index=True)
    requested_at = fields.Datetime(required=True, default=fields.Datetime.now)
    effective_at = fields.Datetime(readonly=True)
    error_code = fields.Char(readonly=True)
    error_message = fields.Text(readonly=True)

    _sql_constraints = [
        ("measurement_command_uid_unique", "unique(command_uid)", "Measurement Command UID must be unique."),
    ]

    @api.constrains("session_id", "run_id")
    def _check_run_session(self):
        for rec in self:
            if rec.run_id.session_id != rec.session_id:
                raise ValidationError(_("Measurement Command and Run must belong to the same Session."))

class NspMeasurementEvent(models.Model):
    _name = "nsp.measurement.event"
    _description = "NSP Measurement Event"
    _rec_name = "measurement_uid"
    _order = "read_at desc, id desc"

    measurement_uid = fields.Char(required=True, copy=False, index=True)
    session_id = fields.Many2one(
        "nsp.measurement.session", required=True, ondelete="cascade", index=True
    )
    run_id = fields.Many2one(
        "nsp.measurement.run", required=True, ondelete="cascade", index=True
    )
    serial_number = fields.Char(required=True, index=True)
    antenna_no = fields.Integer(required=True, index=True)
    tid = fields.Char(required=True, index=True)
    read_at = fields.Datetime(required=True, index=True)
    rssi_dbm = fields.Float()
    payload_hash = fields.Char(required=True, copy=False, index=True)
    sync_state = fields.Selection([
        ("pending", "Pending"),
        ("synced", "Synced"),
        ("failed", "Failed"),
    ], required=True, default="pending", index=True, copy=False)
    retry_count = fields.Integer(default=0, copy=False)
    next_retry_at = fields.Datetime(copy=False)
    last_sync_at = fields.Datetime(copy=False)

    _sql_constraints = [
        ("measurement_event_uid_unique", "unique(measurement_uid)", "Measurement UID must be unique."),
        ("measurement_antenna_no_positive", "CHECK(antenna_no > 0)", "Antenna number must be positive."),
        ("measurement_retry_non_negative", "CHECK(retry_count >= 0)", "Retry count cannot be negative."),
    ]

    @api.constrains("session_id", "run_id", "serial_number", "antenna_no")
    def _check_event_scope(self):
        Mapping = self.env["nsp.measurement.antenna"].sudo()
        for rec in self:
            if rec.run_id.session_id != rec.session_id:
                raise ValidationError(_("Measurement Event and Run must belong to the same Session."))
            mapping = Mapping.search([
                ("session_id", "=", rec.session_id.id),
                ("antenna_ref_id.device_id.serial_number", "=", rec.serial_number),
                ("antenna_ref_id.antenna_id", "=", rec.antenna_no),
            ], limit=1)
            if not mapping:
                raise ValidationError(_("Measurement Event antenna is not part of the Measurement Session."))

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        by_session = defaultdict(int)
        by_run = defaultdict(int)
        for rec in records:
            by_session[rec.session_id.id] += 1
            by_run[rec.run_id.id] += 1
        for session_id, count in by_session.items():
            self.env.cr.execute(
                "UPDATE nsp_measurement_session "
                "SET event_count = COALESCE(event_count, 0) + %s WHERE id = %s",
                (count, session_id),
            )
        for run_id, count in by_run.items():
            self.env.cr.execute(
                "UPDATE nsp_measurement_run "
                "SET measurement_count = COALESCE(measurement_count, 0) + %s WHERE id = %s",
                (count, run_id),
            )
        self.env["nsp.measurement.session"].browse(list(by_session)).invalidate_recordset(["event_count"])
        self.env["nsp.measurement.run"].browse(list(by_run)).invalidate_recordset(["measurement_count"])
        return records

    def unlink(self):
        if self.env.context.get("retention_cleanup"):
            return super().unlink()
        by_session = defaultdict(int)
        by_run = defaultdict(int)
        for rec in self:
            by_session[rec.session_id.id] += 1
            by_run[rec.run_id.id] += 1
        result = super().unlink()
        for session_id, count in by_session.items():
            self.env.cr.execute(
                "UPDATE nsp_measurement_session "
                "SET event_count = GREATEST(COALESCE(event_count, 0) - %s, 0) WHERE id = %s",
                (count, session_id),
            )
        for run_id, count in by_run.items():
            self.env.cr.execute(
                "UPDATE nsp_measurement_run "
                "SET measurement_count = GREATEST(COALESCE(measurement_count, 0) - %s, 0) WHERE id = %s",
                (count, run_id),
            )
        self.env["nsp.measurement.session"].browse(list(by_session)).invalidate_recordset(["event_count"])
        self.env["nsp.measurement.run"].browse(list(by_run)).invalidate_recordset(["measurement_count"])
        return result

class NspMeasurementAntennaSummary(models.Model):
    _name = "nsp.measurement.antenna.summary"
    _description = "NSP Measurement Antenna Summary"
    _order = "session_id, serial_number, antenna_no, id"

    session_id = fields.Many2one(
        "nsp.measurement.session", required=True, ondelete="cascade", index=True
    )
    serial_number = fields.Char(required=True, index=True)
    antenna_no = fields.Integer(required=True, index=True)
    read_count = fields.Integer(required=True)
    min_rssi_dbm = fields.Float()
    max_rssi_dbm = fields.Float()
    average_rssi_dbm = fields.Float()
    first_read_at = fields.Datetime(required=True)
    last_read_at = fields.Datetime(required=True)

    _sql_constraints = [
        ("measurement_antenna_summary_unique", "unique(session_id, serial_number, antenna_no)", "Antenna summary must be unique per Measurement Session."),
    ]

class NspMeasurementPairSummary(models.Model):
    _name = "nsp.measurement.pair.summary"
    _description = "NSP Measurement Antenna Pair Summary"
    _order = "session_id, from_serial_number, from_antenna_no, to_serial_number, to_antenna_no, id"

    session_id = fields.Many2one(
        "nsp.measurement.session", required=True, ondelete="cascade", index=True
    )
    from_serial_number = fields.Char(required=True, index=True)
    from_antenna_no = fields.Integer(required=True, index=True)
    to_serial_number = fields.Char(required=True, index=True)
    to_antenna_no = fields.Integer(required=True, index=True)
    sample_count = fields.Integer(required=True)
    min_interval_ms = fields.Integer(required=True)
    max_interval_ms = fields.Integer(required=True)
    average_interval_ms = fields.Integer(required=True)

    _sql_constraints = [
        (
            "measurement_pair_summary_unique",
            "unique(session_id, from_serial_number, from_antenna_no, to_serial_number, to_antenna_no)",
            "Antenna pair summary must be unique per Measurement Session.",
        ),
    ]
