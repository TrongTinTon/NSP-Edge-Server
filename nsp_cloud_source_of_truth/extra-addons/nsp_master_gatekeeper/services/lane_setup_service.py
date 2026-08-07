# -*- coding: utf-8 -*-
"""Persist Lane device configuration and one explicit Lane In/Lane Out path."""

from odoo import _, fields
from odoo.exceptions import ValidationError


class LaneSetupService:
    """Save Lane Setup without modifying Lane Calibration observation data."""

    def __init__(self, env):
        self.env = env

    def save(self, wizard, apply_setup=True):
        wizard.ensure_one()
        direction_lines = wizard.direction_line_ids.sorted(
            lambda row: (row.sequence or 0, row.id)
        )
        device_lines = wizard.device_line_ids
        lane = self._validate_input(wizard, device_lines, direction_lines)

        # Lane Calibration is the hardware/antenna allowlist only. Lane In and
        # Lane Out are independent operator-defined paths and may use any
        # calibrated Reader/Port in any order with their own durations.
        effective_device_lines = device_lines

        values = {
            "reader_config_ids": self._build_device_commands(
                wizard, effective_device_lines
            ),
            "timeline_line_ids": self._build_calibration_scope_commands(wizard),
            "setup_state": "applied" if apply_setup else "draft",
            "setup_applied_at": fields.Datetime.now() if apply_setup else False,
        }

        direction_commands = self._build_direction_commands(
            wizard.direction, direction_lines
        )
        if wizard.direction == "lane_in":
            values["checkin_sequence_ids"] = direction_commands
        else:
            values["checkout_sequence_ids"] = direction_commands

        lane.with_context(
            skip_lane_reader_config_sync=True,
            lane_setup=True,
        ).write(values)
        lane._validate_lane_assembly()
        lane._validate_timeline_and_sequences()
        lane._validate_reader_configs()

        direction_label = dict(wizard._fields["direction"].selection).get(
            wizard.direction, wizard.direction
        )
        action_label = _("applied") if apply_setup else _("saved as draft")
        wizard.session_id.message_post(
            body=_(
                "Lane Setup %(action)s for %(lane)s · %(direction)s with "
                "%(points)s Antenna points and %(readers)s Reader configuration(s). "
                "Lane Calibration observation data was not modified."
            ) % {
                "action": action_label,
                "direction": direction_label,
                "lane": lane.display_name,
                "points": len(direction_lines),
                "readers": len(effective_device_lines),
            }
        )
        return {
            "type": "ir.actions.act_window_close",
            "infos": {
                "refresh_lane_calibration": True,
                "lane_id": lane.id,
                "lane_name": lane.display_name,
                "direction": wizard.direction,
                "setup_state": lane.setup_state,
            },
        }

    def apply(self, wizard):
        """Compatibility alias. New callers should use save(..., apply_setup=True)."""
        return self.save(wizard, apply_setup=True)

    @staticmethod
    def _validate_input(wizard, device_lines, direction_lines):
        if not wizard.lane_id:
            raise ValidationError(_("Select a Lane."))
        if wizard.direction not in ("lane_in", "lane_out"):
            raise ValidationError(_("Select Lane In or Lane Out."))
        if len(direction_lines) < 2:
            raise ValidationError(_("Lane Setup requires at least two Antenna points."))
        if not device_lines:
            raise ValidationError(_("Lane Setup requires Device Configuration."))

        lane = wizard.lane_id
        if not lane.parking_area_id:
            raise ValidationError(_("The selected Lane is not assigned to a Parking Layout."))
        if lane.parking_area_id.state != "draft":
            raise ValidationError(_("Lane Setup can be changed only while Parking Layout is Draft."))
        if lane.edge_server_id != wizard.edge_server_id or lane.controller_id != wizard.controller_id:
            raise ValidationError(_(
                "The selected Lane must use the same Server and Controller as Lane Calibration."
            ))

        calibrated_keys = {
            (reader_line.reader_id.id, int(port.port_no or 0))
            for reader_line in wizard.session_id.reader_line_ids
            for port in reader_line.reader_port_ids
            if reader_line.reader_id and int(port.port_no or 0) > 0
        }
        calibrated_reader_ids = {reader_id for reader_id, _port in calibrated_keys}
        if not calibrated_keys:
            raise ValidationError(_(
                "Lane Calibration has no configured Reader/Antenna scope."
            ))

        point_keys = [
            (line.reader_id.id, int(line.port_no or 0))
            for line in direction_lines
        ]
        if any(not reader_id for reader_id, _port in point_keys):
            raise ValidationError(_("Every Antenna requires a Reader."))
        if any(port < 1 or port > 16 for _reader_id, port in point_keys):
            raise ValidationError(_("Every Antenna/Port must be between 1 and 16."))
        if len(point_keys) != len(set(point_keys)):
            raise ValidationError(_("Each Antenna can appear only once in a Lane Direction."))
        outside_calibration = [key for key in point_keys if key not in calibrated_keys]
        if outside_calibration:
            raise ValidationError(_(
                "Lane Direction can use only Readers and Antennas configured in this Lane Calibration."
            ))
        if int(direction_lines[0].duration_ms or 0) != 0:
            raise ValidationError(_("The first Antenna must use 0 ms Max Duration."))
        for line in direction_lines[1:]:
            if int(line.duration_ms or 0) <= 0:
                raise ValidationError(_(
                    "Every Antenna after the first must have a positive Max Duration."
                ))

        configured_reader_ids = set(device_lines.mapped("reader_id").ids)
        outside_reader_ids = configured_reader_ids - calibrated_reader_ids
        if outside_reader_ids:
            raise ValidationError(_(
                "Device Configuration can use only Readers configured in this Lane Calibration."
            ))
        direction_reader_ids = set(direction_lines.mapped("reader_id").ids)
        missing_reader_ids = direction_reader_ids - configured_reader_ids
        if missing_reader_ids:
            raise ValidationError(_(
                "Device Configuration is missing one or more Readers used by Direction."
            ))
        for line in device_lines.filtered(
            lambda item: item.reader_id.id in direction_reader_ids
        ):
            if line.power_dbm < 0 or line.power_dbm > 40:
                raise ValidationError(_("Reader Power must be between 0 and 40 dBm."))
            if line.read_interval_ms <= 0 or line.read_interval_ms > 60000:
                raise ValidationError(_("Read Interval must be between 1 and 60000 ms."))
            if line.tid_start_address < 0:
                raise ValidationError(_("TID Start cannot be negative."))
            if line.tid_length <= 0:
                raise ValidationError(_("TID Length must be greater than zero."))
        return lane

    @staticmethod
    def _build_calibration_scope_commands(wizard):
        """Persist the calibrated Reader/Port allowlist on the Lane.

        ``nsp.parking.lane.timeline`` is retained as an NSP 19.x compatibility
        container for the allowed physical Reader/Ports. Direction order and
        timing are authoritative on each Lane In/Lane Out event sequence.
        """
        rows = []
        seen = set()
        for reader_line in wizard.session_id.reader_line_ids.sorted(
            lambda row: (row.reader_id.id, row.id)
        ):
            for port in reader_line.reader_port_ids.sorted(
                lambda row: (int(row.port_no or 0), row.id)
            ):
                key = (reader_line.reader_id.id, int(port.port_no or 0))
                if not key[0] or key[1] <= 0 or key in seen:
                    continue
                seen.add(key)
                rows.append(key)
        commands = [(5, 0, 0)]
        for index, (reader_id, port_no) in enumerate(rows, start=1):
            commands.append((0, 0, {
                "sequence": index,
                "reader_id": reader_id,
                "port_no": port_no,
                # Compatibility field only. Direction timing is stored on the
                # Lane In/Lane Out sequence step, not on this allowlist.
                "duration_from_previous": 0.0,
            }))
        return commands

    @staticmethod
    def _build_device_commands(wizard, device_lines):
        now = fields.Datetime.now()
        commands = [(5, 0, 0)]
        for line in device_lines.sorted(lambda row: (row.reader_id.id, row.id)):
            commands.append((0, 0, {
                "reader_id": line.reader_id.id,
                "power_dbm": int(line.power_dbm or 0),
                "read_interval_ms": int(line.read_interval_ms or 200),
                "tid_start_address": int(line.tid_start_address or 0),
                "tid_length": int(line.tid_length or 4),
                "source_type": "lane_calibration",
                "source_reference": wizard.session_id.measurement_code or "",
                "source_revision": int(wizard.session_id.revision or 1),
                "applied_at": now,
            }))
        return commands

    @staticmethod
    def _build_direction_commands(direction, direction_lines):
        technical_sequence_type = {
            "lane_in": "check_in",
            "lane_out": "check_out",
        }[direction]
        commands = [(5, 0, 0)]
        for index, line in enumerate(direction_lines, start=1):
            commands.append((0, 0, {
                # NSP 19.x compatibility: persistence still uses check_in/check_out.
                "sequence_type": technical_sequence_type,
                "sequence": index,
                "reader_id": line.reader_id.id,
                "port_no": int(line.port_no or 0),
                "duration_from_previous": float(int(line.duration_ms or 0)) / 1000.0,
            }))
        return commands
