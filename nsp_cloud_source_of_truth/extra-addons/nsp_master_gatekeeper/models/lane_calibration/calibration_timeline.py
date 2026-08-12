# -*- coding: utf-8 -*-
import math
from statistics import median

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError
from odoo.addons.nsp_core.utils import new_management_code

from ...services.calibration_timeline_builder import CalibrationTimelineBuilder


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

class NspMeasurementSessionTimeline(models.Model):
    _inherit = "nsp.measurement.session"

    def _event_timestamp(self, event):
        self.ensure_one()
        if not event.read_at:
            return None
        base = fields.Datetime.to_string(event.read_at).replace(" ", "T")
        return "%s.%03dZ" % (base, int(event.read_at_ms or 0))

    def _event_seconds(self, event):
        self.ensure_one()
        observed_at = fields.Datetime.to_datetime(event.read_at)
        if not observed_at:
            return 0.0
        return observed_at.timestamp() + (int(event.read_at_ms or 0) / 1000.0)

    def _build_detection_steps(self, events):
        self.ensure_one()
        readers_by_serial = {}
        for node in self._reader_nodes():
            controller_node = node.parent_id if node.parent_id.device_type == "controller" else False
            readers_by_serial[(node.reader_id.serial_number or "").strip().upper()] = {
                "controller_code": (
                    controller_node.controller_id.controller_id or ""
                    if controller_node and controller_node.controller_id else ""
                ),
                "reader_name": node.reader_id.name or node.reader_id.serial_number or "",
            }
        event_values = []
        for event in events:
            event_values.append({
                "id": event.id,
                "tid": event.tid,
                "serial_number": event.serial_number,
                "port_no": int(event.port_no or 0),
                "timestamp": self._event_timestamp(event),
                "observed_seconds": self._event_seconds(event),
                "rssi_dbm": False if event.rssi_dbm in (False, None) else float(event.rssi_dbm),
            })
        return CalibrationTimelineBuilder.build(
            event_values,
            readers_by_serial=readers_by_serial,
        )

    def _port_summary(self):
        self.ensure_one()
        rows = self.env["nsp.measurement.event"].sudo()._read_group(
            [("session_id", "=", self.id), ("revision", "=", self.revision)],
            ["tid", "serial_number", "port_no"],
            ["__count", "read_at:min", "read_at:max"],
            order="tid, serial_number, port_no",
        )
        return [
            {
                "tid": tid,
                "serial_number": serial_number,
                "port_no": int(port_no or 0),
                "read_count": int(count or 0),
                "first_read_at": first_read,
                "last_read_at": last_read,
            }
            for tid, serial_number, port_no, count, first_read, last_read in rows
        ]

    def _clear_detection_timeline(self):
        self.check_access("write")
        for session in self:
            events = session.event_ids.filtered(lambda row: row.revision == session.revision)
            # Keep sync receipts intact so an already acknowledged Edge event
            # cannot be replayed into the freshly cleared timeline.
            count = len(events)
            events.sudo().unlink()
            session.message_post(
                body=_("Detection Timeline cleared (%(count)s raw observations removed).")
                % {"count": count}
            )
        return True

    def _configuration_steps_from_selection(self, selected_event_ids):
        self.ensure_one()
        try:
            ordered_ids = [int(value) for value in (selected_event_ids or [])]
        except (TypeError, ValueError) as exc:
            raise ValidationError(_("Invalid Detection Timeline selection.")) from exc
        ordered_ids = list(dict.fromkeys(value for value in ordered_ids if value > 0))
        if len(ordered_ids) < 2:
            raise ValidationError(_("Select at least two Detection Timeline rows."))

        events = self.env["nsp.measurement.event"].sudo().search([
            ("session_id", "=", self.id),
            ("revision", "=", self.revision),
        ], order="read_at asc, read_at_ms asc, id asc")
        steps = self._build_detection_steps(events)
        step_by_event = {
            int(step.get("first_event_id") or 0): step
            for step in steps
            if step.get("first_event_id")
        }
        event_by_id = {event.id: event for event in events}

        selected = []
        point_keys = set()
        controller_ids = set()
        edge_ids = set()
        previous_seconds = None
        for event_id in ordered_ids:
            step = step_by_event.get(event_id)
            event = event_by_id.get(event_id)
            if not step or not event:
                raise ValidationError(_(
                    "A Detection Timeline row is no longer available. Refresh and try again."
                ))
            reader_node = self._measurement_node_for_serial(step.get("serial_number"))
            if not reader_node:
                raise ValidationError(_("A detection does not belong to the current Infrastructure Scope."))
            reader = reader_node.reader_id
            port_no = int(step.get("port_no") or 0)
            point_key = (reader.id, port_no)

            # Detection Timeline is observation data. With overlapping antenna coverage,
            # the same Reader/Port can naturally appear again later in the raw path.
            # A Lane configuration, however, stores each physical Reader/Port once.
            # Preserve the first chronological occurrence and ignore later repeats.
            if point_key in point_keys:
                continue

            point_keys.add(point_key)
            controller_node = reader_node.parent_id
            server_node = controller_node.parent_id if controller_node else False
            if (
                not controller_node
                or controller_node.device_type != "controller"
                or not controller_node.controller_id
                or not server_node
                or server_node.device_type != "server"
                or not server_node.server_id
            ):
                raise ValidationError(_("A detection belongs to an unassigned Device Tree Reader."))
            controller_ids.add(controller_node.controller_id.id)
            edge_ids.add(server_node.server_id.id)
            current_seconds = self._event_seconds(event)
            duration = 0.0 if previous_seconds is None else max(abs(current_seconds - previous_seconds), 0.001)
            previous_seconds = current_seconds
            selection_order = len(selected) + 1
            selected.append({
                "selection_order": selection_order,
                "event_id": event.id,
                "reader_id": reader.id,
                "reader_name": reader.name or reader.serial_number or "",
                "serial_number": reader.serial_number or "",
                "port_no": port_no,
                "observed_at": event.read_at,
                "observed_at_ms": int(event.read_at_ms or 0),
                "duration_from_previous": duration,
                "reader_power_dbm": int(reader_node.power_dbm or 0),
                "read_interval_ms": int(reader_node.read_interval_ms or 200),
                "tid_start_address": int(reader_node.tid_addr or 0),
                "tid_length": int(reader_node.tid_len or 4),
            })

        if len(selected) < 2:
            raise ValidationError(_(
                "Lane Setup requires at least two unique observed Reader Ports."
            ))
        if len(controller_ids) != 1 or len(edge_ids) != 1:
            raise ValidationError(_(
                "A Lane must be built from Detection Timeline rows belonging to one Server and one Controller."
            ))
        return selected, next(iter(edge_ids)), next(iter(controller_ids))


    def action_open_lane_setup(self):
        """Open Lane Setup using Detection Timeline as observation defaults."""
        self.ensure_one()
        self.check_access("read")

        events = self.env["nsp.measurement.event"].search([
            ("session_id", "=", self.id),
            ("revision", "=", self.revision),
        ], order="read_at asc, read_at_ms asc, id asc")
        steps = self._build_detection_steps(events)
        configuration_steps = CalibrationTimelineBuilder.unique_reader_port_path(steps)
        ordered_event_ids = [
            int(step.get("first_event_id") or 0)
            for step in configuration_steps
            if int(step.get("first_event_id") or 0) > 0
        ]
        if len(ordered_event_ids) < 2:
            raise ValidationError(_(
                "Lane Setup requires at least two observed Reader Ports in Detection Timeline."
            ))

        selected, edge_server_id, controller_id = self._configuration_steps_from_selection(
            ordered_event_ids
        )
        # Device Configuration is projected from the exact Server -> Controller
        # branch used by the selected Detection Timeline path. The Antenna Sequence
        # remains editable, but it cannot escape that calibrated branch.
        server_node = self._server_nodes().filtered(
            lambda node: node.server_id.id == edge_server_id
        )[:1]
        controller_node = self._controller_nodes().filtered(
            lambda node: node.controller_id.id == controller_id
            and node.parent_id == server_node
        )[:1]
        if not server_node or not controller_node:
            raise ValidationError(_(
                "The selected Detection Timeline rows do not resolve to a valid Device Configuration branch."
            ))

        reader_defaults = {}
        for reader_node in self._reader_nodes().filtered(
            lambda node: node.parent_id == controller_node
        ):
            reader_defaults[reader_node.reader_id.id] = {
                "reader_id": reader_node.reader_id.id,
                "reader_power_dbm": int(reader_node.power_dbm or 0),
                "read_interval_ms": int(reader_node.read_interval_ms or 200),
                "tid_start_address": int(reader_node.tid_addr or 0),
                "tid_length": int(reader_node.tid_len or 4),
            }

        draft_layouts = self.env["nsp.parking.area"].search([("state", "=", "draft")], limit=2)
        default_layout = draft_layouts[:1] if len(draft_layouts) == 1 else self.env["nsp.parking.area"].browse()

        wizard = self.env["nsp.lane.setup.wizard"].create({
            "source_scope": "calibration",
            "session_id": self.id,
            "parking_area_id": default_layout.id if default_layout else False,
            "edge_server_id": edge_server_id,
            "controller_id": controller_id,
            "device_line_ids": [
                (0, 0, {
                    "reader_id": row["reader_id"],
                    "power_dbm": row["reader_power_dbm"],
                    "read_interval_ms": row["read_interval_ms"],
                    "tid_start_address": row["tid_start_address"],
                    "tid_length": row["tid_length"],
                })
                for row in reader_defaults.values()
            ],
            "sequence_line_ids": [
                (0, 0, {
                    "sequence": row["selection_order"],
                    "reader_id": row["reader_id"],
                    "port_no": row["port_no"],
                    "duration_ms": int(round(float(row["duration_from_previous"] or 0.0) * 1000.0)),
                })
                for row in selected
            ],
        })
        return {
            "type": "ir.actions.act_window",
            "name": _("Lane Setup"),
            "res_model": "nsp.lane.setup.wizard",
            "res_id": wizard.id,
            "view_mode": "form",
            "views": [(
                self.env.ref(
                    "nsp_master_gatekeeper.view_nsp_lane_direction_setup_wizard_form"
                ).id,
                "form",
            )],
            "target": "new",
            "context": dict(self.env.context),
        }

    def action_open_lane_direction_setup(self):
        """Deprecated compatibility alias. Removal target: NSP 20.0."""
        return self.action_open_lane_setup()



class NspMeasurementSessionCalibrationResult(models.Model):
    _inherit = "nsp.measurement.session"

    pass_ids = fields.One2many(
        "nsp.measurement.pass", "session_id", string="Calibration Runs", copy=False,
    )
    pass_count = fields.Integer(compute="_compute_calibration_result_counts")
    accepted_pass_count = fields.Integer(compute="_compute_calibration_result_counts")
    result_ids = fields.One2many(
        "nsp.measurement.result", "session_id", string="Calibration Results", copy=False,
    )
    accepted_result_id = fields.Many2one(
        "nsp.measurement.result", compute="_compute_calibration_result_counts",
        string="Accepted Result",
    )
    @api.depends("pass_ids.state", "result_ids.state", "result_ids.accepted_at")
    def _compute_calibration_result_counts(self):
        Result = self.env["nsp.measurement.result"]
        for session in self:
            session.pass_count = len(session.pass_ids)
            session.accepted_pass_count = len(
                session.pass_ids.filtered(lambda item: item.state == "accepted")
            )
            accepted = session.result_ids.filtered(
                lambda item: item.state == "accepted"
            ).sorted(
                key=lambda item: (item.accepted_at or item.write_date or item.create_date, item.id),
                reverse=True,
            )[:1]
            session.accepted_result_id = accepted or Result.browse()

    def _reader_port_for_event(self, event):
        self.ensure_one()
        serial = str(event.serial_number or "").strip().upper()
        port_no = int(event.port_no or 0)
        for reader_node in self._reader_nodes():
            if str(reader_node.reader_id.serial_number or "").strip().upper() != serial:
                continue
            reader_port = reader_node.reader_port_ids.filtered(
                lambda row: int(row.port_no or 0) == port_no
            )[:1]
            if reader_port:
                return reader_port
        return self.env["nsp.measurement.reader.port"]

    def _collapse_events_to_steps(self, events):
        """Return stable consecutive Reader-Port points from raw reads."""
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
                "reader_node_id": reader_port.reader_node_id.id,
                "reader_id": reader_port.reader_node_id.reader_id.id,
                "reader_serial_number": reader_port.reader_node_id.reader_id.serial_number or "",
                "reader_code": reader_port.reader_node_id.reader_id.device_code or "",
                "port_no": int(reader_port.port_no or 0),
                "point_key": "%s:%s" % (
                    reader_port.reader_node_id.reader_id.device_code
                    or reader_port.reader_node_id.reader_id.serial_number
                    or reader_port.reader_node_id.reader_id.id,
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
        self.check_access("write")
        self._require_ready_configuration()
        if len(self.target_line_ids) != 1:
            raise ValidationError(_("Configure exactly one raw RFID Tag before starting a Calibration Run."))
        if self.pass_ids.filtered(lambda item: item.state == "running"):
            raise ValidationError(_("A Calibration Run is already running."))
        if self.status == "ready":
            self._apply_status_transition(
                "running", {"started_at": self.started_at or fields.Datetime.now()}, allow_same=False,
            )
        elif self.status != "running":
            raise ValidationError(_("Release the calibration or use Measure Again before starting a Calibration Run."))
        target = self.target_line_ids[:1]
        next_no = max(self.pass_ids.mapped("pass_no") or [0]) + 1
        self.env["nsp.measurement.pass"].create({
            "session_id": self.id,
            "pass_no": next_no,
            "revision": self.revision,
            "tid": target.tid,
            "started_at": fields.Datetime.now(),
            "state": "running",
        })
        return True

    def action_stop_reference_pass(self):
        self.ensure_one()
        self.check_access("write")
        running = self.pass_ids.filtered(lambda item: item.state == "running").sorted("id")[-1:]
        if not running:
            raise ValidationError(_("No Calibration Run is currently running."))
        running.action_stop_and_analyse()
        return True

    def action_build_calibration_result(self):
        self.ensure_one()
        self.check_access("write")
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
            "reference_tid": self.target_line_ids[:1].tid,
            "accepted_pass_count": total_samples,
            "tolerance_percent": 30.0,
            "line_ids": values,
        })
        return result.action_open_form()
    def _current_calibration_result(self):
        self.ensure_one()
        candidates = self.result_ids.filtered(
            lambda item: item.state == "accepted"
        ).sorted(key=lambda item: (item.revision, item.id), reverse=True)
        return candidates[:1]

class NspMeasurementPass(models.Model):
    _name = "nsp.measurement.pass"
    _description = "NSP Lane Calibration Pass"
    _order = "session_id, pass_no desc, id desc"

    session_id = fields.Many2one("nsp.measurement.session", required=True, ondelete="cascade", index=True)
    pass_no = fields.Integer(required=True, index=True)
    revision = fields.Integer(required=True, default=1, index=True)
    tid = fields.Char(string="Raw TID", required=True, index=True)
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
        self.ensure_one()
        self.check_access("write")
        if self.state != "running":
            raise ValidationError(_("Only a running Run can be stopped."))
        ended_at = fields.Datetime.now()
        events = self.env["nsp.measurement.event"].sudo().search([
            ("session_id", "=", self.session_id.id),
            ("revision", "=", self.revision),
            ("tid", "=", self.tid),
            ("read_at", ">=", self.started_at),
            ("read_at", "<=", ended_at),
        ], order="read_at asc, read_at_ms asc, id asc")
        steps = self.session_id._collapse_events_to_steps(events)
        self.step_ids.unlink()
        commands = [(0, 0, {
            "sequence": row["sequence"],
            "reader_port_id": row["reader_port_id"],
            "first_read_at": row["first_read_at"],
            "first_read_at_ms": row["first_read_at_ms"],
            "last_read_at": row["last_read_at"],
            "last_read_at_ms": row["last_read_at_ms"],
            "read_count": row["read_count"],
            "duration_from_previous": row["duration_from_previous"],
        }) for row in steps]
        self._apply_pass_state("completed", {
            "ended_at": ended_at,
            "result_status": "complete" if len(steps) >= 2 else "insufficient",
            "detected_sequence": " → ".join(row["point_key"] for row in steps),
            "missing_or_error": "" if len(steps) >= 2 else _("At least two detection points are required."),
            "total_duration": sum(float(row["duration_from_previous"] or 0.0) for row in steps),
            "step_ids": commands,
        })
        return True

    def action_accept(self):
        self.check_access("write")
        for record in self:
            if record.result_status != "complete":
                raise ValidationError(_("Only a complete Run can be accepted."))
            record._apply_pass_state("accepted")
        return True

    def action_reject(self):
        self.check_access("write")
        records = self.filtered(lambda item: item.state != "running")
        for record in records:
            record._apply_pass_state("rejected")
        return True

    def _workspace_payload(self):
        self.ensure_one()
        return {
            "id": self.id,
            "pass_no": self.pass_no,
            "revision": self.revision,
            "tid": self.tid or "",
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
    reader_id = fields.Many2one(related="reader_port_id.reader_node_id.reader_id", store=True, readonly=True)
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
    _description = "NSP Lane Calibration Result"
    _order = "session_id, revision desc, id desc"

    name = fields.Char(default=lambda self: new_management_code("CAL"), required=True, readonly=True, copy=False)
    session_id = fields.Many2one("nsp.measurement.session", required=True, ondelete="cascade", index=True)
    revision = fields.Integer(required=True, default=1, index=True)
    reference_tid = fields.Char(string="Calibration Raw TID", required=True, index=True)
    state = fields.Selection([
        ("draft", "Draft"),
        ("accepted", "Accepted"),
        ("superseded", "Superseded"),
    ], default="draft", required=True, index=True)
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

    def action_accept(self):
        self.check_access("write")
        for record in self:
            if record.state != "draft":
                raise ValidationError(_("Only a Draft Calibration Result can be accepted."))
            if len(record.line_ids) < 2:
                raise ValidationError(_("Result requires at least two Timeline points."))
            previous = record.session_id.result_ids.filtered(
                lambda item: item.state == "accepted" and item != record
            )
            for previous_result in previous:
                previous_result._apply_result_state("superseded")
            record._apply_result_state("accepted", {
                "accepted_at": fields.Datetime.now(),
                "accepted_by_id": self.env.user.id,
            })
            record.session_id._apply_status_transition(
                "completed",
                {"ended_at": record.session_id.ended_at or fields.Datetime.now()},
            )
        return True

    def action_open_form(self):
        self.ensure_one()
        self.check_access("read")
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
            "reference_tid": self.reference_tid or "",
            "lines": [line._workspace_payload() for line in self.line_ids.sorted("sequence")],
        }


class NspMeasurementResultLine(models.Model):
    _name = "nsp.measurement.result.line"
    _description = "NSP Result Timeline Line"
    _order = "result_id, sequence, id"

    result_id = fields.Many2one("nsp.measurement.result", required=True, ondelete="cascade", index=True)
    sequence = fields.Integer(required=True)
    reader_port_id = fields.Many2one("nsp.measurement.reader.port", required=True, ondelete="restrict")
    reader_id = fields.Many2one(related="reader_port_id.reader_node_id.reader_id", store=True, readonly=True)
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
