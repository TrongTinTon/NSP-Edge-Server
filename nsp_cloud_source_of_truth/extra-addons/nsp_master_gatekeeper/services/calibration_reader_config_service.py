# -*- coding: utf-8 -*-
"""Reader configuration preparation for applying Lane Calibration."""

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
