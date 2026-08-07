# -*- coding: utf-8 -*-
"""Persist manual Lane In / Lane Out reader-path configuration from observed data."""

from odoo import _
from odoo.exceptions import ValidationError

from .calibration_apply_service import CalibrationReaderConfigService


class LaneDirectionSetupService:
    """Configure one Lane In / Lane Out path without guessing the opposite path."""

    def __init__(self, env):
        self.env = env

    def apply(self, wizard):
        wizard.ensure_one()
        lines = wizard.line_ids.sorted("sequence")
        lane = self._validate_input(wizard, lines)

        scope_by_reader = CalibrationReaderConfigService.validate_selected_scope(
            wizard.session_id,
            lines,
            wizard.edge_server_id,
            wizard.controller_id,
        )

        self._validate_existing_timeline(lane, lines)
        lane_values = self._build_lane_values(
            wizard,
            lane,
            lines,
            scope_by_reader,
        )
        lane.with_context(
            skip_lane_reader_config_sync=True,
            lane_direction_setup=True,
        ).write(lane_values)
        lane._validate_lane_assembly()
        lane._validate_timeline_and_sequences()
        lane._validate_reader_configs()

        direction_label = dict(wizard._fields["direction"].selection).get(
            wizard.direction, wizard.direction
        )
        wizard.session_id.message_post(
            body=_(
                "Lane Direction Setup saved %(direction)s for %(lane)s using "
                "%(count)s observed Detection Timeline points as reference. "
                "Lane Calibration data was not modified."
            ) % {
                "direction": direction_label,
                "lane": lane.display_name,
                "count": len(lines),
            }
        )
        return {
            "type": "ir.actions.act_window_close",
            "infos": {
                "refresh_lane_calibration": True,
                "lane_id": lane.id,
                "lane_name": lane.display_name,
                "direction": wizard.direction,
            },
        }

    @staticmethod
    def _validate_input(wizard, lines):
        if not wizard.lane_id:
            raise ValidationError(_("Select a Lane."))
        if len(lines) < 2:
            raise ValidationError(_("Lane Direction Setup requires at least two observed Reader Ports."))
        if wizard.direction not in ("lane_in", "lane_out"):
            raise ValidationError(_("Select Lane In or Lane Out."))

        lane = wizard.lane_id
        if not lane.parking_area_id:
            raise ValidationError(_("The selected Lane is not assigned to a Parking Layout."))
        if lane.parking_area_id.state != "draft":
            raise ValidationError(_("Lane Direction Setup can be changed only while Parking Layout is Draft."))
        if lane.edge_server_id != wizard.edge_server_id or lane.controller_id != wizard.controller_id:
            raise ValidationError(_(
                "The selected Lane must use the same Server and Controller as the observed Detection Timeline."
            ))
        return lane

    @staticmethod
    def _validate_existing_timeline(lane, lines):
        existing = lane.timeline_line_ids.sorted(lambda row: (row.sequence or 0, row.id))
        if not existing:
            return
        existing_keys = [(row.reader_id.id, int(row.port_no or 0)) for row in existing]
        observed_keys = [(row.reader_id.id, int(row.port_no or 0)) for row in lines]
        if set(existing_keys) != set(observed_keys) or len(existing_keys) != len(observed_keys):
            raise ValidationError(_(
                "Observed Detection Timeline does not match the Reader Ports already configured on this Lane."
            ))
        if observed_keys not in (existing_keys, list(reversed(existing_keys))):
            raise ValidationError(_(
                "Observed path must follow the existing Lane Timeline either forward or reverse."
            ))

    @staticmethod
    def _build_lane_values(wizard, lane, lines, scope_by_reader):
        technical_sequence_type = {
            "lane_in": "check_in",
            "lane_out": "check_out",
        }[wizard.direction]
        direction_commands = [(5, 0, 0)]
        for index, line in enumerate(lines, start=1):
            direction_commands.append((0, 0, {
                # NSP 19.x database/API compatibility: sequence_type still stores
                # check_in/check_out. UI and configuration semantics are Lane In/Lane Out.
                "sequence_type": technical_sequence_type,
                "sequence": index,
                "reader_id": line.reader_id.id,
                "port_no": int(line.port_no or 0),
            }))

        values = {
            "reader_config_ids": CalibrationReaderConfigService.build_commands(
                wizard.session_id,
                lines,
                scope_by_reader,
            ),
        }
        if not lane.timeline_line_ids:
            timeline_commands = [(5, 0, 0)]
            for index, line in enumerate(lines, start=1):
                timeline_commands.append((0, 0, {
                    "sequence": index,
                    "reader_id": line.reader_id.id,
                    "port_no": int(line.port_no or 0),
                    "duration_from_previous": (
                        0.0 if index == 1 else max(float(line.duration_from_previous or 0.0), 0.001)
                    ),
                }))
            values["timeline_line_ids"] = timeline_commands
        if wizard.direction == "lane_in":
            values["checkin_sequence_ids"] = direction_commands
        else:
            values["checkout_sequence_ids"] = direction_commands
        return values
