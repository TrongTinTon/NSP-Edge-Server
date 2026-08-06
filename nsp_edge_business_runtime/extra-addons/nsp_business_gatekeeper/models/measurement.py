# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError
from odoo.addons.nsp_core.utils import new_management_code


def _new_measurement_code():
    return new_management_code("MSR")


class NspMeasurementSession(models.Model):
    """Measurement plan shared by Cloud, Edge and one-or-more Controllers.

    The Session owns Vehicle RFID targets and a list of Reader lines.
    Reader ownership determines Controller scope; therefore Controller is not stored
    again on the Session. Each Edge receives only Reader lines belonging to it and
    each physical Controller pulls only its own Reader subset.
    """

    _name = "nsp.measurement.session"
    _description = "NSP Lane Calibration"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _rec_name = "measurement_code"
    _order = "create_date desc, id desc"

    measurement_code = fields.Char(
        required=True,
        readonly=True,
        copy=False,
        index=True,
        default=lambda self: _new_measurement_code(),
    )
    target_line_ids = fields.One2many(
        "nsp.measurement.target.line",
        "session_id",
        string="Vehicles",
        copy=True,
    )
    target_count = fields.Integer(string="Vehicles", compute="_compute_scope_counts")
    target_tag_count = fields.Integer(string="Vehicle RFID Tags", compute="_compute_scope_counts")
    reader_line_ids = fields.One2many(
        "nsp.measurement.reader.line",
        "session_id",
        string="Readers",
        copy=True,
    )
    reader_count = fields.Integer(compute="_compute_scope_counts")
    controller_ids = fields.Many2many(
        "nsp.controller",
        string="Controllers",
        compute="_compute_scope_counts",
        readonly=True,
    )
    controller_count = fields.Integer(compute="_compute_scope_counts")
    revision = fields.Integer(
        string="Revision",
        required=True,
        default=1,
        readonly=True,
        copy=False,
        index=True,
    )
    started_at = fields.Datetime(readonly=True, copy=False)
    ended_at = fields.Datetime(readonly=True, copy=False)
    applied_at = fields.Datetime(readonly=True, copy=False)
    status = fields.Selection(
        [
            ("draft", "Draft"),
            ("ready", "Ready"),
            ("running", "Running"),
            ("completed", "Completed"),
            ("applied", "Configured"),
            ("failed", "Failed"),
            ("cancelled", "Cancelled"),
        ],
        required=True,
        default="draft",
        index=True,
        tracking=True,
    )
    event_ids = fields.One2many(
        "nsp.measurement.event",
        "session_id",
        string="Measurement Observations",
        readonly=True,
    )
    event_count = fields.Integer(compute="_compute_event_count")
    live_dashboard = fields.Boolean(
        string="Live Measurement Dashboard",
        compute="_compute_live_ui",
    )
    is_cloud_deployment = fields.Boolean(
        string="Cloud Deployment",
        compute="_compute_live_ui",
    )

    _sql_constraints = [
        (
            "measurement_code_unique",
            "unique(measurement_code)",
            "Measurement Code must be unique.",
        ),
        (
            "measurement_revision_positive",
            "CHECK(revision > 0)",
            "Measurement Revision must be greater than zero.",
        ),
    ]

    def _deployment_role(self):
        # Deployment ownership is defined by the installed Gatekeeper module.
        return "edge_server"

    def _compute_live_ui(self):
        is_cloud = self._deployment_role() == "cloud"
        for session in self:
            session.live_dashboard = True
            session.is_cloud_deployment = is_cloud

    @api.depends("reader_line_ids", "reader_line_ids.controller_id", "target_line_ids")
    def _compute_scope_counts(self):
        Controller = self.env["nsp.controller"]
        for session in self:
            controllers = session.reader_line_ids.mapped("controller_id")
            session.controller_ids = controllers if controllers else Controller.browse()
            session.controller_count = len(controllers)
            session.reader_count = len(session.reader_line_ids)
            session.target_count = len(session.target_line_ids)
            session.target_tag_count = len(session.target_line_ids.filtered("vehicle_tid"))

    @api.depends("event_ids", "revision")
    def _compute_event_count(self):
        ids = [record.id for record in self if record.id]
        counts = {}
        if ids:
            rows = self.env["nsp.measurement.event"].sudo()._read_group(
                [("session_id", "in", ids)],
                ["session_id", "revision"],
                ["__count"],
            )
            counts = {(session.id, revision): count for session, revision, count in rows}
        for session in self:
            session.event_count = counts.get((session.id, session.revision), 0)

    def _sanitize_target_commands(self, commands):
        if not commands:
            return commands
        Target = self.env["nsp.measurement.target.line"]
        clear_all = any(
            isinstance(command, (list, tuple)) and command and command[0] == 5
            for command in commands
        )
        removed_ids = {
            int(command[1])
            for command in commands
            if isinstance(command, (list, tuple))
            and len(command) > 1
            and command[0] in (2, 3)
            and command[1]
        }
        existing = self.mapped("target_line_ids") if self and not clear_all else Target
        existing = existing.filtered(lambda line: line.id not in removed_ids)
        seen_tids = set(existing.mapped("vehicle_tid"))
        seen_vehicles = set(existing.mapped("vehicle_id").ids)
        cleaned = []
        for command in commands:
            if not isinstance(command, (list, tuple)) or not command:
                cleaned.append(command)
                continue
            operation = command[0]
            if operation not in (0, 1) or len(command) < 3:
                cleaned.append(command)
                continue

            values = dict(command[2] or {})
            current = Target.browse()
            if operation == 1:
                current = Target.browse(int(command[1] or 0)).exists()
                if current:
                    seen_tids.discard(current.vehicle_tid)
                    seen_vehicles.discard(current.vehicle_id.id)

            tid = Target._normalize_tid(
                values.get("vehicle_tid")
                if "vehicle_tid" in values
                else (current.vehicle_tid if current else False)
            )
            vehicle_id = Target._many2one_id(
                values.get("vehicle_id")
                if "vehicle_id" in values
                else (current.vehicle_id.id if current else False)
            )
            if operation == 0 and not tid and not vehicle_id:
                continue
            if not tid or not vehicle_id:
                raise ValidationError(_("Each Vehicle line requires RFID TID and License Plate."))
            if tid in seen_tids:
                raise ValidationError(_("The same RFID TID can be used only once."))
            if vehicle_id in seen_vehicles:
                raise ValidationError(_("The same Vehicle can be used only once."))

            values["vehicle_tid"] = tid
            values["vehicle_id"] = vehicle_id
            seen_tids.add(tid)
            seen_vehicles.add(vehicle_id)
            cleaned.append((operation, command[1] if operation == 1 else 0, values))
        return cleaned

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for source in vals_list:
            vals = dict(source)
            if "target_line_ids" in vals:
                vals["target_line_ids"] = self._sanitize_target_commands(vals.get("target_line_ids"))
            vals["measurement_code"] = str(
                vals.get("measurement_code") or _new_measurement_code()
            ).strip().upper()
            vals["revision"] = max(int(vals.get("revision") or 1), 1)
            if not self.env.context.get("measurement_sync"):
                vals["status"] = "draft"
            prepared.append(vals)
        records = super().create(prepared)
        records._validate_measurement_scope()
        return records

    def write(self, vals):
        values = dict(vals)
        if "target_line_ids" in values:
            values["target_line_ids"] = self._sanitize_target_commands(values.get("target_line_ids"))
        configuration_fields = {
            "measurement_code", "target_line_ids", "reader_line_ids",
        }
        if configuration_fields.intersection(values) and not self.env.context.get("measurement_sync"):
            protected = self.filtered(lambda session: session.status not in ("draft", "completed"))
            if protected:
                raise ValidationError(_(
                    "Measurement configuration can be edited only while Draft or after completion before Measure Again."
                ))
        if "measurement_code" in values:
            values["measurement_code"] = str(values.get("measurement_code") or "").strip().upper()
        result = super().write(values)
        if {"target_line_ids", "reader_line_ids"}.intersection(values):
            self._validate_measurement_scope()
        return result

    @api.constrains("reader_line_ids", "target_line_ids")
    def _check_scope_constraint(self):
        self._validate_measurement_scope()

    def _validate_measurement_scope(self):
        RuntimeAssignment = self.env["nsp.rfid.runtime.assignment"].sudo()
        for session in self:
            reader_ids = session.reader_line_ids.mapped("reader_id").ids
            if len(reader_ids) != len(set(reader_ids)):
                raise ValidationError(_("A Reader can be selected only once in a Lane Calibration."))

            incomplete = session.target_line_ids.filtered(
                lambda line: not line.vehicle_tid or not line.vehicle_id or not line.license_plate
            )
            if incomplete:
                raise ValidationError(_("Every Vehicle line must contain RFID TID and License Plate."))
            tids = session.target_line_ids.mapped("vehicle_tid")
            vehicle_ids = session.target_line_ids.mapped("vehicle_id").ids
            if len(tids) != len(set(tids)):
                raise ValidationError(_("An RFID TID can be selected only once."))
            if len(vehicle_ids) != len(set(vehicle_ids)):
                raise ValidationError(_("A Vehicle can be selected only once."))

            assignments = RuntimeAssignment.search([("tid", "in", tids)]) if tids else RuntimeAssignment.browse()
            assignment_by_tid = {row.tid: row for row in assignments}
            for target in session.target_line_ids:
                assignment = assignment_by_tid.get(target.vehicle_tid)
                if (
                    not assignment
                    or assignment.vehicle_id != target.vehicle_id
                    or not target.vehicle_id.active
                ):
                    raise ValidationError(_("Vehicle RFID runtime assignment is missing, inactive or changed."))

            edge_ids = set(session.reader_line_ids.mapped("edge_server_id").ids)
            if len(edge_ids) > 1:
                raise ValidationError(_("All Reader assemblies must use the same Server."))
        return True

    def _require_ready_configuration(self):
        self.ensure_one()
        missing = []
        if not self.target_line_ids:
            missing.append(_("Vehicles"))
        if not self.reader_line_ids:
            missing.append(_("Readers"))
        if missing:
            raise ValidationError(_("Missing Lane Calibration configuration: %s") % ", ".join(missing))
        missing_ports = self.reader_line_ids.filtered(lambda line: not line.reader_port_ids)
        if missing_ports:
            names = ", ".join(missing_ports.mapped("reader_id.display_name"))
            raise ValidationError(_("Select at least one Reader Port for each RFID Reader. Missing: %s") % names)
        self._validate_measurement_scope()

    def _allowed_target_tids(self):
        """Return TIDs frozen in this released Lane Calibration snapshot.

        The runtime assignment is validated when the Cloud snapshot is applied.
        Re-reading the mutable runtime-assignment table for every event can make
        an already released calibration silently stop accepting its own target
        after an unrelated assignment refresh. Event validation must therefore
        use the immutable session target lines.
        """
        self.ensure_one()
        return {
            self.env["nsp.rfid.runtime.assignment"].sudo()._normalize_tid(line.vehicle_tid)
            for line in self.target_line_ids
            if line.vehicle_tid and line.vehicle_id
        }

    def _allowed_reader_port_pairs(self):
        self.ensure_one()
        return {
            ((line.reader_id.serial_number or "").strip().upper(), int(mapping.port_no or 0))
            for line in self.reader_line_ids
            for mapping in line.reader_port_ids
        }

    def _measurement_line_for_serial(self, serial_number):
        self.ensure_one()
        serial = str(serial_number or "").strip().upper()
        return self.reader_line_ids.filtered(
            lambda line: (line.reader_id.serial_number or "").strip().upper() == serial
        )[:1]

    def _reader_power_for_serial(self, serial_number):
        self.ensure_one()
        line = self._measurement_line_for_serial(serial_number)
        return int(line.reader_power_dbm or 0) if line else 0

    def _reader_interval_for_serial(self, serial_number):
        self.ensure_one()
        line = self._measurement_line_for_serial(serial_number)
        return int(line.read_interval_ms or 0) if line else 0

    def _controller_codes(self):
        self.ensure_one()
        return sorted({
            str(line.controller_id.controller_id or "").strip().upper()
            for line in self.reader_line_ids
            if line.controller_id
        })

    def _edge_server_codes(self):
        self.ensure_one()
        values = set()
        for edge in self.reader_line_ids.mapped("edge_server_id"):
            code = str(edge.edge_server_code or "").strip().upper() if edge else ""
            if code:
                values.add(code)
        return sorted(values)

    def action_ready(self):
        for session in self:
            if session.status == "ready":
                continue
            if session.status != "draft":
                raise ValidationError(_("Only draft Lane Calibrations can be released."))
            session._require_ready_configuration()
            session.with_context(measurement_sync=True).write({"status": "ready"})
        return True

    def action_complete(self):
        for session in self:
            if session.status == "completed":
                continue
            if session.status != "running":
                raise ValidationError(_("Only running Lane Calibrations can be completed."))
            session.with_context(measurement_sync=True).write(
                {"status": "completed", "ended_at": fields.Datetime.now()}
            )
        return True

    def action_cancel(self):
        for session in self:
            if session.status in ("completed", "applied", "failed", "cancelled"):
                raise ValidationError(_("This Lane Calibration can no longer be cancelled."))
            session.with_context(measurement_sync=True).write(
                {"status": "cancelled", "ended_at": fields.Datetime.now()}
            )
        return True

    def _release_new_revision(self):
        self.ensure_one()
        self.with_context(measurement_sync=True).write({
            "revision": self.revision + 1,
            "status": "ready",
            "started_at": False,
            "ended_at": False,
            "applied_at": False,
        })
        return self.get_live_snapshot(self.id)

    def action_prepare_device_reconfiguration(self):
        """Create a new editable revision without changing historical Pass/Validation data."""
        self.ensure_one()
        if self.status not in ("completed", "failed", "applied"):
            raise ValidationError(_(
                "Devices can be changed only after the current measurement or validation has finished."
            ))
        running_pass = self.pass_ids.filtered(lambda item: item.state == "running")[:1]
        if running_pass:
            raise ValidationError(_("Stop the running Run before changing devices."))
        running_run = self.validation_run_ids.filtered(lambda item: item.state == "running")[:1]
        if running_run:
            raise ValidationError(_("Stop the running Validation Run before changing devices."))
        self.with_context(measurement_sync=True).write({
            "revision": int(self.revision or 1) + 1,
            "status": "draft",
            "started_at": False,
            "ended_at": False,
            "applied_at": False,
        })
        action = self.action_open_session_form()
        action["name"] = _("Revise · R%(revision)s") % {"revision": self.revision}
        action["context"] = {
            **dict(action.get("context") or {}),
            "form_view_initial_mode": "edit",
            "nsp_device_reconfiguration": True,
        }
        return action

    def action_measure_again(self, reader_settings=None):
        self.ensure_one()
        if self.status not in ("running", "completed", "failed"):
            raise ValidationError(_("Measure Again is available for running, completed, or failed sessions."))
        self._require_ready_configuration()
        if reader_settings not in (None, False, ""):
            if not isinstance(reader_settings, list):
                raise ValidationError(_("Reader settings must be a list."))
            line_by_id = {line.id: line for line in self.reader_line_ids}
            seen = set()
            for item in reader_settings:
                if not isinstance(item, dict):
                    raise ValidationError(_("Invalid Reader settings."))
                try:
                    line_id = int(item.get("reader_line_id") or 0)
                    power = int(item.get("power_dbm"))
                    interval = int(item.get("read_interval_ms"))
                except Exception as exc:
                    raise ValidationError(_("Invalid Reader settings.")) from exc
                line = line_by_id.get(line_id)
                if not line or line_id in seen:
                    raise ValidationError(_("Reader settings do not match this Lane Calibration."))
                seen.add(line_id)
                line.with_context(measurement_sync=True).write({
                    "reader_power_dbm": power,
                    "read_interval_ms": interval,
                })
        return self._release_new_revision()

    def action_apply_reader_settings(self, reader_line_id, power_dbm, read_interval_ms):
        """Apply one Reader configuration and release a new shared revision."""
        self.ensure_one()
        if self.status not in ("running", "completed", "failed"):
            raise ValidationError(_("Reader settings can be applied only while running, completed, or failed."))
        try:
            line_id = int(reader_line_id or 0)
            power = int(power_dbm)
            interval = int(read_interval_ms)
        except Exception as exc:
            raise ValidationError(_("Invalid Reader settings.")) from exc
        line = self.reader_line_ids.filtered(lambda item: item.id == line_id)[:1]
        if not line:
            raise ValidationError(_("Reader does not belong to this Lane Calibration."))
        line.with_context(measurement_sync=True).write({
            "reader_power_dbm": power,
            "read_interval_ms": interval,
        })
        self._require_ready_configuration()
        return self._release_new_revision()

    def action_apply_to_operation(self):
        self.ensure_one()
        if self._deployment_role() != "cloud":
            raise UserError(_("Applying calibration results is owned by the Cloud Master."))
        if self.status != "completed":
            raise ValidationError(_("Complete the Lane Calibration before applying its result to a Lane configuration."))
        self._require_ready_configuration()
        for line in self.reader_line_ids:
            line.reader_id.sudo().write({
                "power_dbm": int(line.reader_power_dbm or 0),
                "read_interval_ms": int(line.read_interval_ms or 200),
            })
        self.with_context(measurement_sync=True).sudo().write({
            "status": "applied",
            "applied_at": fields.Datetime.now(),
        })
        return self.get_live_snapshot(self.id)

    def action_view_events(self):
        self.ensure_one()
        module = "nsp_business_gatekeeper"
        action = self.env.ref("%s.action_nsp_measurement_event" % module).read()[0]
        action["domain"] = [("session_id", "=", self.id)]
        action["context"] = {
            "search_default_session_id": self.id,
            "search_default_group_revision": 1,
        }
        return action

    def _measurement_form_action(self, view_xmlid, name):
        self.ensure_one()
        view = self.env.ref(view_xmlid)
        return {
            "type": "ir.actions.act_window",
            "name": name,
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "views": [(view.id, "form")],
            "target": "current",
            "context": {
                **dict(self.env.context),
                "active_id": self.id,
                "active_ids": [self.id],
                "active_model": self._name,
                "form_view_initial_mode": "view",
            },
        }

    def action_open_live(self):
        """Keep API compatibility while using the unified Lane Calibration form."""
        self.ensure_one()
        return self.action_open_session_form()

    def action_open_session_form(self):
        self.ensure_one()
        module = "nsp_business_gatekeeper"
        return self._measurement_form_action(
            f"{module}.view_nsp_measurement_session_form",
            _("Lane Calibration"),
        )

    def action_live_measure_again(self):
        self.ensure_one()
        self.action_measure_again()
        return self.action_open_live()

    def action_live_complete(self):
        self.ensure_one()
        self.action_complete()
        return self.action_open_live()

    def action_live_cancel(self):
        self.ensure_one()
        self.action_cancel()
        return self.action_open_live()

    def action_live_apply_to_operation(self):
        self.ensure_one()
        self.action_apply_to_operation()
        return self.action_open_live()

    def _target_coverage(self):
        """Return Vehicle-level RFID detection coverage for the current revision."""
        self.ensure_one()
        targets = self.target_line_ids.sorted(
            key=lambda line: ((line.license_plate or ""), (line.vehicle_tid or ""), line.id)
        )
        rows = self.env["nsp.measurement.event"].sudo()._read_group(
            [("session_id", "=", self.id), ("revision", "=", self.revision)],
            ["tid"],
            ["__count", "read_at:min", "read_at:max"],
            order="tid",
        )
        stats = {
            tid: {
                "read_count": int(count or 0),
                "first_read_at": first_read,
                "last_read_at": last_read,
            }
            for tid, count, first_read, last_read in rows
        }
        result = []
        for line in targets:
            data = stats.get(line.vehicle_tid, {})
            read_count = int(data.get("read_count") or 0)
            result.append({
                "id": line.id,
                "vehicle_tid": line.vehicle_tid or "",
                "vehicle_id": line.vehicle_id.id,
                "license_plate": line.license_plate or "",
                "owner_id": line.vehicle_id.owner_id.id if line.vehicle_id.owner_id else False,
                "owner_name": line.vehicle_id.owner_id.display_name if line.vehicle_id.owner_id else "",
                "detected": bool(read_count),
                "read_count": read_count,
                "first_read_at": fields.Datetime.to_string(data.get("first_read_at"))
                if data.get("first_read_at") else None,
                "last_read_at": fields.Datetime.to_string(data.get("last_read_at"))
                if data.get("last_read_at") else None,
            })
        return result

    @api.model
    def get_live_snapshot(self, session_id, last_event_id=0, limit=2000):
        session = self.sudo().browse(int(session_id or 0)).exists()
        if not session:
            return {"found": False}
        try:
            limit = min(max(int(limit or 2000), 100), 5000)
        except Exception:
            limit = 2000
        events = self.env["nsp.measurement.event"].sudo().search(
            [("session_id", "=", session.id), ("revision", "=", session.revision)],
            order="read_at asc, read_at_ms asc, id asc",
            limit=limit,
        )
        steps = session._build_detection_steps(events)
        vehicles = session._target_coverage()
        detected_vehicle_count = sum(1 for vehicle in vehicles if vehicle["detected"])
        controllers = []
        for controller in session.reader_line_ids.mapped("controller_id").sorted(
            key=lambda item: ((item.controller_id or ""), item.id)
        ):
            edge_code = ""
            edge_status = ""
            line = session.reader_line_ids.filtered(lambda row: row.controller_id == controller)[:1]
            if line and line.edge_server_id:
                edge_code = line.edge_server_id.edge_server_code or ""
                edge_status = line.edge_server_id.status or ""
            controllers.append({
                "id": controller.id,
                "code": controller.controller_id or "",
                "name": controller.controller_name or "",
                "edge_server_code": edge_code,
                "edge_status": edge_status,
            })
        readers = []
        for line in session.reader_line_ids.sorted(
            key=lambda item: (
                (item.controller_id.controller_id or ""),
                (item.reader_id.name or ""),
                (item.reader_id.serial_number or ""),
                item.id,
            )
        ):
            reader = line.reader_id
            controller = line.controller_id
            readers.append({
                "reader_line_id": line.id,
                "id": reader.id,
                "name": reader.name or reader.serial_number or "",
                "serial_number": reader.serial_number or "",
                "controller_code": controller.controller_id or "",
                "controller_name": controller.controller_name or "",
                "status": reader.status or "",
                "runtime_power_dbm": int(reader.runtime_power_dbm or 0),
                "runtime_read_interval_ms": int(reader.runtime_read_interval_ms or 0),
                "reader_power_dbm": int(line.reader_power_dbm or 0),
                "read_interval_ms": int(line.read_interval_ms or 0),
                "firmware_version": reader.firmware_version or "",
                "ports": sorted(line.reader_port_ids.mapped("port_no")),
            })
        return {
            "found": True,
            "session_id": session.id,
            "deployment_role": session._deployment_role(),
            "measurement_code": session.measurement_code,
            "revision": int(session.revision or 1),
            "status": session.status,
            "controllers": controllers,
            "controller_count": len(controllers),
            "edge_server_codes": session._edge_server_codes(),
            "vehicles": vehicles,
            "vehicle_count": len(vehicles),
            "vehicle_tag_count": int(session.target_tag_count or 0),
            "detected_vehicle_count": detected_vehicle_count,
            "coverage_percent": round((detected_vehicle_count * 100.0 / len(vehicles)), 1) if vehicles else 0.0,
            "readers": readers,
            "reader_count": len(readers),
            "started_at": fields.Datetime.to_string(session.started_at) if session.started_at else None,
            "ended_at": fields.Datetime.to_string(session.ended_at) if session.ended_at else None,
            "applied_at": fields.Datetime.to_string(session.applied_at) if session.applied_at else None,
            "raw_event_count": int(session.event_count or 0),
            "detection_count": len(steps),
            "unique_reader_ports": len({(step["serial_number"], step["port_no"]) for step in steps}),
            "unique_readers": len({step["serial_number"] for step in steps}),
            "unique_controllers": len({step["controller_code"] for step in steps}),
            "first_detection": steps[0] if steps else False,
            "last_detection": steps[-1] if steps else False,
            "steps": steps,
            "last_event_id": max(events.ids or [int(last_event_id or 0)]),
            "server_time": fields.Datetime.to_string(fields.Datetime.now()),
        }

    def _event_timestamp(self, event):
        self.ensure_one()
        if not event.read_at:
            return None
        base = fields.Datetime.to_string(event.read_at).replace(" ", "T")
        return "%s.%03dZ" % (base, int(event.read_at_ms or 0))

    def _build_detection_steps(self, events):
        """Collapse consecutive reads for the same target and Reader Port."""
        self.ensure_one()
        lines = {
            (line.reader_id.serial_number or "").strip().upper(): line
            for line in self.reader_line_ids
        }
        targets_by_tid = {
            line.vehicle_tid: {
                "assignment_role": "vehicle",
                "assigned_to": line.license_plate or "",
                "license_plate": line.license_plate or "",
                "owner_name": line.vehicle_id.owner_id.display_name if line.vehicle_id.owner_id else "",
            }
            for line in self.target_line_ids
            if line.vehicle_tid
        }
        steps = []
        current = None
        for event in events:
            key = (event.tid, event.serial_number, int(event.port_no or 0))
            if current and current["_key"] == key:
                current["last_seen_at"] = self._event_timestamp(event)
                current["read_count"] += 1
                continue
            if current:
                current.pop("_key", None)
                steps.append(current)
            line = lines.get((event.serial_number or "").strip().upper())
            target = targets_by_tid.get(event.tid)
            controller = line.controller_id if line else self.env["nsp.controller"]
            current = {
                "_key": key,
                "sequence_no": len(steps) + 1,
                "first_seen_at": self._event_timestamp(event),
                "last_seen_at": self._event_timestamp(event),
                "tid": event.tid,
                "assignment_role": target.get("assignment_role", "") if target else "",
                "assigned_to": target.get("assigned_to", "") if target else "",
                "license_plate": target.get("license_plate", "") if target else "",
                "controller_code": controller.controller_id if controller else "",
                "serial_number": event.serial_number,
                "reader_name": line.reader_id.name if line else event.serial_number,
                "port_no": int(event.port_no or 0),
                "read_count": 1,
            }
        if current:
            current.pop("_key", None)
            steps.append(current)
        for index, step in enumerate(steps, start=1):
            step["sequence_no"] = index
        return steps

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
        except Exception:
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


class NspMeasurementTargetLine(models.Model):
    _name = "nsp.measurement.target.line"
    _description = "NSP Lane Calibration Vehicle"
    _order = "session_id, license_plate, id"

    session_id = fields.Many2one(
        "nsp.measurement.session",
        ondelete="cascade",
        index=True,
    )
    vehicle_tid = fields.Char(
        string="RFID TID", required=True, index=True,
    )
    vehicle_id = fields.Many2one(
        "nsp.vehicle", string="License Plate", required=True,
        ondelete="restrict", index=True,
        domain=[("active", "=", True)],
    )
    license_plate = fields.Char(
        related="vehicle_id.license_plate", readonly=True, store=True, index=True,
    )
    owner_id = fields.Many2one(
        related="vehicle_id.owner_id", string="Owner", readonly=True, store=True,
    )
    owner_locked = fields.Boolean(compute="_compute_owner_locked")
    vehicle_detection_state = fields.Selection(
        [("pending", "Not Detected"), ("detected", "Detected")],
        compute="_compute_detection_state",
    )
    vehicle_detection_count = fields.Integer(compute="_compute_detection_state")

    _sql_constraints = [
        (
            "measurement_target_tid_unique",
            "unique(session_id, vehicle_tid)",
            "This RFID TID is already selected in the Lane Calibration.",
        ),
        (
            "measurement_target_vehicle_unique",
            "unique(session_id, vehicle_id)",
            "This Vehicle is already selected in the Lane Calibration.",
        ),
    ]

    @api.depends("vehicle_id.owner_id")
    def _compute_owner_locked(self):
        for line in self:
            line.owner_locked = bool(line.vehicle_id.owner_id)

    @api.model
    def _normalize_tid(self, value):
        return self.env["nsp.rfid.runtime.assignment"]._normalize_tid(value)

    @api.model
    def _many2one_id(self, value):
        if isinstance(value, (list, tuple)):
            value = value[0] if value else False
        if isinstance(value, dict):
            value = value.get("id") or value.get("resId")
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    @api.depends("session_id.revision", "session_id.event_ids")
    def _compute_detection_state(self):
        session_ids = self.mapped("session_id").ids
        counts = {}
        if session_ids:
            rows = self.env["nsp.measurement.event"].sudo()._read_group(
                [("session_id", "in", session_ids)],
                ["session_id", "revision", "tid"],
                ["__count"],
            )
            counts = {
                (session.id, int(revision or 1), tid): int(count or 0)
                for session, revision, tid, count in rows
            }
        for line in self:
            count = counts.get((
                line.session_id.id,
                int(line.session_id.revision or 1),
                line.vehicle_tid,
            ), 0)
            line.vehicle_detection_count = count
            line.vehicle_detection_state = "detected" if count else "pending"

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for source in vals_list:
            vals = dict(source)
            vals["vehicle_tid"] = self._normalize_tid(vals.get("vehicle_tid"))
            if not vals["vehicle_tid"] or not self._many2one_id(vals.get("vehicle_id")):
                raise ValidationError(_("RFID TID and License Plate are required."))
            prepared.append(vals)
        records = super().create(prepared)
        records._check_runtime_assignment()
        return records

    def write(self, vals):
        values = dict(vals)
        if "vehicle_tid" in values:
            values["vehicle_tid"] = self._normalize_tid(values.get("vehicle_tid"))
            if not values["vehicle_tid"]:
                raise ValidationError(_("RFID TID is required."))
        result = super().write(values)
        self._check_runtime_assignment()
        return result

    @api.constrains("vehicle_tid", "vehicle_id")
    def _check_runtime_assignment(self):
        RuntimeAssignment = self.env["nsp.rfid.runtime.assignment"].sudo()
        tids = self.mapped("vehicle_tid")
        assignments = RuntimeAssignment.search([("tid", "in", tids)]) if tids else RuntimeAssignment.browse()
        by_tid = {row.tid: row for row in assignments}
        for line in self:
            assignment = by_tid.get(line.vehicle_tid)
            if (
                not assignment
                or assignment.vehicle_id != line.vehicle_id
                or not line.vehicle_id.active
            ):
                raise ValidationError(_("RFID TID is not assigned to this active Vehicle at Edge runtime."))


class NspMeasurementReaderLine(models.Model):
    """Contextual hardware assembly for one Lane Calibration.

    This line stores the Server, Controller, RFID Reader and Reader Ports used by the calibration session.
    """

    _name = "nsp.measurement.reader.line"
    _description = "NSP Measurement Reader Assembly"
    _order = "session_id, edge_server_id, controller_id, reader_id, id"

    session_id = fields.Many2one(
        "nsp.measurement.session",
        required=False,
        ondelete="cascade",
        index=True,
        help=(
            "Assigned automatically when the Reader Assembly is attached to "
            "a Lane Calibration. It may be temporarily empty while editing "
            "a new, unsaved calibration form."
        ),
    )
    edge_server_id = fields.Many2one(
        "nsp.edge.server", string="Server", required=True,
        ondelete="restrict", index=True,
    )
    controller_id = fields.Many2one(
        "nsp.controller", string="Controller", required=True,
        ondelete="restrict", index=True,
    )
    reader_id = fields.Many2one(
        "nsp.device", string="RFID Reader", required=True,
        ondelete="restrict", index=True,
    )
    serial_number = fields.Char(related="reader_id.serial_number", readonly=True)
    reader_status = fields.Selection(related="reader_id.status", readonly=True)
    reader_tid_addr = fields.Integer(
        related="reader_id.tid_addr", string="TID Start Address (Words)", readonly=False,
    )
    reader_tid_len = fields.Integer(
        related="reader_id.tid_len", string="TID Length (Words)", readonly=False,
    )
    reader_power_dbm = fields.Integer(
        string="Reader Power (dBm)", default=30, required=True,
    )
    read_interval_ms = fields.Integer(
        string="Read Interval ms", default=200, required=True,
        help="Temporary inventory interval applied during this calibration.",
    )
    reader_port_ids = fields.One2many(
        "nsp.measurement.reader.port", "reader_line_id",
        string="Reader Ports", copy=True,
    )
    port_count = fields.Integer(compute="_compute_port_count")

    available_edge_server_ids = fields.Many2many(
        "nsp.edge.server", compute="_compute_available_devices", readonly=True,
    )
    available_controller_ids = fields.Many2many(
        "nsp.controller", compute="_compute_available_devices", readonly=True,
    )
    available_reader_ids = fields.Many2many(
        "nsp.device", compute="_compute_available_devices", readonly=True,
    )

    @api.model
    def default_get(self, fields_list):
        values = super().default_get(fields_list)
        if "session_id" in fields_list and not values.get("session_id"):
            session_id = self.env.context.get("default_session_id")
            if not session_id and self.env.context.get("active_model") == "nsp.measurement.session":
                session_id = self.env.context.get("active_id")
            if session_id:
                values["session_id"] = int(session_id)
        return values

    @api.model
    def _session_id_from_context(self):
        session_id = self.env.context.get("default_session_id")
        if not session_id and self.env.context.get("active_model") == "nsp.measurement.session":
            session_id = self.env.context.get("active_id")
        return int(session_id) if session_id else False

    _sql_constraints = [
        (
            "measurement_reader_unique", "unique(session_id, reader_id)",
            "An RFID Reader can be selected only once in a Lane Calibration.",
        ),
        (
            "measurement_reader_power_range",
            "CHECK(reader_power_dbm >= 0 AND reader_power_dbm <= 40)",
            "Reader Power must be between 0 and 40 dBm.",
        ),
        (
            "measurement_reader_interval_range",
            "CHECK(read_interval_ms > 0 AND read_interval_ms <= 60000)",
            "Read Interval must be between 1 and 60000 ms.",
        ),
    ]
    @api.depends("reader_port_ids")
    def _compute_port_count(self):
        for line in self:
            line.port_count = len(line.reader_port_ids)

    @api.model
    def _active_whitelisted(self, model_name, type_code):
        return self.env[model_name].search([
            ("active", "=", True),
            ("whitelist_id", "!=", False),
            ("whitelist_id.active", "=", True),
            ("whitelist_id.device_type_code", "=", type_code),
        ])

    @api.depends("edge_server_id", "controller_id", "reader_id")
    def _compute_available_devices(self):
        Edge = self.env["nsp.edge.server"]
        Controller = self.env["nsp.controller"]
        Reader = self.env["nsp.device"]
        edges = self._active_whitelisted("nsp.edge.server", "SERVER")
        controllers = self._active_whitelisted("nsp.controller", "CONTROLLER")
        readers = self._active_whitelisted("nsp.device", "RFID_READER")
        for line in self:
            line.available_edge_server_ids = edges if edges else Edge.browse()
            line.available_controller_ids = controllers if controllers else Controller.browse()
            line.available_reader_ids = readers if readers else Reader.browse()

    @api.model
    def _validate_whitelist_identity(self, record, type_code, label):
        if (
            not record
            or not record.active
            or not record.whitelist_id
            or not record.whitelist_id.active
            or record.whitelist_id.device_type_code != type_code
        ):
            raise ValidationError(
                _("%(label)s must be an active %(type)s from Device Whitelist.")
                % {"label": label, "type": type_code.replace("_", " ").title()}
            )

    def _validate_line_scope(self):
        for line in self:
            self._validate_whitelist_identity(line.edge_server_id, "SERVER", _("Server"))
            self._validate_whitelist_identity(line.controller_id, "CONTROLLER", _("Controller"))
            self._validate_whitelist_identity(line.reader_id, "RFID_READER", _("RFID Reader"))
            if line.reader_power_dbm < 0 or line.reader_power_dbm > 40:
                raise ValidationError(_("Reader Power must be between 0 and 40 dBm."))
            if line.read_interval_ms <= 0 or line.read_interval_ms > 60000:
                raise ValidationError(_("Read Interval must be between 1 and 60000 ms."))
            if line.reader_tid_addr < 0:
                raise ValidationError(_("TID Start Address (Words) cannot be negative."))
            if line.reader_tid_len <= 0:
                raise ValidationError(_("TID Length (Words) must be greater than zero."))
            if not line.reader_port_ids:
                raise ValidationError(_("Select at least one Reader Port for every RFID Reader."))
            for port in line.reader_port_ids:
                port._validate_port()
        return True

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        context_session_id = self._session_id_from_context()
        for source in vals_list:
            values = dict(source)
            if not values.get("session_id") and context_session_id:
                values["session_id"] = context_session_id
            # A Reader Assembly opened from an unsaved Lane Calibration is
            # first kept as an x2many child without a database parent id. Odoo
            # assigns session_id when the parent form is saved. Do not block
            # that standard form workflow.
            reader = self.env["nsp.device"].browse(values.get("reader_id")).exists()
            if reader:
                values.setdefault(
                    "reader_power_dbm",
                    int(reader.runtime_power_dbm or reader.power_dbm or 30),
                )
                values.setdefault(
                    "read_interval_ms",
                    int(reader.runtime_read_interval_ms or reader.read_interval_ms or 200),
                )
            prepared.append(values)
        records = super().create(prepared)
        records._validate_line_scope()
        return records

    def write(self, vals):
        if not self.env.context.get("measurement_sync"):
            protected = self.filtered(
                lambda line: line.session_id
                and line.session_id.status not in ("draft", "completed")
            )
            if protected:
                raise ValidationError(
                    _("Device assembly can be edited only while Draft or after completion before Measure Again.")
                )
        result = super().write(vals)
        self._validate_line_scope()
        return result

    def unlink(self):
        if not self.env.context.get("measurement_sync"):
            protected = self.filtered(
                lambda line: line.session_id
                and line.session_id.status not in ("draft", "completed")
            )
            if protected:
                raise ValidationError(
                    _("Calibration device lines can be removed only while Draft or after completion before Measure Again.")
                )
        return super().unlink()

    @api.onchange("reader_id")
    def _onchange_reader_id(self):
        for line in self:
            if not line.reader_id:
                continue
            line.reader_power_dbm = int(
                line.reader_id.runtime_power_dbm or line.reader_id.power_dbm or 30
            )
            line.read_interval_ms = int(
                line.reader_id.runtime_read_interval_ms
                or line.reader_id.read_interval_ms
                or 200
            )

    @api.constrains(
        "edge_server_id", "controller_id", "reader_id", "reader_port_ids",
        "reader_power_dbm", "read_interval_ms", "session_id",
    )
    def _check_line_scope(self):
        self._validate_line_scope()


class NspMeasurementReaderPort(models.Model):
    _name = "nsp.measurement.reader.port"
    _description = "NSP Measurement Reader Port"
    _order = "reader_line_id, port_no, id"
    _rec_name = "display_name"

    reader_line_id = fields.Many2one(
        "nsp.measurement.reader.line", required=True,
        ondelete="cascade", index=True,
    )
    session_id = fields.Many2one(
        related="reader_line_id.session_id", store=True, readonly=True, index=True,
    )
    port_no = fields.Integer(
        string="Port", required=True, index=True,
        help="Physical RFID Reader port number. Allowed range: 1 to 16.",
    )
    display_name = fields.Char(compute="_compute_display_name")

    _sql_constraints = [
        (
            "measurement_reader_port_unique", "unique(reader_line_id, port_no)",
            "Reader Port must be unique per RFID Reader.",
        ),
        (
            "measurement_reader_port_range", "CHECK(port_no >= 1 AND port_no <= 16)",
            "Reader Port must be an integer from 1 to 16.",
        ),
    ]

    @api.model
    def default_get(self, fields_list):
        values = super().default_get(fields_list)
        if "port_no" in fields_list and not values.get("port_no"):
            reader_line_id = self.env.context.get("default_reader_line_id")
            if reader_line_id:
                reader_line = self.env["nsp.measurement.reader.line"].browse(
                    int(reader_line_id)
                ).exists()
                if reader_line:
                    next_port = max(reader_line.reader_port_ids.mapped("port_no") or [0]) + 1
                    values["port_no"] = next_port if next_port <= 16 else False
        return values

    @api.model_create_multi
    def create(self, vals_list):
        next_by_reader = {}
        prepared = []
        for source in vals_list:
            values = dict(source)
            reader_line_id = int(values.get("reader_line_id") or 0)
            if reader_line_id and not values.get("port_no"):
                if reader_line_id not in next_by_reader:
                    existing = self.search([
                        ("reader_line_id", "=", reader_line_id),
                    ]).mapped("port_no")
                    next_by_reader[reader_line_id] = max(existing or [0]) + 1
                values["port_no"] = next_by_reader[reader_line_id]
                next_by_reader[reader_line_id] += 1
            prepared.append(values)
        records = super().create(prepared)
        records._validate_port()
        return records

    @api.depends("port_no")
    def _compute_display_name(self):
        for record in self:
            record.display_name = _("Port %s") % (record.port_no or "-")

    def _validate_port(self):
        for record in self:
            port_no = int(record.port_no or 0)
            if port_no < 1 or port_no > 16:
                raise ValidationError(_("Reader Port must be an integer from 1 to 16."))
        return True

    @api.constrains("port_no", "reader_line_id")
    def _check_port(self):
        self._validate_port()


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
            vals["tid"] = self.env["nsp.rfid.runtime.assignment"]._normalize_tid(vals.get("tid"))
            vals["revision"] = max(int(vals.get("revision") or 1), 1)
            vals["read_at_ms"] = max(0, min(int(vals.get("read_at_ms") or 0), 999))
            vals["read_interval_ms"] = max(1, min(int(vals.get("read_interval_ms") or 200), 60000))
            prepared.append(vals)
        return super().create(prepared)

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
