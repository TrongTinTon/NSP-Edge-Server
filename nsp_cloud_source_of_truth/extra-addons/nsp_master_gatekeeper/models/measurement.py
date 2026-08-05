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
            ("applied", "Applied to Operation"),
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
        return "cloud"

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
            session.target_tag_count = len(session.target_line_ids.filtered("tag_id"))

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
        """Normalize Vehicle scan rows and remove only a completely blank virtual row."""
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
        seen_tags = set(existing.mapped("tag_id").ids)
        seen_vehicles = set(existing.mapped("vehicle_id").ids)

        cleaned = []
        for command in commands:
            if not isinstance(command, (list, tuple)) or not command:
                cleaned.append(command)
                continue
            operation = command[0]
            if operation == 0 and len(command) >= 3:
                values = Target._prepare_scanned_values(dict(command[2] or {}))
                tag_id = Target._many2one_id(values.get("tag_id"))
                vehicle_id = Target._many2one_id(values.get("vehicle_id"))
                has_input = bool(
                    tag_id
                    or vehicle_id
                    or Target._normalize_tid(values.get("vehicle_scan_tid"))
                )
                if not has_input:
                    continue
                if not tag_id or not vehicle_id:
                    raise ValidationError(_(
                        "Each Vehicle line requires one RFID Tag and one License Plate."
                    ))
                if tag_id in seen_tags:
                    raise ValidationError(_(
                        "The same RFID Tag can be used only once in a Lane Calibration."
                    ))
                if vehicle_id in seen_vehicles:
                    raise ValidationError(_(
                        "The same Vehicle can be used only once in a Lane Calibration."
                    ))
                cleaned.append((0, 0, values))
                seen_tags.add(tag_id)
                seen_vehicles.add(vehicle_id)
                continue

            if operation == 1 and len(command) >= 3:
                values = dict(command[2] or {})
                if {"vehicle_scan_tid", "tag_id", "vehicle_id"}.intersection(values):
                    values = Target._prepare_scanned_values(values)
                cleaned.append((1, command[1], values))
                continue

            cleaned.append(command)
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
        for session in self:
            seen_readers = set()
            for line in session.reader_line_ids:
                if line.reader_id.id in seen_readers:
                    raise ValidationError(_("A Reader can be selected only once in a Lane Calibration."))
                seen_readers.add(line.reader_id.id)

            incomplete = session.target_line_ids.filtered(
                lambda line: not line.tag_id or not line.vehicle_id or not line.license_plate
            )
            if incomplete:
                raise ValidationError(_(
                    "Every Vehicle line must contain one RFID Tag and one License Plate."
                ))
            tag_ids = session.target_line_ids.mapped("tag_id").ids
            vehicle_ids = session.target_line_ids.mapped("vehicle_id").ids
            if len(tag_ids) != len(set(tag_ids)):
                raise ValidationError(_(
                    "An RFID Tag can be selected only once in a Lane Calibration."
                ))
            if len(vehicle_ids) != len(set(vehicle_ids)):
                raise ValidationError(_(
                    "A Vehicle can be selected only once in a Lane Calibration."
                ))
            for target in session.target_line_ids:
                result = target._resolve_vehicle_scan(target.tag_id.id)
                resolved_vehicle_id = int(result.get("vehicle_id") or 0)
                if resolved_vehicle_id and resolved_vehicle_id != target.vehicle_id.id:
                    raise ValidationError(_(
                        "Vehicle RFID assignment changed after this calibration Vehicle was created."
                    ))

            edge_ids = set(session.reader_line_ids.mapped("edge_server_id").ids)
            if len(edge_ids) > 1:
                raise ValidationError(_(
                    "All Reader assemblies in one calibration must use the same Server."
                ))
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
        self.ensure_one()
        Tag = self.env["nsp.rfid.tag"]
        return {
            Tag._normalize_tid(tid)
            for tid in self.target_line_ids.mapped("vehicle_tid")
            if tid
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
            raise UserError(_("Apply to Operation is owned by the Cloud Master."))
        if self.status != "completed":
            raise ValidationError(_("Complete the Measurement before applying it to operation."))
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
        module = "nsp_master_gatekeeper" if self._deployment_role() == "cloud" else "nsp_business_gatekeeper"
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
        module = "nsp_master_gatekeeper"
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
                "tag_id": line.tag_id.id,
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
            "reader_online_count": sum(1 for reader in readers if reader.get("status") in ("online", "degraded")),
            "reader_offline_count": sum(1 for reader in readers if reader.get("status") == "offline"),
            "configured_reader_port_count": len({
                (reader["serial_number"], int(port_no or 0))
                for reader in readers
                for port_no in reader.get("ports", [])
            }),
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


    @api.model
    def get_infrastructure_scope_snapshot(self, session_id):
        """Return the Lane Calibration infrastructure topology and live health.

        Configuration ownership comes from ``nsp.measurement.reader.line``.
        Runtime health comes from the Edge Server, Controller and Reader
        heartbeat mirrors.  RFID activity is derived only from raw calibration
        observations for the current revision; it is not an Antenna health
        assertion.
        """
        session = self.sudo().browse(int(session_id or 0)).exists()
        if not session:
            return {"found": False}

        now = fields.Datetime.now()
        try:
            silent_after_sec = int(
                self.env["ir.config_parameter"].sudo().get_param(
                    "nsp_master_gatekeeper.lane_calibration_reader_silent_after_sec",
                    "60",
                ) or "60"
            )
        except Exception:
            silent_after_sec = 60
        silent_after_sec = min(max(silent_after_sec, 15), 3600)

        lines = session.reader_line_ids.sorted(
            key=lambda line: (
                line.edge_server_id.edge_server_code or "",
                line.controller_id.controller_id or "",
                line.reader_id.name or "",
                line.reader_id.serial_number or "",
                line.id,
            )
        )
        # The dialog polls every two seconds. Aggregate in PostgreSQL instead
        # of materializing every raw observation into the ORM cache.
        self.env.cr.execute(
            """
            SELECT UPPER(BTRIM(serial_number)) AS serial_number,
                   port_no,
                   COUNT(*) AS detection_count,
                   MIN(read_at) AS first_detection,
                   MAX(read_at) AS last_detection
              FROM nsp_measurement_event
             WHERE session_id = %s
               AND revision = %s
             GROUP BY UPPER(BTRIM(serial_number)), port_no
            """,
            (session.id, int(session.revision or 1)),
        )
        port_stats = {
            (str(serial or "").strip().upper(), int(port_no or 0)): {
                "count": int(detection_count or 0),
                "first_detection": first_detection,
                "last_detection": last_detection,
            }
            for serial, port_no, detection_count, first_detection, last_detection
            in self.env.cr.fetchall()
            if serial and int(port_no or 0) > 0
        }

        connection_labels = dict(
            self.env["nsp.device"]._fields["connection_type"].selection
        )
        edge_map = {}
        warnings = []
        readers_flat = []
        active_runtime = session.status in ("ready", "running")

        def _dt(value):
            return fields.Datetime.to_string(value) if value else None

        def _seconds_since(value):
            if not value:
                return None
            parsed = fields.Datetime.to_datetime(value)
            return max(0, int((now - parsed).total_seconds())) if parsed else None

        def _warning(severity, code, message, happened_at=None, reader_line_id=False):
            warnings.append({
                "severity": severity,
                "code": code,
                "message": message,
                "happened_at": _dt(happened_at) if happened_at else None,
                "reader_line_id": int(reader_line_id or 0),
            })

        for line in lines:
            edge = line.edge_server_id
            controller = line.controller_id
            reader = line.reader_id
            edge_node = edge_map.setdefault(edge.id, {
                "id": edge.id,
                "code": edge.edge_server_code or "",
                "name": edge.name or edge.edge_server_code or "Edge Server",
                "status": edge.status or "offline",
                "last_heartbeat": _dt(edge.timestamp),
                "controllers": {},
            })
            controller_node = edge_node["controllers"].setdefault(controller.id, {
                "id": controller.id,
                "code": controller.controller_id or "",
                "name": controller.controller_name or controller.controller_id or "Controller",
                "status": controller.status or "offline",
                "last_heartbeat": _dt(controller.timestamp),
                "runtime_mode": "Lane Calibration" if active_runtime else "Inactive",
                "readers": [],
            })

            serial_aliases = {
                str(value or "").strip().upper()
                for value in (reader.serial_number, reader.runtime_detected_serial_number)
                if str(value or "").strip()
            }
            configured_ports = sorted(set(line.reader_port_ids.mapped("port_no")))
            observed_ports = sorted({
                port_no for event_serial, port_no in port_stats
                if event_serial in serial_aliases
            })
            status_last_detection = reader.runtime_last_detection_at
            if (
                status_last_detection
                and session.started_at
                and status_last_detection < session.started_at
            ):
                # Do not reuse a detection from a previous runtime session.
                status_last_detection = False
            status_last_port = int(reader.runtime_last_detection_port_no or 0)
            all_ports = sorted(
                set(configured_ports)
                | set(observed_ports)
                | ({status_last_port} if status_last_port > 0 else set())
            )
            ports = []
            reader_detection_count = 0
            reader_last_detection = None
            silent_port_count = 0
            for port_no in all_ports:
                matching_stats = [
                    port_stats.get((serial_alias, port_no), {})
                    for serial_alias in serial_aliases
                    if port_stats.get((serial_alias, port_no))
                ]
                detection_count = sum(int(item.get("count") or 0) for item in matching_stats)
                first_values = [item.get("first_detection") for item in matching_stats if item.get("first_detection")]
                last_values = [item.get("last_detection") for item in matching_stats if item.get("last_detection")]
                first_detection = min(first_values) if first_values else None
                last_detection = max(last_values) if last_values else None
                if (
                    status_last_detection
                    and status_last_port == port_no
                    and (not last_detection or status_last_detection > last_detection)
                ):
                    # edge/status can arrive before the raw event forwarding retry.
                    last_detection = status_last_detection
                reader_detection_count += detection_count
                if last_detection and (
                    not reader_last_detection or last_detection > reader_last_detection
                ):
                    reader_last_detection = last_detection
                configured = port_no in configured_ports
                last_age = _seconds_since(last_detection)
                if last_detection and (last_age is None or last_age <= silent_after_sec):
                    activity = "active"
                elif configured and active_runtime and reader.status in ("online", "degraded"):
                    activity = "silent"
                    silent_port_count += 1
                elif detection_count:
                    activity = "historical"
                else:
                    activity = "unknown"
                ports.append({
                    "port_no": port_no,
                    "configured": configured,
                    "activity": activity,
                    "detection_count": detection_count,
                    "first_detection": _dt(first_detection),
                    "last_detection": _dt(last_detection),
                })
                if not configured and detection_count:
                    _warning(
                        "warning",
                        "port_outside_scope",
                        _("Reader %(reader)s reported Port %(port)s outside the configured Infrastructure Scope.") % {
                            "reader": reader.display_name,
                            "port": port_no,
                        },
                        last_detection,
                        line.id,
                    )

            if (
                status_last_detection
                and (not reader_last_detection or status_last_detection > reader_last_detection)
            ):
                reader_last_detection = status_last_detection
            last_detection_age = _seconds_since(reader_last_detection)
            if reader.status == "offline" and reader_last_detection and (
                last_detection_age is None or last_detection_age <= silent_after_sec
            ):
                # A fresh data-plane detection is stronger evidence than a delayed
                # status mirror.  edge/status will reconcile the persisted status.
                activity_status = "active"
            elif reader.status == "offline":
                activity_status = "offline"
            elif reader.status == "degraded":
                activity_status = "degraded"
            elif reader.status == "online" and reader_last_detection and (
                last_detection_age is None or last_detection_age <= silent_after_sec
            ):
                activity_status = "active"
            elif reader.status == "online" and active_runtime:
                activity_status = "silent"
            elif reader.status == "online":
                activity_status = "connected"
            else:
                activity_status = "unknown"

            effective_reader_status = reader.status or "offline"
            if activity_status == "active" and effective_reader_status == "offline":
                effective_reader_status = "online"

            reader_node = {
                "reader_line_id": line.id,
                "id": reader.id,
                "name": reader.name or reader.serial_number or "RFID Reader",
                "serial_number": reader.serial_number or "",
                "detected_serial_number": reader.runtime_detected_serial_number or "",
                "status": effective_reader_status,
                "activity_status": activity_status,
                "last_seen": _dt(reader.last_seen),
                "last_detection": _dt(reader_last_detection),
                "detection_count": reader_detection_count,
                "firmware_version": reader.firmware_version or "",
                "connection_type": reader.connection_type or "",
                "connection_label": connection_labels.get(reader.connection_type, "") if reader.connection_type else "",
                "runtime_power_dbm": int(reader.runtime_power_dbm or 0),
                "runtime_read_interval_ms": int(reader.runtime_read_interval_ms or 0),
                "configured_power_dbm": int(line.reader_power_dbm or 0),
                "configured_read_interval_ms": int(line.read_interval_ms or 0),
                "ports": ports,
            }
            controller_node["readers"].append(reader_node)
            readers_flat.append(reader_node)

            if edge.status != "online":
                _warning(
                    "danger" if edge.status in ("error", "revoked", "block") else "warning",
                    "edge_not_online",
                    _("Edge Server %(edge)s is %(status)s.") % {
                        "edge": edge.display_name,
                        "status": edge.status or "offline",
                    },
                    edge.timestamp,
                )
            if controller.status != "online":
                _warning(
                    "danger" if controller.status in ("error", "revoked", "block") else "warning",
                    "controller_not_online",
                    _("Controller %(controller)s is %(status)s.") % {
                        "controller": controller.display_name,
                        "status": controller.status or "offline",
                    },
                    controller.timestamp,
                )
            if effective_reader_status == "offline":
                _warning(
                    "danger",
                    "reader_offline",
                    _("Reader %(reader)s is offline.") % {"reader": reader.display_name},
                    reader.last_seen,
                    line.id,
                )
            elif effective_reader_status == "degraded":
                _warning(
                    "warning",
                    "reader_degraded",
                    _("Reader %(reader)s reported a degraded runtime state.") % {
                        "reader": reader.display_name,
                    },
                    reader.last_seen,
                    line.id,
                )
            if effective_reader_status in ("online", "degraded") and not reader.firmware_version:
                _warning(
                    "info",
                    "firmware_unknown",
                    _("Reader %(reader)s is connected but firmware information is unavailable.") % {
                        "reader": reader.display_name,
                    },
                    reader.last_seen,
                    line.id,
                )
            if active_runtime and effective_reader_status in ("online", "degraded") and not reader_last_detection:
                _warning(
                    "warning",
                    "reader_no_detection",
                    _("Reader %(reader)s is connected but has not produced a detection in this calibration.") % {
                        "reader": reader.display_name,
                    },
                    reader.last_seen,
                    line.id,
                )
            elif active_runtime and effective_reader_status in ("online", "degraded") and (
                last_detection_age is not None and last_detection_age > silent_after_sec
            ):
                _warning(
                    "warning",
                    "reader_silent",
                    _("Reader %(reader)s has not produced a detection in the last %(seconds)s seconds.") % {
                        "reader": reader.display_name,
                        "seconds": silent_after_sec,
                    },
                    reader_last_detection,
                    line.id,
                )
            if silent_port_count:
                for port in ports:
                    if port["activity"] == "silent":
                        _warning(
                            "warning",
                            "port_silent",
                            _("Reader %(reader)s Port %(port)s has not produced a recent detection.") % {
                                "reader": reader.display_name,
                                "port": port["port_no"],
                            },
                            fields.Datetime.to_datetime(port["last_detection"]) if port["last_detection"] else reader.last_seen,
                            line.id,
                        )

        edges = []
        for edge in sorted(edge_map.values(), key=lambda item: (item["code"], item["id"])):
            controllers = list(edge.pop("controllers").values())
            controllers.sort(key=lambda item: (item["code"], item["id"]))
            for controller in controllers:
                controller["readers"].sort(
                    key=lambda item: (item["name"], item["serial_number"], item["reader_line_id"])
                )
            edge["controllers"] = controllers
            edges.append(edge)

        # The same Edge/Controller warning can occur once per Reader line.
        unique_warnings = []
        seen_warning_keys = set()
        for warning in warnings:
            key = (
                warning["severity"], warning["code"], warning["message"],
                warning.get("reader_line_id") or 0,
            )
            if key in seen_warning_keys:
                continue
            seen_warning_keys.add(key)
            unique_warnings.append(warning)

        edge_nodes = [edge for edge in edges]
        controller_nodes = [
            controller for edge in edges for controller in edge["controllers"]
        ]
        reader_total = len(readers_flat)
        active_count = sum(1 for item in readers_flat if item["activity_status"] == "active")
        silent_count = sum(1 for item in readers_flat if item["activity_status"] == "silent")
        offline_count = sum(1 for item in readers_flat if item["activity_status"] == "offline")
        degraded_count = sum(1 for item in readers_flat if item["activity_status"] == "degraded")
        connected_count = sum(
            1 for item in readers_flat
            if item["status"] in ("online", "degraded")
        )

        return {
            "found": True,
            "session_id": session.id,
            "measurement_code": session.measurement_code or "",
            "revision": int(session.revision or 1),
            "status": session.status,
            "editable": session.status == "draft",
            "runtime_active": active_runtime,
            "silent_after_sec": silent_after_sec,
            "server_time": fields.Datetime.to_string(now),
            "summary": {
                "edge_total": len(edge_nodes),
                "edge_online": sum(1 for item in edge_nodes if item["status"] == "online"),
                "controller_total": len(controller_nodes),
                "controller_online": sum(1 for item in controller_nodes if item["status"] == "online"),
                "reader_total": reader_total,
                "reader_connected": connected_count,
                "reader_active": active_count,
                "reader_silent": silent_count,
                "reader_offline": offline_count,
                "reader_degraded": degraded_count,
            },
            "edges": edges,
            "readers": readers_flat,
            "warnings": unique_warnings,
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
                "_first_seconds": fields.Datetime.to_datetime(event.read_at).timestamp()
                + (int(event.read_at_ms or 0) / 1000.0),
                "first_event_id": event.id,
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
        previous_seconds = None
        for index, step in enumerate(steps, start=1):
            current_seconds = float(step.pop("_first_seconds", 0.0) or 0.0)
            step["sequence_no"] = index
            step["duration_from_previous"] = (
                0.0 if previous_seconds is None
                else max(current_seconds - previous_seconds, 0.0)
            )
            previous_seconds = current_seconds
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
    """One Vehicle RFID target in a Lane Calibration.

    A scanned TID may already be assigned to a Vehicle. In that case the License
    Plate and existing owner are resolved immediately. When the TID is available,
    the operator can quick-create/select a Vehicle by License Plate; saving the
    line creates the active Vehicle RFID assignment. Vehicle ownership is optional
    for test vehicles and can be assigned by quick-creating/selecting an NSP User.
    """

    _name = "nsp.measurement.target.line"
    _description = "NSP Lane Calibration Vehicle"
    _order = "session_id, license_plate, id"

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
    vehicle_scan_tid = fields.Char(
        string="RFID Tag", store=False, copy=False,
        help=(
            "Scan or enter a whitelisted TID. If it is already assigned to a Vehicle, "
            "the License Plate and owner are resolved automatically."
        ),
    )
    tag_id = fields.Many2one(
        "nsp.rfid.tag", string="RFID Tag", required=True,
        ondelete="restrict", index=True,
    )
    vehicle_tid = fields.Char(
        related="tag_id.tid", string="RFID TID", readonly=True,
        store=True, index=True,
    )
    vehicle_id = fields.Many2one(
        "nsp.vehicle", string="License Plate", required=True,
        ondelete="restrict", index=True,
        domain=[("active", "=", True)],
    )
    license_plate = fields.Char(
        related="vehicle_id.license_plate", string="License Plate",
        readonly=True, store=True, index=True,
    )
    owner_id = fields.Many2one(
        related="vehicle_id.owner_id", string="Owner",
        readonly=False, store=True,
    )
    owner_locked = fields.Boolean(
        compute="_compute_owner_locked", string="Existing Owner",
    )

    vehicle_detection_state = fields.Selection(
        [("pending", "Not Detected"), ("detected", "Detected")],
        compute="_compute_detection_state", string="Vehicle Status",
    )
    vehicle_detection_count = fields.Integer(
        compute="_compute_detection_state", string="Reads",
    )

    _sql_constraints = [
        (
            "measurement_target_tag_unique",
            "unique(session_id, tag_id)",
            "This RFID Tag is already selected in the Lane Calibration.",
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
        return self.env["nsp.rfid.tag"]._normalize_tid(value)

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

    @api.model
    def _resolve_vehicle_scan(self, tag_id=False, scan_tid=False):
        Tag = self.env["nsp.rfid.tag"].sudo()
        resolved_tag_id = self._many2one_id(tag_id)
        tag = Tag.browse(resolved_tag_id).exists() if resolved_tag_id else Tag.browse()
        tid = self._normalize_tid(scan_tid or (tag.tid if tag else False))
        if not tid:
            return {}
        result = Tag.nsp_validate_scan(
            tid,
            require_active_assignment=False,
            expected_target="vehicle_or_available",
        )
        if not result.get("valid") or not result.get("tag_id"):
            raise ValidationError(result.get("message") or _("Invalid Vehicle RFID Tag."))
        if resolved_tag_id and int(result["tag_id"]) != resolved_tag_id:
            raise ValidationError(_("The scanned RFID Tag does not match the selected whitelist tag."))
        return result

    @api.model
    def _prepare_scanned_values(self, vals):
        values = dict(vals)
        scan_requested = bool(
            values.get("tag_id") or self._normalize_tid(values.get("vehicle_scan_tid"))
        )
        if scan_requested:
            result = self._resolve_vehicle_scan(
                values.get("tag_id"), values.get("vehicle_scan_tid")
            )
            values["tag_id"] = int(result["tag_id"])
            values["vehicle_scan_tid"] = result.get("tid")
            resolved_vehicle_id = int(result.get("vehicle_id") or 0)
            selected_vehicle_id = self._many2one_id(values.get("vehicle_id"))
            if resolved_vehicle_id and selected_vehicle_id and resolved_vehicle_id != selected_vehicle_id:
                raise ValidationError(_(
                    "RFID Tag %(tid)s is already assigned to another Vehicle."
                ) % {"tid": result.get("tid") or "-"})
            if resolved_vehicle_id and not selected_vehicle_id:
                values["vehicle_id"] = resolved_vehicle_id

        vehicle_id = self._many2one_id(values.get("vehicle_id"))
        if vehicle_id and not self._many2one_id(values.get("tag_id")):
            assignment = self.env["nsp.rfid.tag.assignment"].sudo().active_for_vehicle(
                self.env["nsp.vehicle"].sudo().browse(vehicle_id)
            )
            if assignment:
                values["tag_id"] = assignment.tag_id.id
                values["vehicle_scan_tid"] = assignment.tid
        return values

    def _ensure_vehicle_assignment(self):
        Assignment = self.env["nsp.rfid.tag.assignment"].sudo()
        for line in self:
            if not line.tag_id or not line.vehicle_id:
                continue
            if not line.vehicle_id.active:
                raise ValidationError(_("An archived Vehicle cannot be used in Lane Calibration."))
            active = Assignment.search([
                ("tag_id", "=", line.tag_id.id),
                ("state", "=", "active"),
            ], limit=1)
            if active:
                if active.user_id:
                    raise ValidationError(_(
                        "RFID Tag %(tid)s is assigned to User %(user)s and cannot be used as a Vehicle Tag."
                    ) % {
                        "tid": line.vehicle_tid or line.tag_id.tid,
                        "user": active.user_id.display_name,
                    })
                if active.vehicle_id != line.vehicle_id:
                    raise ValidationError(_(
                        "RFID Tag %(tid)s is already assigned to Vehicle %(vehicle)s."
                    ) % {
                        "tid": line.vehicle_tid or line.tag_id.tid,
                        "vehicle": active.vehicle_id.display_name,
                    })
                continue
            Assignment.with_context(rfid_audit_user_id=self.env.user.id).create({
                "tag_id": line.tag_id.id,
                "vehicle_id": line.vehicle_id.id,
            })

    @api.depends("session_id.revision", "session_id.event_ids")
    def _compute_detection_state(self):
        session_ids = self.mapped("session_id").ids
        counts = {}
        if session_ids:
            rows = self.env["nsp.measurement.event"].sudo()._read_group(
                [("session_id", "in", session_ids)],
                ["session_id", "revision", "tid"], ["__count"],
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
        prepared = [self._prepare_scanned_values(vals) for vals in vals_list]
        tag_ids = [self._many2one_id(vals.get("tag_id")) for vals in prepared]
        vehicle_ids = [self._many2one_id(vals.get("vehicle_id")) for vals in prepared]
        if any(not value for value in tag_ids):
            raise ValidationError(_("RFID Tag is required for every calibration Vehicle."))
        if any(not value for value in vehicle_ids):
            raise ValidationError(_("License Plate is required for every calibration Vehicle."))
        if len(tag_ids) != len(set(tag_ids)):
            raise ValidationError(_("The same RFID Tag is entered more than once."))
        if len(vehicle_ids) != len(set(vehicle_ids)):
            raise ValidationError(_("The same Vehicle is entered more than once."))
        records = super().create(prepared)
        records._ensure_vehicle_assignment()
        return records

    def write(self, vals):
        result = super().write(self._prepare_scanned_values(vals))
        self._ensure_vehicle_assignment()
        return result

    @api.constrains("tag_id", "vehicle_id", "session_id")
    def _check_vehicle_target(self):
        Assignment = self.env["nsp.rfid.tag.assignment"].sudo()
        for line in self:
            if not line.tag_id or not line.vehicle_id:
                raise ValidationError(_("Each Lane Calibration Vehicle requires an RFID Tag and License Plate."))
            active = Assignment.search([
                ("tag_id", "=", line.tag_id.id),
                ("state", "=", "active"),
            ], limit=1)
            if active and active.vehicle_id != line.vehicle_id:
                if active.user_id:
                    raise ValidationError(_("The selected RFID Tag is assigned to a User."))
                raise ValidationError(_("The selected RFID Tag is assigned to another Vehicle."))

    @api.onchange("tag_id")
    def _onchange_tag_id(self):
        """Resolve a persisted RFID Tag selection inside the Odoo 19 One2many editor.

        The Vehicles popup stores ``tag_id`` directly.  Keeping this onchange on
        the persistent field makes the row complete before the parent form Save
        is executed and avoids relying on the non-stored scanner helper field.
        """
        for line in self:
            if not line.tag_id:
                line.vehicle_scan_tid = False
                continue
            result = line._resolve_vehicle_scan(tag_id=line.tag_id.id)
            line.vehicle_scan_tid = result.get("tid")
            resolved_vehicle_id = int(result.get("vehicle_id") or 0)
            if resolved_vehicle_id:
                line.vehicle_id = self.env["nsp.vehicle"].browse(resolved_vehicle_id)

    @api.onchange("vehicle_scan_tid")
    def _onchange_vehicle_scan_tid(self):
        for line in self:
            if not line._normalize_tid(line.vehicle_scan_tid):
                line.tag_id = False
                continue
            result = line._resolve_vehicle_scan(scan_tid=line.vehicle_scan_tid)
            line.tag_id = self.env["nsp.rfid.tag"].browse(int(result["tag_id"]))
            line.vehicle_scan_tid = result.get("tid")
            if result.get("vehicle_id"):
                line.vehicle_id = self.env["nsp.vehicle"].browse(int(result["vehicle_id"]))

    @api.onchange("vehicle_id")
    def _onchange_vehicle_id(self):
        for line in self:
            if not line.vehicle_id:
                continue
            assignment = self.env["nsp.rfid.tag.assignment"].sudo().active_for_vehicle(line.vehicle_id)
            if assignment and not line.tag_id:
                line.tag_id = assignment.tag_id
                line.vehicle_scan_tid = assignment.tid


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
        # A new Reader Assembly should be immediately saveable from the
        # Infrastructure Scope popup. Seed Port 1 unless the caller supplied
        # an explicit port collection.
        if "reader_port_ids" in fields_list and not values.get("reader_port_ids"):
            values["reader_port_ids"] = [(0, 0, {"port_no": 1})]
        return values

    def action_open_scope_create(self):
        session_id = self._session_id_from_context()
        session = self.env["nsp.measurement.session"].browse(session_id).exists()
        if not session:
            raise UserError(_("Lane Calibration was not found."))
        if session.status != "draft":
            raise ValidationError(_("Infrastructure Scope can be edited only while Lane Calibration is Draft."))
        form_view = self.env.ref(
            "nsp_master_gatekeeper.view_nsp_measurement_reader_line_form"
        )
        return {
            "type": "ir.actions.act_window",
            "name": _("New Reader Assembly"),
            "res_model": self._name,
            "view_mode": "form",
            "views": [(form_view.id, "form")],
            "target": "new",
            "context": {
                **dict(self.env.context),
                "active_model": "nsp.measurement.session",
                "active_id": session.id,
                "active_ids": session.ids,
                "default_session_id": session.id,
                "default_reader_port_ids": [(0, 0, {"port_no": 1})],
            },
        }

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
            vals["tid"] = self.env["nsp.rfid.tag"]._normalize_tid(vals.get("tid"))
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
