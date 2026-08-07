# -*- coding: utf-8 -*-
"""Application service that persists an accepted Lane Calibration timeline."""

from odoo import _, fields
from odoo.exceptions import ValidationError

class CalibrationReaderConfigService:
    """Validate selected Reader Ports and build immutable Reader snapshots."""

    @staticmethod
    def validate_selected_scope(session, lines, edge_server, controller):
        scope_by_reader = {
            scope.reader_id.id: scope
            for scope in session.reader_line_ids
        }
        for line in lines:
            scope = scope_by_reader.get(line.reader_id.id)
            if not scope:
                raise ValidationError(_(
                    "Selected Reader %(reader)s is no longer part of this Lane Calibration Infrastructure Scope."
                ) % {"reader": line.reader_id.display_name})
            if scope.edge_server_id != edge_server or scope.controller_id != controller:
                raise ValidationError(_(
                    "Selected Reader %(reader)s does not belong to the Server and Controller captured by this configuration."
                ) % {"reader": line.reader_id.display_name})
            allowed_ports = {int(port.port_no or 0) for port in scope.reader_port_ids}
            if int(line.port_no or 0) not in allowed_ports:
                raise ValidationError(_(
                    "Reader Port %(reader)s:P%(port)s is no longer part of this Lane Calibration Infrastructure Scope."
                ) % {
                    "reader": line.reader_id.display_name,
                    "port": int(line.port_no or 0),
                })
        return scope_by_reader

    @staticmethod
    def build_commands(session, lines, scope_by_reader, applied_at=None):
        applied_at = applied_at or fields.Datetime.now()
        commands = [(5, 0, 0)]
        for reader in lines.mapped("reader_id"):
            scope = scope_by_reader[reader.id]
            commands.append((0, 0, {
                "reader_id": reader.id,
                "power_dbm": int(scope.reader_power_dbm or 0),
                "read_interval_ms": int(scope.read_interval_ms or 200),
                "tid_start_address": int(scope.reader_tid_addr or 0),
                "tid_length": int(scope.reader_tid_len or 4),
                "source_type": "lane_calibration",
                "source_reference": session.measurement_code or "",
                "source_revision": int(session.revision or 1),
                "applied_at": applied_at,
            }))
        return commands



class CalibrationApplyService:
    """Apply a wizard selection to one existing Parking Lane atomically."""

    def __init__(self, env):
        self.env = env

    def apply(self, wizard):
        wizard.ensure_one()
        lines = wizard.line_ids.sorted("selection_order")
        lane = self._validate_input(wizard, lines)
        scope_by_reader = CalibrationReaderConfigService.validate_selected_scope(
            wizard.session_id,
            lines,
            wizard.edge_server_id,
            wizard.controller_id,
        )
        now = fields.Datetime.now()
        lane_values = self._build_lane_values(
            wizard.session_id,
            lines,
            scope_by_reader,
            now,
        )
        lane.with_context(
            skip_lane_reader_config_sync=True,
            lane_calibration_apply=True,
        ).write(lane_values)
        lane._validate_lane_assembly()
        lane._validate_timeline_and_sequences()
        lane._validate_reader_configs()
        if lane.parking_area_id.state != "draft":
            lane.parking_area_id._apply_parking_state_transition("draft")

        wizard.session_id._apply_status_transition("applied", {
            "ended_at": wizard.session_id.ended_at or now,
            "applied_at": now,
        })
        wizard.session_id.message_post(
            body=_(
                "Lane configuration applied to %(lane)s with %(count)s timeline points and "
                "%(readers)s Reader configuration snapshot(s). The Lane stores only the "
                "Calibration code/revision as audit text and no relational reference."
            ) % {
                "lane": lane.display_name,
                "count": len(lines),
                "readers": len(lines.mapped("reader_id")),
            }
        )
        return {
            "type": "ir.actions.act_window_close",
            "infos": {
                "refresh_lane_calibration": True,
                "session_id": wizard.session_id.id,
                "lane_id": lane.id,
                "lane_name": lane.display_name,
            },
        }

    @staticmethod
    def _validate_input(wizard, lines):
        if not wizard.parking_area_id:
            raise ValidationError(_(
                "Select a Parking Layout before saving the Lane configuration."
            ))
        if not wizard.lane_id:
            raise ValidationError(_(
                "Select an existing Lane or create a new Lane before saving."
            ))
        if len(lines) < 2:
            raise ValidationError(_("Select at least two Detection Timeline rows."))
        lane = wizard.lane_id
        if lane.parking_area_id != wizard.parking_area_id:
            raise ValidationError(_(
                "The selected Lane does not belong to the selected Parking Layout."
            ))
        if (
            lane.edge_server_id != wizard.edge_server_id
            or lane.controller_id != wizard.controller_id
        ):
            raise ValidationError(_(
                "The selected Lane must use the same Server and Controller as the selected timeline."
            ))
        return lane

    @staticmethod
    def _build_lane_values(session, lines, scope_by_reader, applied_at):
        timeline_commands = [(5, 0, 0)]
        checkin_commands = [(5, 0, 0)]
        checkout_commands = [(5, 0, 0)]
        for index, line in enumerate(lines, start=1):
            timeline_commands.append((0, 0, {
                "sequence": index,
                "reader_id": line.reader_id.id,
                "port_no": int(line.port_no or 0),
                "duration_from_previous": (
                    0.0
                    if index == 1
                    else float(line.duration_from_previous or 0.001)
                ),
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
        return {
            "timeline_line_ids": timeline_commands,
            "reader_config_ids": CalibrationReaderConfigService.build_commands(
                session,
                lines,
                scope_by_reader,
                applied_at=applied_at,
            ),
            "checkin_sequence_ids": checkin_commands,
            "checkout_sequence_ids": checkout_commands,
        }
