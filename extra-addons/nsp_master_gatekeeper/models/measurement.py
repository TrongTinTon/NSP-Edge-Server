# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError
from odoo.addons.nsp_core.utils import new_management_code


def _new_measurement_code():
    return new_management_code("MSR")


class NspMeasurementSession(models.Model):
    """Measurement plan shared by Cloud, Edge and one-or-more Controllers.

    The Session owns paired User/Vehicle RFID targets and a list of Reader lines.
    Reader ownership determines Controller scope; therefore Controller is not stored
    again on the Session. Each Edge receives only Reader lines belonging to it and
    each physical Controller pulls only its own Reader subset.
    """

    _name = "nsp.measurement.session"
    _description = "NSP Measurement Session"
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
        string="RFID Target Pairs",
        copy=True,
    )
    target_count = fields.Integer(string="Target Pairs", compute="_compute_scope_counts")
    target_tag_count = fields.Integer(string="RFID Tags", compute="_compute_scope_counts")
    reader_line_ids = fields.One2many(
        "nsp.measurement.reader.line",
        "session_id",
        string="Measurement Readers",
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
    planned_start_at = fields.Datetime(tracking=True)
    planned_end_at = fields.Datetime(tracking=True)
    started_at = fields.Datetime(readonly=True, copy=False)
    ended_at = fields.Datetime(readonly=True, copy=False)
    applied_at = fields.Datetime(readonly=True, copy=False)
    note = fields.Text()
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

    @api.depends("reader_line_ids", "reader_line_ids.reader_id", "reader_line_ids.reader_id.controller_id", "target_line_ids")
    def _compute_scope_counts(self):
        Controller = self.env["nsp.controller"]
        for session in self:
            controllers = session.reader_line_ids.mapped("reader_id.controller_id")
            session.controller_ids = controllers if controllers else Controller.browse()
            session.controller_count = len(controllers)
            session.reader_count = len(session.reader_line_ids)
            session.target_count = len(session.target_line_ids)
            session.target_tag_count = sum(
                int(bool(line.user_assignment_id)) + int(bool(line.vehicle_assignment_id))
                for line in session.target_line_ids
            )

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
        """Resolve paired RFID scans and remove only a completely blank virtual row.

        One target line is one real-world measurement pair: one active User RFID Tag
        and one active Vehicle RFID Tag. The License Plate is resolved from the active
        Vehicle RFID Tag assignment. Partially entered rows are never silently discarded.
        """
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
        seen_users = set(existing.mapped("user_assignment_id").ids)
        seen_vehicles = set(existing.mapped("vehicle_assignment_id").ids)
        seen_pairs = {
            (line.user_assignment_id.id, line.vehicle_assignment_id.id)
            for line in existing
            if line.user_assignment_id and line.vehicle_assignment_id
        }

        cleaned = []
        for command in commands:
            if not isinstance(command, (list, tuple)) or not command:
                cleaned.append(command)
                continue
            operation = command[0]
            if operation == 0 and len(command) >= 3:
                values = Target._prepare_scanned_values(dict(command[2] or {}))
                user_id = Target._many2one_id(values.get("user_assignment_id"))
                vehicle_id = Target._many2one_id(values.get("vehicle_assignment_id"))
                has_user_input = bool(user_id or Target._normalize_tid(values.get("user_scan_tid")))
                has_vehicle_input = bool(vehicle_id or Target._normalize_tid(values.get("vehicle_scan_tid")))

                if not has_user_input and not has_vehicle_input:
                    continue
                if not user_id or not vehicle_id:
                    raise ValidationError(_(
                        "Each RFID Target line requires one User RFID Tag and one Vehicle RFID Tag."
                    ))

                pair = (user_id, vehicle_id)
                if pair in seen_pairs:
                    continue
                if user_id in seen_users:
                    raise ValidationError(_(
                        "The same User RFID Tag can be used only once in a Measurement Session."
                    ))
                if vehicle_id in seen_vehicles:
                    raise ValidationError(_(
                        "The same Vehicle RFID Tag can be used only once in a Measurement Session."
                    ))
                cleaned.append((0, 0, values))
                seen_users.add(user_id)
                seen_vehicles.add(vehicle_id)
                seen_pairs.add(pair)
                continue

            if operation == 1 and len(command) >= 3:
                values = dict(command[2] or {})
                if {
                    "user_scan_tid", "user_assignment_id", "vehicle_scan_tid", "vehicle_assignment_id"
                }.intersection(values):
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
            "planned_start_at", "planned_end_at", "note",
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

    @api.constrains("planned_start_at", "planned_end_at")
    def _check_planned_time(self):
        for session in self:
            if (
                session.planned_start_at
                and session.planned_end_at
                and session.planned_end_at <= session.planned_start_at
            ):
                raise ValidationError(_("Planned end time must be later than planned start time."))

    @api.constrains("reader_line_ids", "target_line_ids")
    def _check_scope_constraint(self):
        self._validate_measurement_scope()

    def _active_reader_antennas(self, reader):
        antennas = reader.antennas_ids
        if "active" in antennas._fields:
            antennas = antennas.filtered("active")
        if "cloud_removed" in antennas._fields:
            antennas = antennas.filtered(lambda antenna: not antenna.cloud_removed)
        return antennas

    def _validate_measurement_scope(self):
        for session in self:
            seen_readers = set()
            for line in session.reader_line_ids:
                if line.reader_id.id in seen_readers:
                    raise ValidationError(_("A Reader can be selected only once in a Measurement Session."))
                seen_readers.add(line.reader_id.id)
                invalid = line.antenna_ids.filtered(lambda antenna: antenna.device_id != line.reader_id)
                if invalid:
                    raise ValidationError(_("Every Measurement Antenna must belong to its Reader."))
            incomplete = session.target_line_ids.filtered(
                lambda line: not line.user_assignment_id or not line.vehicle_assignment_id or not line.license_plate
            )
            if incomplete:
                raise ValidationError(_(
                    "Every RFID Target line must contain one User RFID Tag, one Vehicle RFID Tag and a License Plate."
                ))
            user_ids = session.target_line_ids.mapped("user_assignment_id").ids
            vehicle_ids = session.target_line_ids.mapped("vehicle_assignment_id").ids
            if len(user_ids) != len(set(user_ids)):
                raise ValidationError(_(
                    "A User RFID Tag can be selected only once in a Measurement Session."
                ))
            if len(vehicle_ids) != len(set(vehicle_ids)):
                raise ValidationError(_(
                    "A Vehicle RFID Tag can be selected only once in a Measurement Session."
                ))
            for target in session.target_line_ids:
                user = target._resolve_assignment(target.user_assignment_id.id, expected_target="user")
                vehicle = target._resolve_assignment(target.vehicle_assignment_id.id, expected_target="vehicle")
                if int(user.get("user_id") or 0) != target.user_id.id:
                    raise ValidationError(_(
                        "User RFID Tag assignment changed after this Measurement target was created."
                    ))
                if int(vehicle.get("vehicle_id") or 0) != target.vehicle_id.id:
                    raise ValidationError(_(
                        "Vehicle RFID Tag assignment changed after this Measurement target was created."
                    ))
                if str(vehicle.get("license_plate") or "").strip().upper() != str(
                    target.license_plate or ""
                ).strip().upper():
                    raise ValidationError(_(
                        "Vehicle License Plate changed after this Measurement target was created."
                    ))

            controllers = session.reader_line_ids.mapped("reader_id.controller_id")
            if controllers and "edge_server_id" in controllers._fields:
                edge_ids = set(controllers.mapped("edge_server_id").ids)
                if any(not controller.edge_server_id for controller in controllers) or len(edge_ids) > 1:
                    raise ValidationError(
                        _("All Measurement Controllers must belong to the same Edge Server.")
                    )
        return True

    def _require_ready_configuration(self):
        self.ensure_one()
        missing = []
        if not self.target_line_ids:
            missing.append(_("RFID Target Pairs"))
        if not self.reader_line_ids:
            missing.append(_("Measurement Readers"))
        if missing:
            raise ValidationError(_("Missing Measurement configuration: %s") % ", ".join(missing))
        readers_without_antennas = self.reader_line_ids.filtered(
            lambda line: line.reader_id and not self._active_reader_antennas(line.reader_id)
        )
        if readers_without_antennas:
            names = ", ".join(readers_without_antennas.mapped("reader_id.display_name"))
            raise ValidationError(
                _("The following Reader(s) have no active Antennas: %s. Configure antennas first.") % names
            )
        missing_antennas = self.reader_line_ids.filtered(lambda line: not line.antenna_ids)
        if missing_antennas:
            names = ", ".join(missing_antennas.mapped("reader_id.display_name"))
            raise ValidationError(_("Select at least one Antenna for each Measurement Reader. Missing: %s") % names)
        self._validate_measurement_scope()

    def _allowed_target_tids(self):
        self.ensure_one()
        Tag = self.env["nsp.rfid.tag"]
        tids = self.target_line_ids.mapped("user_tid") + self.target_line_ids.mapped("vehicle_tid")
        return {Tag._normalize_tid(tid) for tid in tids if tid}

    def _allowed_antenna_pairs(self):
        self.ensure_one()
        return {
            ((line.reader_id.serial_number or "").strip().upper(), int(antenna.antenna_no or 0))
            for line in self.reader_line_ids
            for antenna in line.antenna_ids
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
            str(line.reader_id.controller_id.controller_id or "").strip().upper()
            for line in self.reader_line_ids
            if line.reader_id.controller_id
        })

    def _edge_server_codes(self):
        self.ensure_one()
        values = set()
        for controller in self.reader_line_ids.mapped("reader_id.controller_id"):
            if "edge_server_id" not in controller._fields:
                continue
            edge = controller.edge_server_id
            code = str(edge.edge_server_code or "").strip().upper() if edge else ""
            if code:
                values.add(code)
        return sorted(values)

    def action_ready(self):
        for session in self:
            if session.status == "ready":
                continue
            if session.status != "draft":
                raise ValidationError(_("Only draft Measurement Sessions can be released."))
            session._require_ready_configuration()
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
            if session.status in ("completed", "applied", "failed", "cancelled"):
                raise ValidationError(_("This Measurement Session can no longer be cancelled."))
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
                    raise ValidationError(_("Reader settings do not match this Measurement Session."))
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
            raise ValidationError(_("Reader does not belong to this Measurement Session."))
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
        self.ensure_one()
        module = "nsp_master_gatekeeper"
        return self._measurement_form_action(
            f"{module}.view_nsp_measurement_session_live_form",
            _("Cloud Live Measurement") if self._deployment_role() == "cloud" else _("Live Measurement"),
        )

    def action_open_session_form(self):
        self.ensure_one()
        module = "nsp_master_gatekeeper"
        return self._measurement_form_action(
            f"{module}.view_nsp_measurement_session_form",
            _("Measurement Session"),
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
        """Return pair-level coverage for the current revision."""
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

        def tag_stats(tid):
            data = stats.get(tid, {})
            return {
                "detected": bool(data.get("read_count")),
                "read_count": int(data.get("read_count") or 0),
                "first_read_at": fields.Datetime.to_string(data.get("first_read_at"))
                if data.get("first_read_at") else None,
                "last_read_at": fields.Datetime.to_string(data.get("last_read_at"))
                if data.get("last_read_at") else None,
            }

        result = []
        for line in targets:
            user = tag_stats(line.user_tid)
            vehicle = tag_stats(line.vehicle_tid)
            result.append({
                "id": line.id,
                "user_assignment_id": line.user_assignment_id.id,
                "user_tid": line.user_tid or "",
                "user_name": line.user_id.display_name or "",
                "user_detected": user["detected"],
                "user_read_count": user["read_count"],
                "user_first_read_at": user["first_read_at"],
                "user_last_read_at": user["last_read_at"],
                "vehicle_assignment_id": line.vehicle_assignment_id.id,
                "vehicle_tid": line.vehicle_tid or "",
                "vehicle_id": line.vehicle_id.id,
                "license_plate": line.license_plate or "",
                "vehicle_detected": vehicle["detected"],
                "vehicle_read_count": vehicle["read_count"],
                "vehicle_first_read_at": vehicle["first_read_at"],
                "vehicle_last_read_at": vehicle["last_read_at"],
                "detected": bool(user["detected"] and vehicle["detected"]),
                "detected_tag_count": int(user["detected"]) + int(vehicle["detected"]),
                "read_count": user["read_count"] + vehicle["read_count"],
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
        target_pairs = session._target_coverage()
        detected_target_count = sum(1 for target in target_pairs if target["detected"])
        controllers = []
        for controller in session.reader_line_ids.mapped("reader_id.controller_id").sorted(
            key=lambda item: ((item.controller_id or ""), item.id)
        ):
            edge_code = ""
            edge_status = ""
            if "edge_server_id" in controller._fields and controller.edge_server_id:
                edge_code = controller.edge_server_id.edge_server_code or ""
                edge_status = controller.edge_server_id.status or ""
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
                (item.reader_id.controller_id.controller_id or ""),
                (item.reader_id.name or ""),
                (item.reader_id.serial_number or ""),
                item.id,
            )
        ):
            reader = line.reader_id
            controller = reader.controller_id
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
                "antennas": sorted(line.antenna_ids.mapped("antenna_no")),
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
            "target_pairs": target_pairs,
            "target_count": len(target_pairs),
            "target_tag_count": int(session.target_tag_count or 0),
            "detected_target_count": detected_target_count,
            "coverage_percent": round((detected_target_count * 100.0 / len(target_pairs)), 1) if target_pairs else 0.0,
            "readers": readers,
            "reader_count": len(readers),
            "started_at": fields.Datetime.to_string(session.started_at) if session.started_at else None,
            "ended_at": fields.Datetime.to_string(session.ended_at) if session.ended_at else None,
            "applied_at": fields.Datetime.to_string(session.applied_at) if session.applied_at else None,
            "planned_start_at": fields.Datetime.to_string(session.planned_start_at) if session.planned_start_at else None,
            "planned_end_at": fields.Datetime.to_string(session.planned_end_at) if session.planned_end_at else None,
            "note": session.note or "",
            "raw_event_count": int(session.event_count or 0),
            "detection_count": len(steps),
            "unique_antennas": len({(step["serial_number"], step["antenna_no"]) for step in steps}),
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
        """Collapse consecutive reads for the same target/Reader/Antenna."""
        self.ensure_one()
        lines = {
            (line.reader_id.serial_number or "").strip().upper(): line
            for line in self.reader_line_ids
        }
        targets_by_tid = {}
        for line in self.target_line_ids:
            if line.user_tid:
                targets_by_tid[line.user_tid] = {
                    "assignment_role": "employee",
                    "assigned_to": line.user_id.display_name or "",
                    "license_plate": line.license_plate or "",
                }
            if line.vehicle_tid:
                targets_by_tid[line.vehicle_tid] = {
                    "assignment_role": "vehicle",
                    "assigned_to": line.license_plate or "",
                    "license_plate": line.license_plate or "",
                }
        steps = []
        current = None
        for event in events:
            key = (event.tid, event.serial_number, int(event.antenna_no or 0))
            if current and current["_key"] == key:
                current["last_seen_at"] = self._event_timestamp(event)
                current["read_count"] += 1
                continue
            if current:
                current.pop("_key", None)
                steps.append(current)
            line = lines.get((event.serial_number or "").strip().upper())
            target = targets_by_tid.get(event.tid)
            controller = line.reader_id.controller_id if line else self.env["nsp.controller"]
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
                "antenna_no": int(event.antenna_no or 0),
                "read_count": 1,
            }
        if current:
            current.pop("_key", None)
            steps.append(current)
        for index, step in enumerate(steps, start=1):
            step["sequence_no"] = index
        return steps

    def _antenna_summary(self):
        self.ensure_one()
        rows = self.env["nsp.measurement.event"].sudo()._read_group(
            [("session_id", "=", self.id), ("revision", "=", self.revision)],
            ["tid", "serial_number", "antenna_no"],
            ["__count", "read_at:min", "read_at:max"],
            order="tid, serial_number, antenna_no",
        )
        return [
            {
                "tid": tid,
                "serial_number": serial_number,
                "antenna_no": int(antenna_no or 0),
                "read_count": int(count or 0),
                "first_read_at": first_read,
                "last_read_at": last_read,
            }
            for tid, serial_number, antenna_no, count, first_read, last_read in rows
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
    _description = "NSP Measurement User-Vehicle RFID Target Pair"
    _order = "session_id, license_plate, id"

    session_id = fields.Many2one(
        "nsp.measurement.session", required=True, ondelete="cascade", index=True,
    )
    user_scan_tid = fields.Char(
        string="Employee RFID Tag", store=False, copy=False,
        help="Keyboard-scanned TID resolved through the active User assignment.",
    )
    user_assignment_id = fields.Many2one(
        "nsp.rfid.tag.assignment", string="Employee RFID Tag", required=True,
        ondelete="restrict", index=True,
        domain=[("state", "=", "active"), ("user_id", "!=", False)],
    )
    user_tid = fields.Char(
        related="user_assignment_id.tag_id.tid", readonly=True, store=True, index=True,
    )
    user_id = fields.Many2one(
        related="user_assignment_id.user_id", string="User", readonly=True,
        store=True, ondelete="restrict", index=True,
    )

    vehicle_scan_tid = fields.Char(
        string="Vehicle RFID Tag", store=False, copy=False,
        help="Keyboard-scanned TID resolved through the active Vehicle assignment.",
    )
    vehicle_assignment_id = fields.Many2one(
        "nsp.rfid.tag.assignment", string="Vehicle RFID Tag", required=True,
        ondelete="restrict", index=True,
        domain=[("state", "=", "active"), ("vehicle_id", "!=", False)],
    )
    vehicle_tid = fields.Char(
        related="vehicle_assignment_id.tag_id.tid", readonly=True, store=True, index=True,
    )
    vehicle_id = fields.Many2one(
        related="vehicle_assignment_id.vehicle_id", string="Vehicle", readonly=True,
        store=True, ondelete="restrict", index=True,
    )
    license_plate = fields.Char(
        related="vehicle_assignment_id.vehicle_id.license_plate",
        readonly=True, store=True, index=True,
    )

    user_detection_state = fields.Selection(
        [("pending", "Not Detected"), ("detected", "Detected")],
        compute="_compute_detection_state", string="Employee Tag Status",
    )
    vehicle_detection_state = fields.Selection(
        [("pending", "Not Detected"), ("detected", "Detected")],
        compute="_compute_detection_state", string="Vehicle Tag Status",
    )
    pair_detection_state = fields.Selection(
        [("pending", "Incomplete"), ("detected", "Detected")],
        compute="_compute_detection_state", string="Pair Status",
    )
    user_detection_count = fields.Integer(compute="_compute_detection_state", string="Employee Reads")
    vehicle_detection_count = fields.Integer(compute="_compute_detection_state", string="Vehicle Reads")

    _sql_constraints = [
        (
            "measurement_target_user_unique",
            "unique(session_id, user_assignment_id)",
            "This Employee RFID Tag is already selected in the Measurement Session.",
        ),
        (
            "measurement_target_vehicle_unique",
            "unique(session_id, vehicle_assignment_id)",
            "This Vehicle RFID Tag is already selected in the Measurement Session.",
        ),
    ]

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
    def _resolve_assignment(self, assignment_id=False, scan_tid=False, expected_target=False):
        Assignment = self.env["nsp.rfid.tag.assignment"].sudo()
        resolved_id = self._many2one_id(assignment_id)
        assignment = Assignment.browse(resolved_id).exists() if resolved_id else Assignment.browse()
        tid = self._normalize_tid(scan_tid or (assignment.tid if assignment else False))
        if not tid:
            return {}
        result = self.env["nsp.rfid.tag"].sudo().nsp_validate_scan(
            tid,
            require_active_assignment=True,
            expected_target=expected_target,
        )
        if not result.get("valid") or not result.get("assignment_id"):
            raise ValidationError(result.get("message") or _("Invalid Measurement RFID Tag."))
        if resolved_id and int(result["assignment_id"]) != resolved_id:
            raise ValidationError(_("The scanned RFID Tag does not match the selected active assignment."))
        return result

    @api.model
    def _prepare_scanned_values(self, vals):
        values = dict(vals)
        user_requested = bool(
            values.get("user_assignment_id") or self._normalize_tid(values.get("user_scan_tid"))
        )
        vehicle_requested = bool(
            values.get("vehicle_assignment_id") or self._normalize_tid(values.get("vehicle_scan_tid"))
        )
        if user_requested:
            result = self._resolve_assignment(
                values.get("user_assignment_id"), values.get("user_scan_tid"), "user"
            )
            values.update({
                "user_assignment_id": int(result["assignment_id"]),
                "user_scan_tid": result.get("tid"),
            })
        if vehicle_requested:
            result = self._resolve_assignment(
                values.get("vehicle_assignment_id"), values.get("vehicle_scan_tid"), "vehicle"
            )
            values.update({
                "vehicle_assignment_id": int(result["assignment_id"]),
                "vehicle_scan_tid": result.get("tid"),
            })
        return values

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
            key = (line.session_id.id, int(line.session_id.revision or 1))
            user_count = counts.get((*key, line.user_tid), 0)
            vehicle_count = counts.get((*key, line.vehicle_tid), 0)
            line.user_detection_count = user_count
            line.vehicle_detection_count = vehicle_count
            line.user_detection_state = "detected" if user_count else "pending"
            line.vehicle_detection_state = "detected" if vehicle_count else "pending"
            line.pair_detection_state = "detected" if user_count and vehicle_count else "pending"

    @api.model_create_multi
    def create(self, vals_list):
        prepared = [self._prepare_scanned_values(vals) for vals in vals_list]
        user_ids = [self._many2one_id(vals.get("user_assignment_id")) for vals in prepared]
        vehicle_ids = [self._many2one_id(vals.get("vehicle_assignment_id")) for vals in prepared]
        if len(user_ids) != len(set(user_ids)):
            raise ValidationError(_("The same Employee RFID Tag is entered more than once."))
        if len(vehicle_ids) != len(set(vehicle_ids)):
            raise ValidationError(_("The same Vehicle RFID Tag is entered more than once."))
        return super().create(prepared)

    def write(self, vals):
        return super().write(self._prepare_scanned_values(vals))

    @api.constrains("user_assignment_id", "vehicle_assignment_id", "session_id")
    def _check_target_pair(self):
        Borrow = self.env["nsp.vehicle.borrow"].sudo()
        for line in self:
            user = line.user_assignment_id.user_id
            vehicle = line.vehicle_assignment_id.vehicle_id
            if not user or line.user_assignment_id.state != "active":
                raise ValidationError(_("Employee RFID Tag must have an active User assignment."))
            if not vehicle or line.vehicle_assignment_id.state != "active":
                raise ValidationError(_("Vehicle RFID Tag must have an active Vehicle assignment."))
            if vehicle.owner_id != user:
                when = line.session_id.planned_start_at or fields.Datetime.now()
                if not Borrow.find_valid_borrow(vehicle, borrower=user, borrow_time=when):
                    raise ValidationError(_(
                        "The selected User must own the Vehicle or have an active Vehicle Borrow permission."
                    ))

    @api.onchange("user_scan_tid")
    def _onchange_user_scan_tid(self):
        for line in self:
            if not line._normalize_tid(line.user_scan_tid):
                continue
            try:
                result = line._resolve_assignment(scan_tid=line.user_scan_tid, expected_target="user")
            except ValidationError:
                line.user_assignment_id = False
                continue
            line.user_assignment_id = self.env["nsp.rfid.tag.assignment"].browse(
                int(result["assignment_id"])
            )
            line.user_scan_tid = result.get("tid")

    @api.onchange("vehicle_scan_tid")
    def _onchange_vehicle_scan_tid(self):
        for line in self:
            if not line._normalize_tid(line.vehicle_scan_tid):
                continue
            try:
                result = line._resolve_assignment(scan_tid=line.vehicle_scan_tid, expected_target="vehicle")
            except ValidationError:
                line.vehicle_assignment_id = False
                continue
            line.vehicle_assignment_id = self.env["nsp.rfid.tag.assignment"].browse(
                int(result["assignment_id"])
            )
            line.vehicle_scan_tid = result.get("tid")


class NspMeasurementReaderLine(models.Model):
    _name = "nsp.measurement.reader.line"
    _description = "NSP Measurement Reader"
    _order = "session_id, id"

    session_id = fields.Many2one(
        "nsp.measurement.session",
        required=True,
        ondelete="cascade",
        index=True,
    )
    reader_id = fields.Many2one(
        "nsp.device",
        string="Reader",
        required=True,
        ondelete="restrict",
        index=True,
    )
    controller_id = fields.Many2one(
        "nsp.controller",
        related="reader_id.controller_id",
        string="Controller",
        readonly=True,
    )
    serial_number = fields.Char(related="reader_id.serial_number", readonly=True)
    reader_status = fields.Selection(related="reader_id.status", readonly=True)
    reader_power_dbm = fields.Integer(string="Reader Power (dBm)", default=30, required=True)
    read_interval_ms = fields.Integer(
        string="Read Interval ms",
        default=200,
        required=True,
        help="Temporary inventory interval applied to this Reader during Measurement.",
    )
    available_antenna_ids = fields.Many2many(
        "nsp.device.antenna",
        string="Available Antennas",
        compute="_compute_available_antennas",
        readonly=True,
    )
    antenna_ids = fields.Many2many(
        "nsp.device.antenna",
        "nsp_measurement_reader_antenna_rel",
        "reader_line_id",
        "antenna_id",
        string="Antennas",
        help="Antennas selected for this Reader during Measurement.",
    )

    _sql_constraints = [
        (
            "measurement_reader_unique",
            "unique(session_id, reader_id)",
            "A Reader can be selected only once in a Measurement Session.",
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

    def _active_antennas(self, reader):
        antennas = reader.antennas_ids
        if "active" in antennas._fields:
            antennas = antennas.filtered("active")
        if "cloud_removed" in antennas._fields:
            antennas = antennas.filtered(lambda antenna: not antenna.cloud_removed)
        return antennas

    @api.depends("reader_id", "reader_id.antennas_ids")
    def _compute_available_antennas(self):
        for line in self:
            line.available_antenna_ids = (
                line._active_antennas(line.reader_id)
                if line.reader_id
                else self.env["nsp.device.antenna"]
            )

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for source in vals_list:
            vals = dict(source)
            reader = self.env["nsp.device"].browse(vals.get("reader_id")).exists()
            if reader and vals.get("reader_power_dbm") in (None, False, ""):
                vals["reader_power_dbm"] = int(reader.runtime_power_dbm or reader.power_dbm or 30)
            if reader and vals.get("read_interval_ms") in (None, False, ""):
                vals["read_interval_ms"] = int(reader.runtime_read_interval_ms or reader.read_interval_ms or 200)
            if reader and "antenna_ids" not in vals:
                vals["antenna_ids"] = [(6, 0, self._active_antennas(reader).ids)]
            prepared.append(vals)
        records = super().create(prepared)
        records._validate_line_scope()
        return records

    def write(self, vals):
        if not self.env.context.get("measurement_sync"):
            protected = self.filtered(lambda line: line.session_id.status not in ("draft", "completed"))
            if protected:
                raise ValidationError(
                    _("Measurement Readers can be edited only while Draft or after completion before Measure Again.")
                )
        values = dict(vals)
        if "reader_id" in values:
            reader = self.env["nsp.device"].browse(values.get("reader_id")).exists()
            if "antenna_ids" not in values:
                values["antenna_ids"] = [(6, 0, self._active_antennas(reader).ids if reader else [])]
            if reader and values.get("reader_power_dbm") in (None, False, ""):
                values["reader_power_dbm"] = int(reader.runtime_power_dbm or reader.power_dbm or 30)
            if reader and values.get("read_interval_ms") in (None, False, ""):
                values["read_interval_ms"] = int(reader.runtime_read_interval_ms or reader.read_interval_ms or 200)
        result = super().write(values)
        self._validate_line_scope()
        return result

    def unlink(self):
        if not self.env.context.get("measurement_sync"):
            protected = self.filtered(lambda line: line.session_id.status not in ("draft", "completed"))
            if protected:
                raise ValidationError(
                    _("Measurement Readers can be removed only while Draft or after completion before Measure Again.")
                )
        return super().unlink()

    @api.onchange("reader_id")
    def _onchange_reader_id(self):
        for line in self:
            if not line.reader_id:
                line.antenna_ids = [(5, 0, 0)]
                continue
            line.reader_power_dbm = int(
                line.reader_id.runtime_power_dbm or line.reader_id.power_dbm or 30
            )
            line.read_interval_ms = int(
                line.reader_id.runtime_read_interval_ms or line.reader_id.read_interval_ms or 200
            )
            line.antenna_ids = [(6, 0, line._active_antennas(line.reader_id).ids)]

    @api.constrains("reader_id", "antenna_ids", "reader_power_dbm", "read_interval_ms", "session_id")
    def _check_line_scope(self):
        self._validate_line_scope()

    def _validate_line_scope(self):
        for line in self:
            if line.reader_power_dbm < 0 or line.reader_power_dbm > 40:
                raise ValidationError(_("Reader Power must be between 0 and 40 dBm."))
            if line.read_interval_ms <= 0 or line.read_interval_ms > 60000:
                raise ValidationError(_("Read Interval must be between 1 and 60000 ms."))
            if not line.antenna_ids:
                raise ValidationError(_("Select at least one Antenna for each Measurement Reader."))
            invalid = line.antenna_ids.filtered(
                lambda antenna: antenna.device_id != line.reader_id
                or ("active" in antenna._fields and not antenna.active)
                or ("cloud_removed" in antenna._fields and antenna.cloud_removed)
            )
            if invalid:
                raise ValidationError(_("Every Measurement Antenna must be active and belong to its Reader."))
        return True


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
    antenna_no = fields.Integer(required=True, index=True)
    tid = fields.Char(required=True, index=True)
    read_at = fields.Datetime(required=True, index=True)
    read_at_ms = fields.Integer(string="Millisecond", required=True, default=0)
    rssi_dbm = fields.Float()
    power_dbm = fields.Integer(string="Reader Power (dBm)")
    read_interval_ms = fields.Integer(string="Read Interval ms", required=True, default=200)

    _sql_constraints = [
        ("measurement_event_uid_unique", "unique(event_uid)", "Measurement Event UID must be unique."),
        ("measurement_event_antenna_positive", "CHECK(antenna_no > 0)", "Antenna number must be greater than zero."),
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

    @api.constrains("session_id", "serial_number", "antenna_no", "tid")
    def _check_event_scope(self):
        for event in self:
            session = event.session_id
            key = (event.serial_number, int(event.antenna_no or 0))
            if key not in session._allowed_antenna_pairs():
                raise ValidationError(_("Measurement observation antenna is not part of the Measurement Session."))
            if event.tid not in session._allowed_target_tids():
                raise ValidationError(_("Only selected RFID Targets may be stored in this Measurement Session."))
