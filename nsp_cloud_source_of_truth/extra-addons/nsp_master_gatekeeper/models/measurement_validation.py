# -*- coding: utf-8 -*-
import json
import math
from statistics import median

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError
from odoo.addons.nsp_core.utils import new_management_code


def _percentile(values, percentile):
    data = sorted(float(value) for value in values if value is not None)
    if not data:
        return 0.0
    index = max(0, min(len(data) - 1, math.ceil((percentile / 100.0) * len(data)) - 1))
    return data[index]


def _event_seconds(event):
    value = fields.Datetime.to_datetime(event.read_at)
    if not value:
        return 0.0
    return value.timestamp() + (int(event.read_at_ms or 0) / 1000.0)


class NspMeasurementSessionValidation(models.Model):
    _inherit = "nsp.measurement.session"

    pass_ids = fields.One2many(
        "nsp.measurement.pass", "session_id", string="Runs", copy=False,
    )
    pass_count = fields.Integer(compute="_compute_calibration_counts")
    accepted_pass_count = fields.Integer(compute="_compute_calibration_counts")
    result_ids = fields.One2many(
        "nsp.measurement.result", "session_id", string="Results", copy=False,
    )
    accepted_result_id = fields.Many2one(
        "nsp.measurement.result", compute="_compute_calibration_counts",
        string="Accepted Result",
    )
    validation_run_ids = fields.One2many(
        "nsp.measurement.validation.run", "session_id", string="Validation", copy=False,
    )
    validation_run_count = fields.Integer(compute="_compute_calibration_counts")
    calibration_workspace = fields.Boolean(compute="_compute_calibration_workspace")

    @api.depends("pass_ids.state", "result_ids.state", "result_ids.accepted_at", "validation_run_ids")
    def _compute_calibration_counts(self):
        Result = self.env["nsp.measurement.result"]
        for session in self:
            session.pass_count = len(session.pass_ids)
            session.accepted_pass_count = len(session.pass_ids.filtered(lambda item: item.state == "accepted"))
            accepted = session.result_ids.filtered(lambda item: item.state == "accepted").sorted(
                key=lambda item: (item.accepted_at or item.write_date or item.create_date, item.id),
                reverse=True,
            )[:1]
            session.accepted_result_id = accepted or Result.browse()
            session.validation_run_count = len(session.validation_run_ids)

    def _compute_calibration_workspace(self):
        for session in self:
            session.calibration_workspace = True

    def _validate_measurement_scope(self):
        # Lane Calibration may observe one or more configured Vehicles. The
        # operational Lane is built directly from selected Detection Timeline
        # rows; repeated reference runs are no longer required by the UI.
        return super()._validate_measurement_scope()

    def _allowed_target_tids(self):
        self.ensure_one()
        values = set(super()._allowed_target_tids())
        running = self.validation_run_ids.filtered(lambda item: item.state == "running")
        values.update(
            self.env["nsp.rfid.tag"]._normalize_tid(tid)
            for tid in running.mapped("vehicle_line_ids.vehicle_tid")
            if tid
        )
        return values

    def _sync_vehicle_targets(self):
        """Vehicle plus the active validation population."""
        self.ensure_one()
        rows = []
        seen = set()
        for target in self.target_line_ids:
            if not target.vehicle_tid or target.vehicle_tid in seen:
                continue
            seen.add(target.vehicle_tid)
            rows.append({
                "vehicle_tid": target.vehicle_tid,
                "vehicle": target.vehicle_id,
                "license_plate": target.license_plate or "",
            })
        for line in self.validation_run_ids.filtered(lambda run: run.state == "running").mapped("vehicle_line_ids"):
            if not line.vehicle_tid or line.vehicle_tid in seen:
                continue
            seen.add(line.vehicle_tid)
            rows.append({
                "vehicle_tid": line.vehicle_tid,
                "vehicle": line.vehicle_id,
                "license_plate": line.license_plate or "",
            })
        return rows

    def _reader_port_for_event(self, event):
        self.ensure_one()
        serial = str(event.serial_number or "").strip().upper()
        port_no = int(event.port_no or 0)
        for reader_line in self.reader_line_ids:
            if str(reader_line.reader_id.serial_number or "").strip().upper() != serial:
                continue
            reader_port = reader_line.reader_port_ids.filtered(
                lambda row: int(row.port_no or 0) == port_no
            )[:1]
            if reader_port:
                return reader_port
        return self.env["nsp.measurement.reader.port"]

    def _collapse_events_to_steps(self, events):
        """Return stable consecutive detection points from raw reads."""
        self.ensure_one()
        result = []
        current = None
        for event in events.sorted(key=lambda item: (_event_seconds(item), item.id)):
            reader_port = self._reader_port_for_event(event)
            if not reader_port:
                continue
            key = reader_port.id
            if current and current["reader_port_id"] == key:
                current["last_seconds"] = _event_seconds(event)
                current["last_read_at"] = event.read_at
                current["last_read_at_ms"] = int(event.read_at_ms or 0)
                current["read_count"] += 1
                continue
            current = {
                "reader_port_id": reader_port.id,
                "reader_line_id": reader_port.reader_line_id.id,
                "reader_id": reader_port.reader_line_id.reader_id.id,
                "reader_serial_number": reader_port.reader_line_id.reader_id.serial_number or "",
                "reader_code": reader_port.reader_line_id.reader_id.device_code or "",
                "port_no": int(reader_port.port_no or 0),
                "point_key": "%s:%s" % (
                    reader_port.reader_line_id.reader_id.device_code
                    or reader_port.reader_line_id.reader_id.serial_number
                    or reader_port.reader_line_id.reader_id.id,
                    int(reader_port.port_no or 0),
                ),
                "first_seconds": _event_seconds(event),
                "last_seconds": _event_seconds(event),
                "first_read_at": event.read_at,
                "first_read_at_ms": int(event.read_at_ms or 0),
                "last_read_at": event.read_at,
                "last_read_at_ms": int(event.read_at_ms or 0),
                "read_count": 1,
            }
            result.append(current)
        previous = None
        for index, row in enumerate(result, start=1):
            row["sequence"] = index
            row["duration_from_previous"] = (
                max(0.0, row["first_seconds"] - previous["first_seconds"])
                if previous else 0.0
            )
            previous = row
        return result

    def action_start_reference_pass(self):
        self.ensure_one()
        self._require_ready_configuration()
        if len(self.target_line_ids) != 1:
            raise ValidationError(_("Select exactly one Vehicle before starting a Pass."))
        if self.pass_ids.filtered(lambda item: item.state == "running"):
            raise ValidationError(_("A Run is already running."))
        if self.status == "ready":
            self.with_context(measurement_sync=True).write({
                "status": "running",
                "started_at": self.started_at or fields.Datetime.now(),
            })
        elif self.status != "running":
            raise ValidationError(
                _("Release the calibration or use Measure Again before starting a Run.")
            )
        target = self.target_line_ids[:1]
        next_no = max(self.pass_ids.mapped("pass_no") or [0]) + 1
        self.env["nsp.measurement.pass"].create({
            "session_id": self.id,
            "pass_no": next_no,
            "revision": self.revision,
            "vehicle_id": target.vehicle_id.id,
            "tag_id": target.tag_id.id,
            "started_at": fields.Datetime.now(),
            "state": "running",
        })
        return True

    def action_stop_reference_pass(self):
        self.ensure_one()
        running = self.pass_ids.filtered(lambda item: item.state == "running").sorted("id")[-1:]
        if not running:
            raise ValidationError(_("No Run is currently running."))
        running.action_stop_and_analyse()
        return True

    def action_build_calibration_result(self):
        self.ensure_one()
        accepted = self.pass_ids.filtered(
            lambda item: item.state == "accepted" and item.revision == self.revision
        ).sorted(key=lambda item: (item.pass_no, item.id))
        if not accepted:
            raise ValidationError(_("Accept at least one complete Run first."))
        expected_path = accepted[0].step_ids.sorted("sequence").mapped("reader_port_id").ids
        if len(expected_path) < 2:
            raise ValidationError(_("An accepted Run must contain at least two detection points."))
        for item in accepted[1:]:
            actual = item.step_ids.sorted("sequence").mapped("reader_port_id").ids
            if actual != expected_path:
                raise ValidationError(_(
                    "All accepted Runs must have the same Reader Port sequence. "
                    "Reject inconsistent Passes or measure again."
                ))
        values = []
        total_samples = len(accepted)
        cumulative = 0.0
        for position, mapping_id in enumerate(expected_path, start=1):
            mapping = self.env["nsp.measurement.reader.port"].browse(mapping_id)
            durations = []
            read_counts = []
            for item in accepted:
                step = item.step_ids.filtered(lambda row: row.sequence == position)[:1]
                durations.append(float(step.duration_from_previous or 0.0))
                read_counts.append(int(step.read_count or 0))
            standard = float(median(durations)) if position > 1 else 0.0
            cumulative += standard
            values.append((0, 0, {
                "sequence": position,
                "reader_port_id": mapping.id,
                "duration_standard": standard,
                "duration_min": min(durations) if durations else 0.0,
                "duration_average": sum(durations) / len(durations) if durations else 0.0,
                "duration_p95": _percentile(durations, 95),
                "duration_max": max(durations) if durations else 0.0,
                "cumulative_time": cumulative,
                "sample_count": total_samples,
                "average_read_count": sum(read_counts) / len(read_counts) if read_counts else 0.0,
            }))
        previous = self.result_ids.filtered(lambda item: item.state == "draft")
        if previous:
            previous.unlink()
        result = self.env["nsp.measurement.result"].create({
            "session_id": self.id,
            "revision": self.revision,
            "reference_vehicle_id": self.target_line_ids[:1].vehicle_id.id,
            "accepted_pass_count": total_samples,
            "tolerance_percent": 30.0,
            "line_ids": values,
        })
        return result.action_open_form()

    def _current_calibration_result(self):
        self.ensure_one()
        candidates = self.result_ids.filtered(
            lambda item: item.state == "validation"
        ).sorted(key=lambda item: (item.revision, item.id), reverse=True)
        return candidates[:1]

    def action_new_validation_run(self):
        self.ensure_one()
        result = self._current_calibration_result()
        if not result or result.state != "validation":
            raise ValidationError(
                _("Build a Result and submit it for Validation first.")
            )
        run = self.env["nsp.measurement.validation.run"].create({
            "session_id": self.id,
            "result_id": result.id,
            "name": new_management_code("VAL"),
        })
        return run.action_open_form()

    @api.model
    def get_calibration_workspace(self, session_id, validation_run_id=False):
        session = self.sudo().browse(int(session_id or 0)).exists()
        if not session:
            return {"found": False}
        accepted_result = session.accepted_result_id
        current_result = session.result_ids.filtered(
            lambda item: item.state in ("draft", "validation", "accepted")
        ).sorted(key=lambda item: (item.revision, item.id), reverse=True)[:1]
        runs = session.validation_run_ids.sorted(
            key=lambda item: (item.started_at or item.create_date, item.id), reverse=True
        )
        active_run = self.env["nsp.measurement.validation.run"]
        if validation_run_id:
            active_run = runs.filtered(lambda item: item.id == int(validation_run_id))[:1]
        if not active_run:
            active_run = runs[:1]
        return {
            "found": True,
            "session_id": session.id,
            "measurement_code": session.measurement_code,
            "revision": int(session.revision or 1),
            "status": session.status,
            "passes": [item._workspace_payload() for item in session.pass_ids.sorted(
                key=lambda row: (row.pass_no, row.id), reverse=True
            )],
            "running_pass_id": session.pass_ids.filtered(lambda item: item.state == "running")[:1].id or False,
            "accepted_result": accepted_result._workspace_payload() if accepted_result else False,
            "current_result": current_result._workspace_payload() if current_result else False,
            "draft_result": session.result_ids.filtered(lambda item: item.state == "draft")[:1]._workspace_payload()
            if session.result_ids.filtered(lambda item: item.state == "draft") else False,
            "validation_runs": [item._summary_payload() for item in runs],
            "active_validation_run": active_run._workspace_payload() if active_run else False,
        }


class NspMeasurementTargetLineReference(models.Model):
    _inherit = "nsp.measurement.target.line"

    @api.constrains("session_id")
    def _check_single_reference_vehicle(self):
        for line in self.filtered("session_id"):
            count = self.search_count([("session_id", "=", line.session_id.id)])
            if count > 1:
                raise ValidationError(
                    _("Lane Calibration allows exactly one Vehicle. Use Validation for multi-Vehicle testing.")
                )


class NspMeasurementPass(models.Model):
    _name = "nsp.measurement.pass"
    _description = "NSP Vehicle Calibration Pass"
    _order = "session_id, pass_no desc, id desc"

    session_id = fields.Many2one("nsp.measurement.session", required=True, ondelete="cascade", index=True)
    pass_no = fields.Integer(required=True, index=True)
    revision = fields.Integer(required=True, default=1, index=True)
    vehicle_id = fields.Many2one("nsp.vehicle", required=True, ondelete="restrict")
    tag_id = fields.Many2one("nsp.rfid.tag", required=True, ondelete="restrict")
    vehicle_tid = fields.Char(related="tag_id.tid", store=True, readonly=True)
    license_plate = fields.Char(related="vehicle_id.license_plate", store=True, readonly=True)
    started_at = fields.Datetime(required=True, index=True)
    ended_at = fields.Datetime(index=True)
    state = fields.Selection([
        ("running", "Running"),
        ("completed", "Completed"),
        ("accepted", "Accepted"),
        ("rejected", "Rejected"),
    ], required=True, default="running", index=True)
    result_status = fields.Selection([
        ("complete", "Complete"),
        ("insufficient", "Insufficient Detection"),
    ], readonly=True)
    detected_sequence = fields.Char(readonly=True)
    missing_or_error = fields.Char(readonly=True)
    total_duration = fields.Float(readonly=True, digits=(8, 3))
    step_ids = fields.One2many("nsp.measurement.pass.step", "pass_id", string="Pass Timeline", copy=False)
    step_count = fields.Integer(compute="_compute_step_count")

    _sql_constraints = [
        ("pass_no_unique", "unique(session_id, pass_no)", "Run number must be unique per calibration."),
    ]

    @api.depends("step_ids")
    def _compute_step_count(self):
        for record in self:
            record.step_count = len(record.step_ids)

    def action_stop_and_analyse(self):
        for record in self:
            if record.state != "running":
                raise ValidationError(_("Only a running Run can be stopped."))
            ended_at = fields.Datetime.now()
            events = self.env["nsp.measurement.event"].sudo().search([
                ("session_id", "=", record.session_id.id),
                ("revision", "=", record.revision),
                ("tid", "=", record.vehicle_tid),
                ("read_at", ">=", record.started_at),
                ("read_at", "<=", ended_at),
            ], order="read_at asc, read_at_ms asc, id asc")
            steps = record.session_id._collapse_events_to_steps(events)
            record.step_ids.unlink()
            commands = []
            for row in steps:
                commands.append((0, 0, {
                    "sequence": row["sequence"],
                    "reader_port_id": row["reader_port_id"],
                    "first_read_at": row["first_read_at"],
                    "first_read_at_ms": row["first_read_at_ms"],
                    "last_read_at": row["last_read_at"],
                    "last_read_at_ms": row["last_read_at_ms"],
                    "read_count": row["read_count"],
                    "duration_from_previous": row["duration_from_previous"],
                }))
            sequence = " → ".join(row["point_key"] for row in steps)
            record.write({
                "ended_at": ended_at,
                "state": "completed",
                "result_status": "complete" if len(steps) >= 2 else "insufficient",
                "detected_sequence": sequence,
                "missing_or_error": "" if len(steps) >= 2 else _("At least two detection points are required."),
                "total_duration": sum(float(row["duration_from_previous"] or 0.0) for row in steps),
                "step_ids": commands,
            })
        return True

    def action_accept(self):
        for record in self:
            if record.state not in ("completed", "accepted") or record.result_status != "complete":
                raise ValidationError(_("Only a complete Run can be accepted."))
            record.state = "accepted"
        return True

    def action_reject(self):
        self.filtered(lambda item: item.state != "running").write({"state": "rejected"})
        return True

    def _workspace_payload(self):
        self.ensure_one()
        return {
            "id": self.id,
            "pass_no": self.pass_no,
            "revision": self.revision,
            "license_plate": self.license_plate or "",
            "state": self.state,
            "result_status": self.result_status or "",
            "detected_sequence": self.detected_sequence or "",
            "missing_or_error": self.missing_or_error or "",
            "total_duration": round(float(self.total_duration or 0.0), 3),
            "started_at": fields.Datetime.to_string(self.started_at) if self.started_at else None,
            "ended_at": fields.Datetime.to_string(self.ended_at) if self.ended_at else None,
            "steps": [item._workspace_payload() for item in self.step_ids.sorted("sequence")],
        }


class NspMeasurementPassStep(models.Model):
    _name = "nsp.measurement.pass.step"
    _description = "NSP Run Timeline Step"
    _order = "pass_id, sequence, id"

    pass_id = fields.Many2one("nsp.measurement.pass", required=True, ondelete="cascade", index=True)
    sequence = fields.Integer(required=True)
    reader_port_id = fields.Many2one("nsp.measurement.reader.port", required=True, ondelete="restrict")
    reader_id = fields.Many2one(related="reader_port_id.reader_line_id.reader_id", store=True, readonly=True)
    port_no = fields.Integer(related="reader_port_id.port_no", store=True, readonly=True)
    first_read_at = fields.Datetime(required=True)
    first_read_at_ms = fields.Integer(default=0)
    last_read_at = fields.Datetime(required=True)
    last_read_at_ms = fields.Integer(default=0)
    read_count = fields.Integer(default=1)
    duration_from_previous = fields.Float(digits=(8, 3), default=0.0)

    def _workspace_payload(self):
        self.ensure_one()
        return {
            "sequence": self.sequence,
            "reader_code": self.reader_id.device_code or "",
            "reader": self.reader_id.serial_number or self.reader_id.display_name or "",
            "port_no": self.port_no,
            "duration_from_previous": round(float(self.duration_from_previous or 0.0), 3),
            "read_count": self.read_count,
            "first_read_at": fields.Datetime.to_string(self.first_read_at),
        }


class NspMeasurementResult(models.Model):
    _name = "nsp.measurement.result"
    _description = "NSP Accepted Result"
    _order = "session_id, revision desc, id desc"

    name = fields.Char(default=lambda self: new_management_code("CAL"), required=True, readonly=True, copy=False)
    session_id = fields.Many2one("nsp.measurement.session", required=True, ondelete="cascade", index=True)
    revision = fields.Integer(required=True, default=1, index=True)
    reference_vehicle_id = fields.Many2one("nsp.vehicle", required=True, ondelete="restrict")
    state = fields.Selection([
        ("draft", "Draft"),
        ("validation", "Ready for Validation"),
        ("accepted", "Accepted"),
        ("superseded", "Superseded"),
    ], default="draft", required=True, index=True)
    validation_state = fields.Selection([
        ("pending", "Pending"), ("passed", "Passed"), ("failed", "Failed"),
    ], default="pending", required=True, readonly=True, index=True)
    validated_run_id = fields.Many2one(
        "nsp.measurement.validation.run", string="Validated By Run", readonly=True, copy=False,
    )
    validated_at = fields.Datetime(readonly=True, copy=False)
    accepted_pass_count = fields.Integer(readonly=True)
    accepted_at = fields.Datetime(readonly=True)
    accepted_by_id = fields.Many2one("res.users", readonly=True)
    tolerance_percent = fields.Float(default=30.0, required=True)
    line_ids = fields.One2many("nsp.measurement.result.line", "result_id", string="Accepted Timeline")
    total_duration = fields.Float(compute="_compute_total_duration", digits=(8, 3))
    path_display = fields.Char(compute="_compute_total_duration")

    @api.depends("line_ids.duration_standard", "line_ids.reader_id", "line_ids.port_no")
    def _compute_total_duration(self):
        for record in self:
            lines = record.line_ids.sorted("sequence")
            record.total_duration = sum(lines.mapped("duration_standard"))
            record.path_display = " → ".join(
                "%s:%s" % (
                    line.reader_id.device_code or line.reader_id.serial_number or line.reader_id.id,
                    line.port_no,
                )
                for line in lines
            )

    def action_submit_validation(self):
        for record in self:
            if len(record.line_ids) < 2:
                raise ValidationError(_("Result requires at least two Timeline points."))
            if record.state not in ("draft", "validation"):
                raise ValidationError(_("Only a Draft Result can be submitted for Validation."))
            record.write({"state": "validation", "validation_state": "pending"})
        return True

    def action_accept(self):
        for record in self:
            if record.state != "validation":
                raise ValidationError(_("Only a Result in Validation can be published as Accepted."))
            if record.validation_state != "passed" or not record.validated_run_id:
                raise ValidationError(_("A passed multi-Vehicle Validation Run is required before publishing the Result."))
            previous = record.session_id.result_ids.filtered(
                lambda item: item.state == "accepted" and item != record
            )
            previous.write({"state": "superseded"})
            record.write({
                "state": "accepted",
                "accepted_at": fields.Datetime.now(),
                "accepted_by_id": self.env.user.id,
            })
            record.session_id.with_context(measurement_sync=True).write({
                "status": "completed",
                "ended_at": record.session_id.ended_at or fields.Datetime.now(),
            })
        return True

    def action_open_form(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Result"),
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "current",
        }

    def _workspace_payload(self):
        self.ensure_one()
        return {
            "id": self.id,
            "name": self.name,
            "revision": self.revision,
            "state": self.state,
            "path_display": self.path_display or "",
            "total_duration": round(float(self.total_duration or 0.0), 3),
            "accepted_pass_count": self.accepted_pass_count,
            "tolerance_percent": float(self.tolerance_percent or 0.0),
            "accepted_at": fields.Datetime.to_string(self.accepted_at) if self.accepted_at else None,
            "validation_state": self.validation_state or "pending",
            "validated_run_id": self.validated_run_id.id if self.validated_run_id else False,
            "validated_run_name": self.validated_run_id.name if self.validated_run_id else "",
            "validated_at": fields.Datetime.to_string(self.validated_at) if self.validated_at else None,
            "lines": [line._workspace_payload() for line in self.line_ids.sorted("sequence")],
        }


class NspMeasurementResultLine(models.Model):
    _name = "nsp.measurement.result.line"
    _description = "NSP Result Timeline Line"
    _order = "result_id, sequence, id"

    result_id = fields.Many2one("nsp.measurement.result", required=True, ondelete="cascade", index=True)
    sequence = fields.Integer(required=True)
    reader_port_id = fields.Many2one("nsp.measurement.reader.port", required=True, ondelete="restrict")
    reader_id = fields.Many2one(related="reader_port_id.reader_line_id.reader_id", store=True, readonly=True)
    port_no = fields.Integer(related="reader_port_id.port_no", store=True, readonly=True)
    duration_standard = fields.Float(digits=(8, 3), default=0.0)
    duration_min = fields.Float(digits=(8, 3))
    duration_average = fields.Float(digits=(8, 3))
    duration_p95 = fields.Float(digits=(8, 3))
    duration_max = fields.Float(digits=(8, 3))
    cumulative_time = fields.Float(digits=(8, 3))
    sample_count = fields.Integer()
    average_read_count = fields.Float(digits=(8, 2))

    def _workspace_payload(self):
        self.ensure_one()
        return {
            "sequence": self.sequence,
            "reader_code": self.reader_id.device_code or "",
            "reader": self.reader_id.serial_number or self.reader_id.display_name or "",
            "port_no": self.port_no,
            "duration_standard": round(float(self.duration_standard or 0.0), 3),
            "duration_min": round(float(self.duration_min or 0.0), 3),
            "duration_average": round(float(self.duration_average or 0.0), 3),
            "duration_p95": round(float(self.duration_p95 or 0.0), 3),
            "duration_max": round(float(self.duration_max or 0.0), 3),
            "cumulative_time": round(float(self.cumulative_time or 0.0), 3),
            "sample_count": self.sample_count,
        }


class NspMeasurementValidationRun(models.Model):
    _name = "nsp.measurement.validation.run"
    _description = "NSP Multi-Vehicle Calibration Validation Run"
    _order = "session_id, create_date desc, id desc"

    name = fields.Char(required=True, default=lambda self: new_management_code("VAL"), copy=False)
    session_id = fields.Many2one("nsp.measurement.session", required=True, ondelete="cascade", index=True)
    result_id = fields.Many2one("nsp.measurement.result", required=True, ondelete="restrict", index=True)
    revision = fields.Integer(related="result_id.revision", store=True, readonly=True)
    measurement_revision = fields.Integer(readonly=True, copy=False)
    test_mode = fields.Selection([
        ("sequential", "Sequential Vehicle Test"),
        ("concurrent", "Concurrent Tag Load Test"),
    ], default="sequential", required=True)
    state = fields.Selection([
        ("draft", "Draft"), ("running", "Running"), ("completed", "Completed"),
        ("passed", "Passed"), ("failed", "Failed"),
    ], default="draft", required=True, index=True)
    started_at = fields.Datetime(readonly=True)
    ended_at = fields.Datetime(readonly=True)
    planned_vehicle_count = fields.Integer(string="Vehicles", default=100, required=True)
    minimum_complete_rate = fields.Float(default=98.0, required=True)
    minimum_port_rate = fields.Float(default=98.0, required=True)
    maximum_wrong_order_rate = fields.Float(default=1.0, required=True)
    vehicle_line_ids = fields.One2many("nsp.measurement.validation.vehicle", "run_id", string="Vehicles")
    port_stat_ids = fields.One2many("nsp.measurement.validation.port.stat", "run_id", readonly=True)
    transition_stat_ids = fields.One2many("nsp.measurement.validation.transition.stat", "run_id", readonly=True)
    expected_count = fields.Integer(compute="_compute_counts", store=True)
    complete_count = fields.Integer(compute="_compute_counts", store=True)
    incomplete_count = fields.Integer(compute="_compute_counts", store=True)
    not_detected_count = fields.Integer(compute="_compute_counts", store=True)
    wrong_order_count = fields.Integer(compute="_compute_counts", store=True)
    timeout_count = fields.Integer(compute="_compute_counts", store=True)
    complete_rate = fields.Float(compute="_compute_counts", store=True, digits=(6, 2))
    wrong_order_rate = fields.Float(compute="_compute_counts", store=True, digits=(6, 2))
    recommendation = fields.Text(readonly=True)

    @api.constrains("planned_vehicle_count", "minimum_complete_rate", "minimum_port_rate", "maximum_wrong_order_rate")
    def _check_validation_limits(self):
        for run in self:
            if run.planned_vehicle_count <= 0 or run.planned_vehicle_count > 5000:
                raise ValidationError(_("Vehicles must be between 1 and 5000."))
            if not (0.0 <= run.minimum_complete_rate <= 100.0):
                raise ValidationError(_("Minimum Complete Rate must be between 0 and 100."))
            if not (0.0 <= run.minimum_port_rate <= 100.0):
                raise ValidationError(_("Minimum Reader Port Rate must be between 0 and 100."))
            if not (0.0 <= run.maximum_wrong_order_rate <= 100.0):
                raise ValidationError(_("Maximum Wrong Order Rate must be between 0 and 100."))

    @api.depends("vehicle_line_ids.result")
    def _compute_counts(self):
        for run in self:
            run.expected_count = len(run.vehicle_line_ids)
            run.complete_count = len(run.vehicle_line_ids.filtered(lambda row: row.result == "complete"))
            run.incomplete_count = len(run.vehicle_line_ids.filtered(lambda row: row.result in ("incomplete", "transition_timeout")))
            run.not_detected_count = len(run.vehicle_line_ids.filtered(lambda row: row.result == "not_detected"))
            run.wrong_order_count = len(run.vehicle_line_ids.filtered(lambda row: row.result == "wrong_order"))
            run.timeout_count = len(run.vehicle_line_ids.filtered(lambda row: row.result == "transition_timeout"))
            run.complete_rate = (run.complete_count * 100.0 / run.expected_count) if run.expected_count else 0.0
            run.wrong_order_rate = (run.wrong_order_count * 100.0 / run.expected_count) if run.expected_count else 0.0

    def action_load_active_vehicles(self):
        for run in self:
            if run.state != "draft":
                raise ValidationError(_("Vehicles can be loaded only while the Validation Run is Draft."))
            limit = max(1, min(int(run.planned_vehicle_count or 100), 5000))
            assignments = self.env["nsp.rfid.tag.assignment"].sudo().search([
                ("state", "=", "active"),
                ("vehicle_id", "!=", False),
                ("vehicle_id.active", "=", True),
            ], order="vehicle_id, id", limit=limit)
            if not assignments:
                raise ValidationError(_("No active Vehicle RFID assignments are available."))
            if len(assignments) < limit:
                raise ValidationError(
                    _("Only %(available)s active Vehicle RFID assignments are available; %(planned)s are required for this Run.")
                    % {"available": len(assignments), "planned": limit}
                )
            run.vehicle_line_ids.unlink()
            run.write({
                "vehicle_line_ids": [(0, 0, {
                    "vehicle_id": assignment.vehicle_id.id,
                    "tag_id": assignment.tag_id.id,
                }) for assignment in assignments]
            })
        return True

    def action_start(self):
        for run in self:
            if run.state != "draft":
                raise ValidationError(_("Only a Draft Validation Run can be started."))
            if run.result_id.state != "validation":
                raise ValidationError(_("Validation requires a Result submitted for Validation."))
            if not run.vehicle_line_ids:
                raise ValidationError(_("Add at least one Vehicle to the Validation Run."))
            if len(run.vehicle_line_ids) != int(run.planned_vehicle_count or 0):
                raise ValidationError(
                    _("Validation Run requires exactly %(planned)s Vehicles; currently configured: %(actual)s.")
                    % {"planned": run.planned_vehicle_count, "actual": len(run.vehicle_line_ids)}
                )
            run.vehicle_line_ids.write({
                "result": "pending", "detected_sequence": False, "missing_or_error": False,
                "total_duration": 0.0, "detected_at": False, "actual_timeline_json": False,
            })
            next_revision = (
                int(run.session_id.revision or 1)
                if run.session_id.status == "ready"
                else int(run.session_id.revision or 1) + 1
            )
            started_at = fields.Datetime.now()
            run.write({
                "state": "running", "started_at": started_at, "ended_at": False,
                "measurement_revision": next_revision,
            })
            run.session_id.with_context(measurement_sync=True).write({
                "revision": next_revision,
                "status": "ready",
                "started_at": started_at,
                "ended_at": False,
            })
        return True

    def action_stop_and_analyse(self):
        for run in self:
            if run.state != "running":
                raise ValidationError(_("Only a running Validation Run can be completed."))
            ended_at = fields.Datetime.now()
            run._analyse(ended_at)
            run.write({"ended_at": ended_at})
            minimum_port = min(run.port_stat_ids.mapped("detection_rate") or [100.0])
            passed = (
                run.complete_rate >= run.minimum_complete_rate
                and minimum_port >= run.minimum_port_rate
                and run.wrong_order_rate <= run.maximum_wrong_order_rate
            )
            recommendations = []
            for stat in run.port_stat_ids.filtered(lambda row: row.detection_rate < run.minimum_port_rate):
                recommendations.append(_(
                    "%(port)s detection rate is %(rate).1f%%. Review Reader power, Reader position, or port configuration."
                ) % {"port": stat.display_name, "rate": stat.detection_rate})
            if run.not_detected_count:
                recommendations.append(_("Re-test %(count)s Vehicles with no RFID detection.") % {"count": run.not_detected_count})
            if run.incomplete_count:
                recommendations.append(_("Re-test %(count)s Vehicles with incomplete or timed-out paths.") % {"count": run.incomplete_count})
            final_state = "passed" if passed else "failed"
            run.write({
                "state": final_state,
                "recommendation": "\n".join(recommendations) or _("Validation meets all configured acceptance criteria."),
            })
            run.result_id.write({
                "validation_state": final_state,
                "validated_run_id": run.id,
                "validated_at": ended_at,
            })
            run.session_id.with_context(measurement_sync=True).write({
                "status": "completed",
                "ended_at": ended_at,
            })
        return True

    def _analyse(self, ended_at):
        self.ensure_one()
        result_lines = self.result_id.line_ids.sorted("sequence")
        expected_ids = result_lines.mapped("reader_port_id").ids
        expected_set = set(expected_ids)
        labels = {
            line.reader_port_id.id: "%s:%s" % (
                line.reader_id.device_code or line.reader_id.serial_number or line.reader_id.id,
                line.port_no,
            )
            for line in result_lines
        }
        tolerance = float(self.result_id.tolerance_percent or 0.0) / 100.0
        transition_samples = {index: [] for index in range(1, len(result_lines))}
        transition_timeout = {index: 0 for index in range(1, len(result_lines))}
        port_detected = {reader_port_id: 0 for reader_port_id in expected_ids}

        for vehicle_line in self.vehicle_line_ids:
            events = self.env["nsp.measurement.event"].sudo().search([
                ("session_id", "=", self.session_id.id),
                ("revision", "=", int(self.measurement_revision or self.session_id.revision or 1)),
                ("tid", "=", vehicle_line.vehicle_tid),
                ("read_at", ">=", self.started_at),
                ("read_at", "<=", ended_at),
            ], order="read_at asc, read_at_ms asc, id asc")
            steps = self.session_id._collapse_events_to_steps(events)
            actual_ids = [row["reader_port_id"] for row in steps]
            for reader_port_id in expected_set.intersection(actual_ids):
                port_detected[reader_port_id] += 1
            result = "complete"
            error = ""
            if not actual_ids:
                result = "not_detected"
                error = _("No Read")
            elif actual_ids == expected_ids:
                timed_out = False
                for index in range(1, len(result_lines)):
                    actual_duration = float(steps[index]["duration_from_previous"] or 0.0)
                    transition_samples[index].append(actual_duration)
                    standard = float(result_lines[index].duration_standard or 0.0)
                    allowed = standard * (1.0 + tolerance)
                    if standard > 0 and actual_duration > allowed:
                        timed_out = True
                        transition_timeout[index] += 1
                if timed_out:
                    result = "transition_timeout"
                    error = _("Transition exceeded calibrated time tolerance")
            elif set(actual_ids) == expected_set:
                result = "wrong_order"
                error = _("Sequence Mismatch")
            else:
                result = "incomplete"
                missing = [labels[item] for item in expected_ids if item not in actual_ids]
                error = _("Missing: %s") % ", ".join(missing)
            total_duration = sum(float(row["duration_from_previous"] or 0.0) for row in steps)
            actual_labels = {
                row["reader_port_id"]: row["point_key"]
                for row in steps
            }
            vehicle_line.write({
                "result": result,
                "detected_sequence": " → ".join(
                    labels.get(item) or actual_labels.get(item) or str(item)
                    for item in actual_ids
                ),
                "missing_or_error": error,
                "total_duration": total_duration,
                "detected_at": steps[0]["first_read_at"] if steps else False,
                "actual_timeline_json": json.dumps(steps, default=str, ensure_ascii=False),
            })

        self.port_stat_ids.unlink()
        expected_count = len(self.vehicle_line_ids)
        port_commands = []
        for result_line in result_lines:
            reader_port_id = result_line.reader_port_id.id
            detected = int(port_detected.get(reader_port_id, 0))
            port_commands.append((0, 0, {
                "reader_port_id": reader_port_id,
                "expected_count": expected_count,
                "detected_count": detected,
                "missed_count": max(0, expected_count - detected),
                "detection_rate": (detected * 100.0 / expected_count) if expected_count else 0.0,
            }))
        self.write({"port_stat_ids": port_commands})

        self.transition_stat_ids.unlink()
        transition_commands = []
        for index in range(1, len(result_lines)):
            values = transition_samples[index]
            transition_commands.append((0, 0, {
                "from_reader_port_id": result_lines[index - 1].reader_port_id.id,
                "to_reader_port_id": result_lines[index].reader_port_id.id,
                "sample_count": len(values),
                "duration_min": min(values) if values else 0.0,
                "duration_median": median(values) if values else 0.0,
                "duration_average": sum(values) / len(values) if values else 0.0,
                "duration_p95": _percentile(values, 95),
                "duration_max": max(values) if values else 0.0,
                "timeout_count": transition_timeout[index],
            }))
        self.write({"transition_stat_ids": transition_commands})

    def action_retest_failed(self):
        self.ensure_one()
        failed = self.vehicle_line_ids.filtered(lambda row: row.result not in ("complete", "pending"))
        if not failed:
            raise ValidationError(_("There are no failed Vehicles to re-test."))
        return self._copy_for_retest(failed)

    def action_retest_selected(self):
        self.ensure_one()
        selected = self.vehicle_line_ids.filtered("retry_selected")
        if not selected:
            raise ValidationError(_("Select at least one Vehicle for re-test."))
        return self._copy_for_retest(selected)

    def action_new_run_all(self):
        self.ensure_one()
        return self._copy_for_retest(self.vehicle_line_ids)

    def _copy_for_retest(self, lines):
        new_run = self.create({
            "session_id": self.session_id.id,
            "result_id": self.result_id.id,
            "name": new_management_code("VAL"),
            "test_mode": self.test_mode,
            "minimum_complete_rate": self.minimum_complete_rate,
            "minimum_port_rate": self.minimum_port_rate,
            "maximum_wrong_order_rate": self.maximum_wrong_order_rate,
            "planned_vehicle_count": len(lines),
            "vehicle_line_ids": [(0, 0, {
                "vehicle_id": line.vehicle_id.id,
                "tag_id": line.tag_id.id,
            }) for line in lines],
        })
        return new_run.action_open_form()

    def action_open_form(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Validation Run"),
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "current",
        }

    def _summary_payload(self):
        self.ensure_one()
        return {
            "id": self.id, "name": self.name, "state": self.state,
            "expected_count": self.expected_count, "complete_rate": round(self.complete_rate, 2),
            "started_at": fields.Datetime.to_string(self.started_at) if self.started_at else None,
        }

    def _workspace_payload(self):
        self.ensure_one()
        return {
            **self._summary_payload(),
            "test_mode": self.test_mode,
            "measurement_revision": int(self.measurement_revision or 0),
            "started_at": fields.Datetime.to_string(self.started_at) if self.started_at else None,
            "ended_at": fields.Datetime.to_string(self.ended_at) if self.ended_at else None,
            "complete_count": self.complete_count,
            "incomplete_count": self.incomplete_count,
            "not_detected_count": self.not_detected_count,
            "wrong_order_count": self.wrong_order_count,
            "timeout_count": self.timeout_count,
            "wrong_order_rate": round(self.wrong_order_rate, 2),
            "minimum_complete_rate": self.minimum_complete_rate,
            "minimum_port_rate": self.minimum_port_rate,
            "maximum_wrong_order_rate": self.maximum_wrong_order_rate,
            "recommendation": self.recommendation or "",
            "vehicles": [line._workspace_payload() for line in self.vehicle_line_ids.sorted(
                key=lambda row: (row.license_plate or "", row.id)
            )],
            "port_stats": [line._workspace_payload() for line in self.port_stat_ids],
            "transition_stats": [line._workspace_payload() for line in self.transition_stat_ids],
        }


class NspMeasurementValidationVehicle(models.Model):
    _name = "nsp.measurement.validation.vehicle"
    _description = "NSP Validation Vehicle Result"
    _order = "run_id, license_plate, id"

    run_id = fields.Many2one("nsp.measurement.validation.run", required=True, ondelete="cascade", index=True)
    vehicle_id = fields.Many2one("nsp.vehicle", required=True, ondelete="restrict", domain=[("active", "=", True)])
    tag_id = fields.Many2one("nsp.rfid.tag", required=True, ondelete="restrict")
    license_plate = fields.Char(related="vehicle_id.license_plate", store=True, readonly=True)
    vehicle_tid = fields.Char(related="tag_id.tid", store=True, readonly=True)
    result = fields.Selection([
        ("pending", "Pending"), ("complete", "Complete"), ("incomplete", "Incomplete"),
        ("not_detected", "Not Detected"), ("wrong_order", "Wrong Order"),
        ("transition_timeout", "Transition Timeout"),
    ], default="pending", required=True, index=True)
    detected_sequence = fields.Char(readonly=True)
    missing_or_error = fields.Char(readonly=True)
    total_duration = fields.Float(readonly=True, digits=(8, 3))
    detected_at = fields.Datetime(readonly=True)
    actual_timeline_json = fields.Text(readonly=True)
    retry_selected = fields.Boolean(string="Re-test")

    _sql_constraints = [
        ("validation_vehicle_unique", "unique(run_id, vehicle_id)", "A Vehicle can appear only once in a Validation Run."),
        ("validation_tag_unique", "unique(run_id, tag_id)", "An RFID Tag can appear only once in a Validation Run."),
    ]

    @api.onchange("vehicle_id")
    def _onchange_vehicle_id(self):
        for record in self:
            if not record.vehicle_id:
                record.tag_id = False
                continue
            assignment = self.env["nsp.rfid.tag.assignment"].sudo().search([
                ("state", "=", "active"), ("vehicle_id", "=", record.vehicle_id.id),
            ], limit=1)
            record.tag_id = assignment.tag_id if assignment else False

    @api.constrains("vehicle_id", "tag_id")
    def _check_assignment(self):
        for record in self:
            assignment = self.env["nsp.rfid.tag.assignment"].sudo().search([
                ("state", "=", "active"),
                ("vehicle_id", "=", record.vehicle_id.id),
                ("tag_id", "=", record.tag_id.id),
            ], limit=1)
            if not assignment:
                raise ValidationError(_("Validation Vehicle must use its active Vehicle RFID Tag."))

    def _workspace_payload(self):
        self.ensure_one()
        return {
            "id": self.id,
            "license_plate": self.license_plate or "",
            "vehicle_tid": self.vehicle_tid or "",
            "detected_sequence": self.detected_sequence or "",
            "result": self.result,
            "missing_or_error": self.missing_or_error or "",
            "total_duration": round(float(self.total_duration or 0.0), 3),
            "detected_at": fields.Datetime.to_string(self.detected_at) if self.detected_at else None,
            "retry_selected": bool(self.retry_selected),
        }


class NspMeasurementValidationPortStat(models.Model):
    _name = "nsp.measurement.validation.port.stat"
    _description = "NSP Validation Reader Port Statistics"
    _order = "run_id, id"
    _rec_name = "display_name"

    run_id = fields.Many2one("nsp.measurement.validation.run", required=True, ondelete="cascade", index=True)
    reader_port_id = fields.Many2one("nsp.measurement.reader.port", required=True, ondelete="restrict")
    reader_id = fields.Many2one(related="reader_port_id.reader_line_id.reader_id", store=True, readonly=True)
    port_no = fields.Integer(related="reader_port_id.port_no", store=True, readonly=True)
    display_name = fields.Char(compute="_compute_display_name")
    expected_count = fields.Integer()
    detected_count = fields.Integer()
    missed_count = fields.Integer()
    detection_rate = fields.Float(digits=(6, 2))

    @api.depends("reader_id", "port_no")
    def _compute_display_name(self):
        for record in self:
            record.display_name = "%s:%s" % (
                record.reader_id.device_code or record.reader_id.serial_number or record.reader_id.id,
                record.port_no,
            )

    def _workspace_payload(self):
        self.ensure_one()
        return {
            "reader_code": self.reader_id.device_code or "",
            "reader_serial_number": self.reader_id.serial_number or "",
            "port_no": self.port_no,
            "expected_count": self.expected_count,
            "detected_count": self.detected_count,
            "missed_count": self.missed_count,
            "detection_rate": round(float(self.detection_rate or 0.0), 2),
        }


class NspMeasurementValidationTransitionStat(models.Model):
    _name = "nsp.measurement.validation.transition.stat"
    _description = "NSP Validation Transition Time Statistics"
    _order = "run_id, id"

    run_id = fields.Many2one("nsp.measurement.validation.run", required=True, ondelete="cascade", index=True)
    from_reader_port_id = fields.Many2one("nsp.measurement.reader.port", required=True, ondelete="restrict")
    to_reader_port_id = fields.Many2one("nsp.measurement.reader.port", required=True, ondelete="restrict")
    sample_count = fields.Integer()
    duration_min = fields.Float(digits=(8, 3))
    duration_median = fields.Float(digits=(8, 3))
    duration_average = fields.Float(digits=(8, 3))
    duration_p95 = fields.Float(digits=(8, 3))
    duration_max = fields.Float(digits=(8, 3))
    timeout_count = fields.Integer()

    def _workspace_payload(self):
        self.ensure_one()
        source_reader = self.from_reader_port_id.reader_line_id.reader_id
        target_reader = self.to_reader_port_id.reader_line_id.reader_id
        return {
            "transition": "%s:%s → %s:%s" % (
                source_reader.device_code or source_reader.serial_number or source_reader.id,
                self.from_reader_port_id.port_no,
                target_reader.device_code or target_reader.serial_number or target_reader.id,
                self.to_reader_port_id.port_no,
            ),
            "sample_count": self.sample_count,
            "duration_min": round(float(self.duration_min or 0.0), 3),
            "duration_median": round(float(self.duration_median or 0.0), 3),
            "duration_average": round(float(self.duration_average or 0.0), 3),
            "duration_p95": round(float(self.duration_p95 or 0.0), 3),
            "duration_max": round(float(self.duration_max or 0.0), 3),
            "timeout_count": self.timeout_count,
        }
