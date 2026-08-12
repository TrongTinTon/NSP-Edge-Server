# -*- coding: utf-8 -*-
"""Persist contextual Lane configuration into one Parking Layout."""

from odoo import _, fields
from odoo.exceptions import ValidationError


class LaneSetupService:
    """Persist Layout-Lane runtime configuration without mutating Lane master identity."""

    def __init__(self, env):
        self.env = env

    def save(self, wizard):
        wizard.ensure_one()
        sequence_lines = wizard.sequence_line_ids.sorted(
            lambda row: (row.sequence or 0, row.id)
        )
        device_lines = wizard.device_line_ids
        self._normalize_first_duration(sequence_lines)
        lane = wizard.lane_id.exists()
        if not lane:
            raise ValidationError(_("Select an existing Lane."))
        lane.check_access("read")
        self._validate_input(wizard, device_lines, sequence_lines)

        layout_lane = self._resolve_layout_lane(wizard, lane)
        used_reader_ids = set(sequence_lines.mapped("reader_id").ids)
        effective_device_lines = device_lines.filtered(
            lambda line: line.reader_id.id in used_reader_ids
        )
        now = fields.Datetime.now()
        infrastructure_values = {
            "edge_server_id": wizard.edge_server_id.id,
            "controller_id": wizard.controller_id.id,
        }
        configuration_values = {
            "reader_config_ids": self._build_device_commands(
                wizard, effective_device_lines, now
            ),
            "antenna_sequence_ids": self._build_sequence_commands(sequence_lines),
            "setup_state": "applied",
            "setup_applied_at": now,
        }

        layout_lane.write(infrastructure_values)
        layout_lane.with_context(
            skip_lane_reader_config_sync=True,
            lane_setup=True,
        ).write(configuration_values)
        layout_lane._normalize_first_sequence_duration()
        layout_lane._validate_lane_assembly()
        layout_lane._validate_antenna_sequence()
        layout_lane._validate_reader_configs()
        wizard.layout_lane_id = layout_lane

        if wizard.session_id:
            wizard.session_id.message_post(
                body=_(
                    "Lane Setup saved for %(lane)s in Parking Layout %(layout)s with "
                    "%(points)s Antenna Sequence points and %(readers)s Reader "
                    "configuration(s). Lane master identity and Calibration evidence "
                    "were not modified."
                ) % {
                    "lane": lane.display_name,
                    "layout": wizard.parking_area_id.display_name,
                    "points": len(sequence_lines),
                    "readers": len(effective_device_lines),
                }
            )
        return {
            "type": "ir.actions.act_window_close",
            "infos": {
                "refresh_lane_calibration": True,
                "lane_id": lane.id,
                "lane_name": lane.display_name,
                "layout_lane_id": layout_lane.id,
                "parking_area_id": wizard.parking_area_id.id,
                "setup_state": layout_lane.setup_state,
            },
        }

    def _resolve_layout_lane(self, wizard, lane):
        LayoutLane = self.env["nsp.parking.layout.lane"]
        layout_lane = wizard.layout_lane_id.exists()
        if layout_lane:
            if (
                layout_lane.parking_area_id != wizard.parking_area_id
                or layout_lane.lane_id != lane
            ):
                raise ValidationError(_(
                    "The selected Lane Configuration does not match the selected Parking Layout and Lane."
                ))
            layout_lane.check_access("write")
            return layout_lane

        layout_lane = LayoutLane.search([
            ("parking_area_id", "=", wizard.parking_area_id.id),
            ("lane_id", "=", lane.id),
        ], limit=1)
        if layout_lane:
            layout_lane.check_access("write")
            return layout_lane

        return LayoutLane.create({
            "parking_area_id": wizard.parking_area_id.id,
            "lane_id": lane.id,
            "edge_server_id": wizard.edge_server_id.id,
            "controller_id": wizard.controller_id.id,
            "setup_state": "draft",
        })

    @staticmethod
    def _normalize_first_duration(sequence_lines):
        if sequence_lines:
            sequence_lines[0].duration_ms = 0

    @staticmethod
    def _validate_input(wizard, device_lines, sequence_lines):
        if not wizard.lane_id:
            raise ValidationError(_("Select a Lane."))
        if not wizard.parking_area_id:
            raise ValidationError(_(
                "Select a Draft Parking Layout to store this Lane Configuration. "
                "The Lane itself remains an independent master."
            ))
        if wizard.parking_area_id.state != "draft":
            raise ValidationError(_(
                "Lane Setup can be changed only while Parking Layout is Draft."
            ))
        if wizard.lane_id.branch_id != wizard.parking_area_id.branch_id:
            raise ValidationError(_(
                "Lane and Parking Layout must belong to the same Branch."
            ))
        if len(sequence_lines) < 2:
            raise ValidationError(_(
                "Lane Setup requires at least two Antenna Sequence points."
            ))
        if not device_lines:
            raise ValidationError(_("Lane Setup requires Device Configuration."))

        LaneSetupService._validate_infrastructure(wizard)
        if wizard.source_scope == "calibration":
            LaneSetupService._validate_calibration_scope(wizard, sequence_lines)
        LaneSetupService._validate_sequence(sequence_lines)
        LaneSetupService._validate_readers(wizard, device_lines, sequence_lines)
        return True

    @staticmethod
    def _validate_infrastructure(wizard):
        edge = wizard.edge_server_id
        controller = wizard.controller_id
        if not edge:
            raise ValidationError(_("Select a Server in Lane Setup."))
        if not controller:
            raise ValidationError(_("Select a Controller in Lane Setup."))
        if (
            not edge.active or not edge.whitelist_id or not edge.whitelist_id.active
            or edge.whitelist_id.device_type_code != "SERVER"
        ):
            raise ValidationError(_(
                "Lane Setup requires an active Server identity from Device Whitelist."
            ))
        if (
            not controller.active or not controller.whitelist_id
            or not controller.whitelist_id.active
            or controller.whitelist_id.device_type_code != "CONTROLLER"
        ):
            raise ValidationError(_(
                "Lane Setup requires an active Controller identity from Device Whitelist."
            ))

    @staticmethod
    def _validate_calibration_scope(wizard, sequence_lines):
        if not wizard.session_id:
            raise ValidationError(_(
                "Lane Calibration scope requires a Calibration session."
            ))
        wizard.session_id.exists().check_access("read")
        server_node = wizard.session_id._server_nodes().filtered(
            lambda node: node.server_id == wizard.edge_server_id
        )[:1]
        controller_node = wizard.session_id._controller_nodes().filtered(
            lambda node: node.controller_id == wizard.controller_id
            and node.parent_id == server_node
        )[:1]
        if not server_node or not controller_node:
            raise ValidationError(_(
                "Lane Setup opened from Lane Calibration must use one "
                "Server/Controller branch from that Calibration Tree."
            ))
        allowed_pairs = wizard._allowed_reader_port_pairs()
        outside_pairs = [
            (line.reader_id.id, int(line.port_no or 0))
            for line in sequence_lines
            if (line.reader_id.id, int(line.port_no or 0)) not in allowed_pairs
        ]
        if outside_pairs:
            raise ValidationError(_(
                "Lane Setup opened from Lane Calibration can use only Reader/Antenna "
                "ports configured in that Calibration."
            ))

    @staticmethod
    def _validate_sequence(sequence_lines):
        point_keys = [
            (line.reader_id.id, int(line.port_no or 0)) for line in sequence_lines
        ]
        if any(not reader_id for reader_id, _port in point_keys):
            raise ValidationError(_("Every Antenna requires a Reader."))
        if any(not 1 <= port <= 16 for _reader_id, port in point_keys):
            raise ValidationError(_("Every Antenna/Port must be between 1 and 16."))
        if len(point_keys) != len(set(point_keys)):
            raise ValidationError(_(
                "Each Reader/Antenna can appear only once in an Antenna Sequence."
            ))
        if any(int(line.duration_ms or 0) <= 0 for line in sequence_lines[1:]):
            raise ValidationError(_(
                "Every Antenna after the first must have a positive Max Duration."
            ))

    @staticmethod
    def _validate_readers(wizard, device_lines, sequence_lines):
        selected_readers = device_lines.mapped("reader_id") | sequence_lines.mapped("reader_id")
        if wizard.source_scope == "calibration":
            available_reader_ids = set(wizard.available_reader_ids.ids)
            if set(selected_readers.ids) - available_reader_ids:
                raise ValidationError(_(
                    "Lane Setup opened from Lane Calibration can use only Readers "
                    "configured in that Calibration."
                ))
        if selected_readers.filtered(lambda reader: not reader.active):
            raise ValidationError(_("Lane Setup can use only active Readers."))
        device_reader_ids = [line.reader_id.id for line in device_lines if line.reader_id]
        if len(device_reader_ids) != len(set(device_reader_ids)):
            raise ValidationError(_(
                "Device Configuration can contain each Reader only once."
            ))
        configured_reader_ids = set(device_lines.mapped("reader_id").ids)
        sequence_reader_ids = set(sequence_lines.mapped("reader_id").ids)
        if sequence_reader_ids - configured_reader_ids:
            raise ValidationError(_(
                "Device Configuration is missing one or more Readers used by Antenna Sequence."
            ))
        for line in device_lines.filtered(lambda item: item.reader_id.id in sequence_reader_ids):
            if not 0 <= line.power_dbm <= 40:
                raise ValidationError(_("Reader Power must be between 0 and 40 dBm."))
            if not 1 <= line.read_interval_ms <= 60000:
                raise ValidationError(_("Read Interval must be between 1 and 60000 ms."))
            if line.tid_start_address < 0:
                raise ValidationError(_("TID Start cannot be negative."))
            if line.tid_length <= 0:
                raise ValidationError(_("TID Length must be greater than zero."))

    @staticmethod
    def _build_device_commands(wizard, device_lines, applied_at):
        commands = [(5, 0, 0)]
        is_calibration = wizard.source_scope == "calibration" and wizard.session_id
        for line in device_lines.sorted(lambda row: (row.reader_id.id, row.id)):
            commands.append((0, 0, {
                "reader_id": line.reader_id.id,
                "power_dbm": int(line.power_dbm or 0),
                "read_interval_ms": int(line.read_interval_ms or 200),
                "tid_start_address": int(line.tid_start_address or 0),
                "tid_length": int(line.tid_length or 4),
                "source_type": "lane_calibration" if is_calibration else "manual",
                "source_reference": wizard.session_id.measurement_code if is_calibration else False,
                "source_revision": int(wizard.session_id.revision or 0) if is_calibration else 0,
                "applied_at": applied_at,
            }))
        return commands

    @staticmethod
    def _build_sequence_commands(sequence_lines):
        commands = [(5, 0, 0)]
        for index, line in enumerate(sequence_lines, start=1):
            commands.append((0, 0, {
                "sequence": index,
                "reader_id": line.reader_id.id,
                "port_no": int(line.port_no or 0),
                "duration_from_previous": (
                    0.0 if index == 1
                    else float(int(line.duration_ms or 0)) / 1000.0
                ),
            }))
        return commands
