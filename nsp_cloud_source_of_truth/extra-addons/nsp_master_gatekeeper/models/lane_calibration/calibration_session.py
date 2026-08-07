# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError
from odoo.addons.nsp_core.utils import new_management_code

import re


_RAW_TID_SEPARATORS = re.compile(r"[\s:\-]+")
_RAW_TID_PATTERN = re.compile(r"^[0-9A-F]+$")


def _normalize_raw_tid_value(value):
    """Canonicalize an arbitrary raw RFID TID without whitelist/assignment lookup."""
    text = str(value or "").strip().upper()
    if text.startswith("0X"):
        text = text[2:]
    text = _RAW_TID_SEPARATORS.sub("", text)
    if not text:
        return ""
    if not _RAW_TID_PATTERN.fullmatch(text):
        raise ValueError("invalid_raw_tid")
    return text


def _new_measurement_code():
    return new_management_code("MSR")


class NspMeasurementSession(models.Model):
    """Measurement plan shared by Cloud, Edge and one-or-more Controllers.

    The Session owns exactly one arbitrary raw Calibration TID and a list of Reader lines.
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
        string="Calibration Tag",
        copy=True,
    )
    target_count = fields.Integer(string="Calibration Tags", compute="_compute_scope_counts")
    target_tag_count = fields.Integer(string="Calibration Tags", compute="_compute_scope_counts")
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
            session.target_tag_count = len(session.target_line_ids.filtered("tid"))

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

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for source in vals_list:
            values = dict(source)
            if "target_line_ids" in values:
                values["target_line_ids"] = self._sanitize_target_commands(
                    values.get("target_line_ids")
                )
            values["measurement_code"] = str(
                values.get("measurement_code") or _new_measurement_code()
            ).strip().upper()
            values["revision"] = max(int(values.get("revision") or 1), 1)
            if not self.env.context.get("measurement_sync"):
                values["status"] = "draft"
            prepared.append(values)
        records = super().create(prepared)
        records._validate_measurement_scope()
        return records

    @api.constrains("reader_line_ids", "target_line_ids")
    def _check_scope_constraint(self):
        self._validate_measurement_scope()

    def _validate_measurement_scope(self):
        self._validate_calibration_tag_scope()
        self._validate_reader_scope()
        return True

    def action_ready(self):
        return self._set_ready()

    def action_complete(self):
        return self._complete_calibration()

    def action_cancel(self):
        return self._cancel_calibration()

    def action_clear_detection_timeline(self):
        return self._clear_detection_timeline()

    def action_view_events(self):
        self.ensure_one()
        self.check_access("read")
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
        self.check_access("read")
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
        """Compatibility alias for the unified Lane Calibration form.

        Deprecated since NSP 19.0. Removal target: NSP 20.0, after all
        frontend clients use ``action_open_session_form``.
        """
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

    def _popup_action(self, name, res_model, view_xmlids, domain, context=None):
        self.ensure_one()
        views = []
        for xmlid, view_type in view_xmlids:
            views.append((self.env.ref(xmlid).id, view_type))
        return {
            "type": "ir.actions.act_window",
            "name": name,
            "res_model": res_model,
            "view_mode": ",".join(view_type for _xmlid, view_type in view_xmlids),
            "views": views,
            "domain": domain,
            "target": "new",
            "context": {
                **dict(self.env.context),
                "active_model": self._name,
                "active_id": self.id,
                "active_ids": self.ids,
                "default_session_id": self.id,
                **dict(context or {}),
            },
        }

    def action_open_calibration_tag_card(self):
        """Open the single raw Calibration Tag through the parent session."""
        self.ensure_one()
        self.check_access("read")
        view = self.env.ref(
            "nsp_master_gatekeeper.view_nsp_measurement_session_vehicles_popup_form"
        )
        return {
            "type": "ir.actions.act_window",
            "name": _("Calibration Tag"),
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "views": [(view.id, "form")],
            "target": "new",
            "context": {
                **dict(self.env.context),
                "active_model": self._name,
                "active_id": self.id,
                "active_ids": self.ids,
                "default_session_id": self.id,
                "form_view_initial_mode": "edit" if self.status == "draft" else "readonly",
            },
        }

    def action_open_calibration_tag_coverage(self):
        self.ensure_one()
        self.check_access("read")
        return self._popup_action(
            _("Calibration Tag Coverage"),
            "nsp.measurement.target.line",
            [("nsp_master_gatekeeper.view_nsp_measurement_target_line_coverage_list", "list")],
            [("session_id", "=", self.id)],
            {"create": False, "edit": False, "delete": False},
        )

    def action_open_vehicles_card(self):
        """Deprecated compatibility alias. Removal target: NSP 20.0."""
        self.ensure_one()
        return self.action_open_calibration_tag_card()

    def action_open_rfid_coverage_card(self):
        """Deprecated compatibility alias. Removal target: NSP 20.0."""
        self.ensure_one()
        return self.action_open_calibration_tag_coverage()

    def action_open_infrastructure_card(self):
        self.ensure_one()
        self.check_access("read")
        editable = self.status == "draft"
        list_xmlid = (
            "nsp_master_gatekeeper.view_nsp_measurement_reader_line_scope_edit_list"
            if editable
            else "nsp_master_gatekeeper.view_nsp_measurement_reader_line_scope_list"
        )
        views = [(list_xmlid, "list")]
        if editable:
            views.append(("nsp_master_gatekeeper.view_nsp_measurement_reader_line_form", "form"))
        return self._popup_action(
            _("Infrastructure Scope"),
            "nsp.measurement.reader.line",
            views,
            [("session_id", "=", self.id)],
            {
                "default_session_id": self.id,
                "create": editable,
                "edit": editable,
                "delete": editable,
                "form_view_ref": "nsp_master_gatekeeper.view_nsp_measurement_reader_line_form",
            },
        )

    def action_open_infrastructure_reader(self, reader_line_id=False):
        """Open one Infrastructure Scope Reader Assembly in an Odoo 19 dialog."""
        self.ensure_one()
        self.check_access("read")
        try:
            line_id = int(reader_line_id or 0)
        except (TypeError, ValueError) as exc:
            raise ValidationError(_("Invalid Reader Assembly.")) from exc
        line = self.env["nsp.measurement.reader.line"]
        if line_id:
            line = line.browse(line_id).exists()
            if not line or line.session_id != self:
                raise ValidationError(_("Reader Assembly does not belong to this Lane Calibration."))
        if self.status != "draft" and not line:
            raise ValidationError(_("Infrastructure Scope can be changed only while Lane Calibration is Draft."))

        form_view = self.env.ref(
            "nsp_master_gatekeeper.view_nsp_measurement_reader_line_form"
        )
        return {
            "type": "ir.actions.act_window",
            "name": _("Reader Assembly") if line else _("New Reader Assembly"),
            "res_model": "nsp.measurement.reader.line",
            "res_id": line.id if line else False,
            "view_mode": "form",
            "views": [(form_view.id, "form")],
            "target": "new",
            "context": {
                **dict(self.env.context),
                "active_model": self._name,
                "active_id": self.id,
                "active_ids": self.ids,
                "default_session_id": self.id,
                "default_reader_port_ids": [(0, 0, {"port_no": 1})],
                "form_view_initial_mode": "edit" if self.status == "draft" else "readonly",
                "create": self.status == "draft",
                "edit": self.status == "draft",
                "delete": self.status == "draft",
            },
        }

    def action_remove_infrastructure_reader(self, reader_line_id):
        """Remove one scoped Reader Assembly after ownership and state checks."""
        self.ensure_one()
        self.check_access("write")
        if self.status != "draft":
            raise ValidationError(_("Infrastructure Scope can be changed only while Lane Calibration is Draft."))
        try:
            line_id = int(reader_line_id or 0)
        except (TypeError, ValueError) as exc:
            raise ValidationError(_("Invalid Reader Assembly.")) from exc
        line = self.env["nsp.measurement.reader.line"].browse(line_id).exists()
        if not line or line.session_id != self:
            raise ValidationError(_("Reader Assembly does not belong to this Lane Calibration."))
        line.unlink()
        return True

    def action_open_apply_configuration(self, selected_event_ids):
        self.ensure_one()
        self.check_access("read")
        if self.status not in ("ready", "running", "completed"):
            raise ValidationError(_(
                "Lane configuration can be applied only from a released or running Lane Calibration."
            ))
        selected, edge_server_id, controller_id = self._configuration_steps_from_selection(
            selected_event_ids
        )
        checkin = " → ".join("%s:P%s" % (row["reader_name"], row["port_no"]) for row in selected)
        checkout = " → ".join(
            "%s:P%s" % (row["reader_name"], row["port_no"])
            for row in reversed(selected)
        )
        wizard = self.env["nsp.measurement.apply.lane.wizard"].create({
            "session_id": self.id,
            "edge_server_id": edge_server_id,
            "controller_id": controller_id,
            "checkin_overview": checkin,
            "checkout_overview": checkout,
            "line_ids": [
                (0, 0, {
                    "selection_order": row["selection_order"],
                    "event_id": row["event_id"],
                    "reader_id": row["reader_id"],
                    "serial_number": row["serial_number"],
                    "port_no": row["port_no"],
                    "observed_at": row["observed_at"],
                    "observed_at_ms": row["observed_at_ms"],
                    "duration_from_previous": row["duration_from_previous"],
                    "reader_power_dbm": row["reader_power_dbm"],
                    "read_interval_ms": row["read_interval_ms"],
                    "tid_start_address": row["tid_start_address"],
                    "tid_length": row["tid_length"],
                    "checkin_order": row["checkin_order"],
                    "checkout_order": row["checkout_order"],
                })
                for row in selected
            ],
        })
        return {
            "type": "ir.actions.act_window",
            "name": _("Apply Lane Configuration"),
            "res_model": "nsp.measurement.apply.lane.wizard",
            "res_id": wizard.id,
            "view_mode": "form",
            "views": [(
                self.env.ref(
                    "nsp_master_gatekeeper.view_nsp_measurement_apply_lane_wizard_form"
                ).id,
                "form",
            )],
            "target": "new",
            "context": dict(self.env.context),
        }


class NspMeasurementSessionTagScope(models.Model):
    _inherit = "nsp.measurement.session"

    calibration_tid = fields.Char(
        string="Calibration Tag",
        compute="_compute_calibration_tid",
        inverse="_inverse_calibration_tid",
        help=(
            "One arbitrary raw RFID TID used as the Lane Calibration probe. "
            "No RFID whitelist or Vehicle mapping is required."
        ),
    )

    @api.depends("target_line_ids.tid")
    def _compute_calibration_tid(self):
        for session in self:
            session.calibration_tid = session.target_line_ids[:1].tid or ""

    @api.onchange("calibration_tid")
    def _onchange_calibration_tid(self):
        """Normalize scanner input immediately while keeping inverse validation server-side."""
        for session in self:
            if not session.calibration_tid:
                continue
            try:
                session.calibration_tid = _normalize_raw_tid_value(session.calibration_tid)
            except ValueError as exc:
                raise ValidationError(
                    _("Calibration Tag must contain hexadecimal characters only.")
                ) from exc

    def _inverse_calibration_tid(self):
        Target = self.env["nsp.measurement.target.line"]
        for session in self:
            if session.status != "draft" and not self.env.context.get("measurement_sync"):
                raise ValidationError(_("Calibration Tag can be changed only while Lane Calibration is Draft."))
            session.check_access("write")
            try:
                tid = _normalize_raw_tid_value(session.calibration_tid)
            except ValueError as exc:
                raise ValidationError(_("Calibration Tag must contain hexadecimal characters only.")) from exc
            targets = session.target_line_ids.sorted("id")
            primary = targets[:1]
            extras = targets[1:]
            if extras:
                extras.unlink()
            if not tid:
                if primary:
                    primary.unlink()
                continue
            if primary:
                if primary.tid != tid:
                    primary.write({"tid": tid})
            else:
                Target.create({"session_id": session.id, "tid": tid})

    def _sanitize_target_commands(self, commands):
        """Normalize the single raw calibration tag configured on a session."""
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
        seen_tids = set(existing.mapped("tid"))

        cleaned = []
        resulting_count = len(existing)
        for command in commands:
            if not isinstance(command, (list, tuple)) or not command:
                cleaned.append(command)
                continue
            operation = command[0]
            if operation == 0 and len(command) >= 3:
                values = dict(command[2] or {})
                tid = Target._normalize_tid(values.get("tid"))
                if not tid:
                    continue
                if tid in seen_tids:
                    raise ValidationError(_("The same raw TID can be used only once."))
                resulting_count += 1
                if resulting_count > 1:
                    raise ValidationError(_("Lane Calibration accepts exactly one raw RFID Tag."))
                values["tid"] = tid
                cleaned.append((0, 0, values))
                seen_tids.add(tid)
                continue

            if operation == 1 and len(command) >= 3:
                current = Target.browse(int(command[1] or 0)).exists()
                values = dict(command[2] or {})
                if "tid" in values:
                    current_tid = current.tid if current else ""
                    seen_tids.discard(current_tid)
                    tid = Target._normalize_tid(values.get("tid"))
                    if not tid:
                        raise ValidationError(_("Raw TID is required."))
                    if tid in seen_tids:
                        raise ValidationError(_("The same raw TID can be used only once."))
                    values["tid"] = tid
                    seen_tids.add(tid)
                cleaned.append((1, command[1], values))
                continue

            cleaned.append(command)
        return cleaned

    def _allowed_target_tids(self):
        self.ensure_one()
        return {line.tid for line in self.target_line_ids if line.tid}

    def _validate_calibration_tag_scope(self):
        for session in self:
            targets = session.target_line_ids
            if len(targets) > 1:
                raise ValidationError(_("Lane Calibration accepts exactly one raw RFID Tag."))
            if targets.filtered(lambda line: not line.tid):
                raise ValidationError(_("Raw TID is required for Lane Calibration."))
        return True


class NspMeasurementTargetLine(models.Model):
    """One arbitrary raw RFID tag used only as the Lane Calibration probe."""

    _name = "nsp.measurement.target.line"
    _description = "NSP Lane Calibration Raw Tag"
    _order = "session_id, id"

    session_id = fields.Many2one(
        "nsp.measurement.session",
        required=False,
        ondelete="cascade",
        index=True,
    )
    tid = fields.Char(
        string="Raw TID",
        required=True,
        index=True,
        help=(
            "Arbitrary raw RFID TID used for calibration. The tag does not need to exist "
            "in RFID Tag Whitelist and is not mapped to a Vehicle or User."
        ),
    )
    detection_state = fields.Selection(
        [("pending", "Not Detected"), ("detected", "Detected")],
        compute="_compute_detection_state",
        string="Detection",
    )
    detection_count = fields.Integer(
        compute="_compute_detection_state",
        string="Reads",
    )

    # Database-compatibility fields from the former Vehicle-based calibration model.
    # They are intentionally optional and are never read or populated by the raw-TID
    # Lane Calibration workflow. Keeping them through NSP 19.x lets module upgrade
    # remove the legacy NOT NULL constraints without renaming/recreating the table.
    # Removal target: NSP 20.0 via an explicit migration.
    tag_id = fields.Many2one(
        "nsp.rfid.tag",
        string="Deprecated RFID Tag",
        required=False,
        ondelete="restrict",
        copy=False,
    )
    vehicle_id = fields.Many2one(
        "nsp.vehicle",
        string="Deprecated Vehicle",
        required=False,
        ondelete="restrict",
        copy=False,
    )

    # Compatibility aliases retained only for callers from NSP 19.0.x.
    # Removal target: NSP 20.0. These aliases do not perform Vehicle/Whitelist lookup.
    vehicle_tid = fields.Char(related="tid", string="Deprecated TID Alias", readonly=False)

    _sql_constraints = [
        (
            "measurement_target_tid_unique",
            "unique(session_id, tid)",
            "This raw TID is already selected in the Lane Calibration.",
        ),
    ]

    @api.model
    def _normalize_tid(self, value):
        try:
            return _normalize_raw_tid_value(value)
        except ValueError as exc:
            raise ValidationError(_("Raw TID must contain hexadecimal characters only.")) from exc

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
                line.tid,
            ), 0)
            line.detection_count = count
            line.detection_state = "detected" if count else "pending"

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for source in vals_list:
            values = dict(source)
            values["tid"] = self._normalize_tid(values.get("tid"))
            if not values["tid"]:
                raise ValidationError(_("Raw TID is required."))
            prepared.append(values)
        return super().create(prepared)

    def write(self, vals):
        values = dict(vals)
        if "tid" in values:
            values["tid"] = self._normalize_tid(values.get("tid"))
            if not values["tid"]:
                raise ValidationError(_("Raw TID is required."))
        return super().write(values)

    @api.constrains("tid", "session_id")
    def _check_raw_tag(self):
        if self.filtered(lambda line: not line.tid):
            raise ValidationError(_("Raw TID is required."))
        self.mapped("session_id")._validate_calibration_tag_scope()
