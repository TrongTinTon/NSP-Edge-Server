# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError


def _event_seconds(event):
    value = fields.Datetime.to_datetime(event.read_at)
    if not value:
        return 0.0
    return value.timestamp() + (int(event.read_at_ms or 0) / 1000.0)


class NspMeasurementSessionLaneApplication(models.Model):
    _inherit = "nsp.measurement.session"

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

    def action_open_vehicles_card(self):
        """Open Vehicles through the parent Calibration record.

        Odoo 19 applies One2many commands, onchange values and required-field
        validation reliably only when the lines are edited through their parent
        form.  A standalone editable list of ``nsp.measurement.target.line`` can
        lose the parent/session context while a Many2one item is selected.
        """
        self.ensure_one()
        view = self.env.ref(
            "nsp_master_gatekeeper.view_nsp_measurement_session_vehicles_popup_form"
        )
        return {
            "type": "ir.actions.act_window",
            "name": _("Vehicles"),
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

    def action_open_rfid_coverage_card(self):
        self.ensure_one()
        return self._popup_action(
            _("RFID Coverage"),
            "nsp.measurement.target.line",
            [("nsp_master_gatekeeper.view_nsp_measurement_target_line_coverage_list", "list")],
            [("session_id", "=", self.id)],
            {"create": False, "edit": False, "delete": False},
        )

    def action_open_infrastructure_card(self):
        self.ensure_one()
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

    def action_clear_detection_timeline(self):
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
        except Exception as exc:
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
        for selection_order, event_id in enumerate(ordered_ids, start=1):
            step = step_by_event.get(event_id)
            event = event_by_id.get(event_id)
            if not step or not event:
                raise ValidationError(_(
                    "A selected Detection Timeline row is no longer available. Refresh and select again."
                ))
            reader_line = self._measurement_line_for_serial(step.get("serial_number"))
            if not reader_line:
                raise ValidationError(_("A selected detection does not belong to the current Infrastructure Scope."))
            reader = reader_line.reader_id
            port_no = int(step.get("port_no") or 0)
            point_key = (reader.id, port_no)
            if point_key in point_keys:
                raise ValidationError(_(
                    "Select each Reader Port only once when building a Lane configuration."
                ))
            point_keys.add(point_key)
            controller_ids.add(reader_line.controller_id.id)
            edge_ids.add(reader_line.edge_server_id.id)
            current_seconds = _event_seconds(event)
            duration = 0.0 if previous_seconds is None else max(abs(current_seconds - previous_seconds), 0.001)
            previous_seconds = current_seconds
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
                "checkin_order": selection_order,
                "checkout_order": len(ordered_ids) - selection_order + 1,
            })

        if len(controller_ids) != 1 or len(edge_ids) != 1:
            raise ValidationError(_(
                "A Lane must be built from Detection Timeline rows belonging to one Server and one Controller."
            ))
        return selected, next(iter(edge_ids)), next(iter(controller_ids))

    def action_open_apply_configuration(self, selected_event_ids):
        self.ensure_one()
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


class NspMeasurementApplyLaneWizard(models.TransientModel):
    _name = "nsp.measurement.apply.lane.wizard"
    _description = "Apply Lane Calibration Timeline"

    session_id = fields.Many2one(
        "nsp.measurement.session", required=True, readonly=True, ondelete="cascade"
    )
    parking_area_id = fields.Many2one(
        "nsp.parking.area", string="Parking Layout", ondelete="cascade"
    )
    edge_server_id = fields.Many2one(
        "nsp.edge.server", string="Server", required=True, readonly=True
    )
    controller_id = fields.Many2one(
        "nsp.controller", string="Controller", required=True, readonly=True
    )
    lane_id = fields.Many2one(
        "nsp.parking.lane", string="Lane", ondelete="cascade"
    )
    line_ids = fields.One2many(
        "nsp.measurement.apply.lane.wizard.line", "wizard_id", string="Selected Timeline"
    )
    selected_count = fields.Integer(compute="_compute_selected_count")
    checkin_overview = fields.Text(string="Check-in", readonly=True)
    checkout_overview = fields.Text(string="Check-out", readonly=True)

    @api.depends("line_ids")
    def _compute_selected_count(self):
        for wizard in self:
            wizard.selected_count = len(wizard.line_ids)

    @api.onchange("parking_area_id")
    def _onchange_parking_area_id(self):
        for wizard in self:
            if wizard.lane_id and wizard.lane_id.parking_area_id != wizard.parking_area_id:
                wizard.lane_id = False

    @api.onchange("lane_id")
    def _onchange_lane_id(self):
        for wizard in self:
            if wizard.lane_id:
                wizard.parking_area_id = wizard.lane_id.parking_area_id

    def _validate_selected_infrastructure_scope(self, lines):
        """Validate transient selections against the Calibration snapshot.

        A saved Lane owns Server and Controller directly.  Reader.controller_id
        is runtime inventory and must not be used as the ownership source for
        applying a Calibration.  Before the transient Calibration can be
        discarded, validate every selected Reader/Port against its immutable
        Reader Assembly in this session.
        """
        self.ensure_one()
        scope_by_reader = {
            scope.reader_id.id: scope
            for scope in self.session_id.reader_line_ids
        }
        for line in lines:
            scope = scope_by_reader.get(line.reader_id.id)
            if not scope:
                raise ValidationError(_(
                    "Selected Reader %(reader)s is no longer part of this Lane Calibration Infrastructure Scope."
                ) % {"reader": line.reader_id.display_name})
            if (
                scope.edge_server_id != self.edge_server_id
                or scope.controller_id != self.controller_id
            ):
                raise ValidationError(_(
                    "Selected Reader %(reader)s does not belong to the Server and Controller captured by this configuration."
                ) % {"reader": line.reader_id.display_name})
            allowed_ports = {
                int(port.port_no or 0)
                for port in scope.reader_port_ids
            }
            if int(line.port_no or 0) not in allowed_ports:
                raise ValidationError(_(
                    "Reader Port %(reader)s:P%(port)s is no longer part of this Lane Calibration Infrastructure Scope."
                ) % {
                    "reader": line.reader_id.display_name,
                    "port": int(line.port_no or 0),
                })
        return True

    def action_save_configuration(self):
        self.ensure_one()
        if not self.parking_area_id:
            raise ValidationError(_("Select a Parking Layout before saving the Lane configuration."))
        if not self.lane_id:
            raise ValidationError(_("Select an existing Lane or create a new Lane before saving."))
        lines = self.line_ids.sorted("selection_order")
        if len(lines) < 2:
            raise ValidationError(_("Select at least two Detection Timeline rows."))
        lane = self.lane_id
        if lane.parking_area_id != self.parking_area_id:
            raise ValidationError(_("The selected Lane does not belong to the selected Parking Layout."))
        if lane.edge_server_id != self.edge_server_id or lane.controller_id != self.controller_id:
            raise ValidationError(_(
                "The selected Lane must use the same Server and Controller as the selected timeline."
            ))
        self._validate_selected_infrastructure_scope(lines)

        timeline_commands = [(5, 0, 0)]
        checkin_commands = [(5, 0, 0)]
        checkout_commands = [(5, 0, 0)]
        for index, line in enumerate(lines, start=1):
            timeline_commands.append((0, 0, {
                "sequence": index,
                "reader_id": line.reader_id.id,
                "port_no": int(line.port_no or 0),
                "duration_from_previous": 0.0 if index == 1 else float(line.duration_from_previous or 0.001),
            }))
            checkin_commands.append((0, 0, {
                "sequence_type": "check_in",
                "sequence": index,
                "reader_id": line.reader_id.id,
                "port_no": int(line.port_no or 0),
            }))
        for index, line in enumerate(lines[::-1], start=1):
            checkout_commands.append((0, 0, {
                "sequence_type": "check_out",
                "sequence": index,
                "reader_id": line.reader_id.id,
                "port_no": int(line.port_no or 0),
            }))

        lane.write({
            "timeline_line_ids": timeline_commands,
            "checkin_sequence_ids": checkin_commands,
            "checkout_sequence_ids": checkout_commands,
        })
        lane._validate_lane_assembly()
        lane._validate_timeline_and_sequences()
        if lane.parking_area_id.state != "draft":
            # The last published snapshot remains active, but the changed working
            # layout must be published explicitly as a new revision.
            lane.parking_area_id.write({"state": "draft"})

        now = fields.Datetime.now()
        self.session_id.with_context(measurement_sync=True).write({
            "status": "applied",
            "ended_at": self.session_id.ended_at or now,
            "applied_at": now,
        })
        self.session_id.message_post(
            body=_(
                "Lane configuration applied to %(lane)s with %(count)s timeline points. "
                "The Lane stores no reference to this Calibration."
            ) % {"lane": lane.display_name, "count": len(lines)}
        )
        return {
            "type": "ir.actions.act_window_close",
            "infos": {
                "refresh_lane_calibration": True,
                "session_id": self.session_id.id,
                "lane_id": lane.id,
                "lane_name": lane.display_name,
            },
        }


class NspMeasurementApplyLaneWizardLine(models.TransientModel):
    _name = "nsp.measurement.apply.lane.wizard.line"
    _description = "Selected Lane Calibration Detection"
    _order = "selection_order, id"

    wizard_id = fields.Many2one(
        "nsp.measurement.apply.lane.wizard", required=True, ondelete="cascade", index=True
    )
    selection_order = fields.Integer(string="#", required=True, readonly=True)
    event_id = fields.Many2one("nsp.measurement.event", readonly=True, ondelete="set null")
    reader_id = fields.Many2one("nsp.device", string="Reader", required=True, readonly=True)
    serial_number = fields.Char(string="Serial", readonly=True)
    port_no = fields.Integer(string="Port", required=True, readonly=True)
    observed_at = fields.Datetime(string="Detected", readonly=True)
    observed_at_ms = fields.Integer(string="ms", readonly=True)
    duration_from_previous = fields.Float(string="Interval (s)", readonly=True, digits=(8, 3))
    checkin_order = fields.Integer(string="Check-in #", readonly=True)
    checkout_order = fields.Integer(string="Check-out #", readonly=True)
