# -*- coding: utf-8 -*-
from odoo import models, _
from odoo.exceptions import UserError


class NspSyncJobParkingLayout(models.Model):
    """Apply the published Cloud Parking Layout runtime snapshot on Edge."""

    _inherit = "nsp.sync.job"

    def _apply_parking_config(self, item):
        self.ensure_one()
        if not isinstance(item, dict):
            raise UserError(_("Parking Layout item must be an object."))
        unsupported = set(item) - {
            "parking_area_code",
            "parking_area_name",
            "branch_code",
            "state",
            "published_revision",
            "lanes",
        }
        if unsupported:
            raise UserError(
                _("Unsupported Parking Layout field(s): %s")
                % ", ".join(sorted(unsupported))
            )

        branch_code = self._normalize_sync_code(item.get("branch_code"))
        area_code = self._normalize_sync_code(item.get("parking_area_code"))
        if not branch_code or not area_code:
            raise UserError(_("Branch Code and Parking Area Code are required."))
        state = str(item.get("state") or "draft").strip().lower()
        if state not in ("draft", "operational", "maintenance", "blocked"):
            raise UserError(_("Invalid Parking Area state: %s") % state)
        try:
            published_revision = int(item.get("published_revision") or 0)
        except (TypeError, ValueError) as exc:
            raise UserError(_("Published Revision must be an integer.")) from exc
        if published_revision <= 0:
            raise UserError(_("Published Revision is required."))

        branch = self.env["nsp.branch"].sudo().with_context(active_test=False).search(
            [("code", "=", branch_code)], limit=1
        )
        if not branch:
            raise UserError(_("Branch %s was not found in the current snapshot.") % branch_code)

        Parking = self.env["nsp.parking.area"].sudo().with_context(active_test=False)
        parking = Parking.search([("code", "=", area_code)], limit=1)
        parking_values = {
            "code": area_code,
            "name": str(item.get("parking_area_name") or area_code).strip(),
            "branch_id": branch.id,
            "state": state,
            "published_revision": published_revision,
        }
        if parking:
            if int(parking.published_revision or 0) > published_revision:
                return parking
            self._write_changed(parking, parking_values)
        else:
            parking = Parking.create(parking_values)

        controllers = self.env["nsp.controller"].sudo().with_context(active_test=False).search([])
        controller_by_code = {
            self._normalize_sync_code(record.controller_id): record for record in controllers
        }
        antennas = self.env["nsp.device.antenna"].sudo().with_context(active_test=False).search([])
        antenna_by_key = {
            (
                self._normalize_sync_code(record.device_id.serial_number),
                int(record.antenna_no or 0),
            ): record
            for record in antennas
        }
        antenna_by_code = {
            self._normalize_sync_code(record.technical_code): record
            for record in antennas if record.technical_code
        }

        lanes_data = item.get("lanes") or []
        if not isinstance(lanes_data, list):
            raise UserError(_("Parking Lanes must be an array."))

        lane_specs = {}
        timeline_specs = []
        sequence_specs = []
        for lane_item in lanes_data:
            if not isinstance(lane_item, dict):
                raise UserError(_("Parking Lanes must contain objects."))
            supported = {
                "lane_code",
                "lane_name",
                "server_code",
                "controller_code",
                "antenna_timeline",
                "event_sequences",
                "timing_tolerance",
            }
            unsupported_lane = set(lane_item) - supported
            if unsupported_lane:
                raise UserError(
                    _("Unsupported Parking Lane field(s): %s")
                    % ", ".join(sorted(unsupported_lane))
                )

            lane_code = self._normalize_sync_code(lane_item.get("lane_code"))
            controller_code = self._normalize_sync_code(lane_item.get("controller_code"))
            server_code = self._normalize_sync_code(lane_item.get("server_code"))
            if not lane_code or lane_code in lane_specs or not controller_code:
                raise UserError(_("Parking Lane Code and Controller Code are required and must be unique."))
            controller = controller_by_code.get(controller_code)
            if not controller or not controller.active or controller.cloud_removed:
                raise UserError(_("Controller %s is missing or inactive.") % controller_code)
            if server_code and (
                not controller.edge_server_id
                or self._normalize_sync_code(controller.edge_server_id.edge_server_code) != server_code
            ):
                raise UserError(
                    _("Controller %(controller)s is not assembled under Server %(server)s.")
                    % {"controller": controller_code, "server": server_code}
                )

            tolerance = lane_item.get("timing_tolerance") or {}
            if not isinstance(tolerance, dict):
                raise UserError(_("Timing Tolerance must be an object."))
            unsupported_tolerance = set(tolerance) - {"type", "value"}
            if unsupported_tolerance:
                raise UserError(
                    _("Unsupported Timing Tolerance field(s): %s")
                    % ", ".join(sorted(unsupported_tolerance))
                )
            tolerance_type = str(tolerance.get("type") or "percent").strip().lower()
            if tolerance_type not in ("percent", "seconds"):
                raise UserError(_("Timing Tolerance type must be percent or seconds."))
            try:
                tolerance_value = float(tolerance.get("value") or 0.0)
            except (TypeError, ValueError) as exc:
                raise UserError(_("Timing Tolerance value must be numeric.")) from exc
            if tolerance_value < 0:
                raise UserError(_("Timing Tolerance cannot be negative."))

            lane_specs[lane_code] = {
                "parking_area_id": parking.id,
                "code": lane_code,
                "name": str(lane_item.get("lane_name") or lane_code).strip(),
                "controller_id": controller.id,
                "tolerance_type": tolerance_type,
                "tolerance_value": tolerance_value,
                "active": True,
            }

            timeline = lane_item.get("antenna_timeline") or []
            if not isinstance(timeline, list):
                raise UserError(_("Antenna Timeline must be an array."))
            if state == "operational" and len(timeline) < 2:
                raise UserError(
                    _("Operational Lane %s requires at least two Antenna Timeline points.")
                    % lane_code
                )
            seen_orders = set()
            seen_antenna_ids = set()
            timeline_position_by_antenna = {}
            for row in timeline:
                if not isinstance(row, dict):
                    raise UserError(_("Antenna Timeline rows must contain objects."))
                supported_timeline = {
                    "sequence",
                    "antenna_code",
                    "antenna_name",
                    "reader_code",
                    "reader_serial_number",
                    "port_no",
                    "duration_from_previous_seconds",
                    "cumulative_time_seconds",
                }
                unsupported_timeline = set(row) - supported_timeline
                if unsupported_timeline:
                    raise UserError(
                        _("Unsupported Antenna Timeline field(s): %s")
                        % ", ".join(sorted(unsupported_timeline))
                    )
                try:
                    sequence = int(row.get("sequence") or 0)
                    port_no = int(row.get("port_no") or 0)
                    duration = float(row.get("duration_from_previous_seconds") or 0.0)
                    cumulative = float(row.get("cumulative_time_seconds") or 0.0)
                except (TypeError, ValueError) as exc:
                    raise UserError(_("Invalid Antenna Timeline number or Duration.")) from exc
                serial = self._normalize_sync_code(row.get("reader_serial_number"))
                antenna_code = self._normalize_sync_code(row.get("antenna_code"))
                antenna = antenna_by_key.get((serial, port_no)) or antenna_by_code.get(antenna_code)
                if sequence <= 0 or port_no <= 0 or not antenna:
                    raise UserError(_("Antenna Timeline references an invalid Reader port or Antenna."))
                if sequence in seen_orders or antenna.id in seen_antenna_ids:
                    raise UserError(_("Timeline Order and Antenna must be unique per Lane."))
                if antenna_code and self._normalize_sync_code(antenna.technical_code) != antenna_code:
                    raise UserError(_("Antenna Code does not match the Reader port mapping."))
                if antenna.device_id.controller_id != controller:
                    raise UserError(_("Every Timeline Reader must belong to the Lane Controller."))
                if sequence == 1 and duration != 0.0:
                    raise UserError(_("The first Timeline point must have zero Duration from previous."))
                if sequence > 1 and duration <= 0.0:
                    raise UserError(_("Every Timeline point after the first requires a positive Duration."))
                seen_orders.add(sequence)
                seen_antenna_ids.add(antenna.id)
                timeline_position_by_antenna[antenna.id] = sequence
                timeline_specs.append({
                    "lane_code": lane_code,
                    "sequence": sequence,
                    "antenna_id": antenna.id,
                    "reader_id": antenna.device_id.id,
                    "port_no": port_no,
                    "duration_from_previous": duration,
                    "cumulative_time": max(0.0, cumulative),
                })
            if seen_orders and seen_orders != set(range(1, len(seen_orders) + 1)):
                raise UserError(_("Antenna Timeline Order must be contiguous and start at 1."))

            event_sequences = lane_item.get("event_sequences") or {}
            if not isinstance(event_sequences, dict):
                raise UserError(_("Event Sequences must be an object."))
            unsupported_events = set(event_sequences) - {"check_in", "check_out"}
            if unsupported_events:
                raise UserError(
                    _("Unsupported Event Sequence type(s): %s")
                    % ", ".join(sorted(unsupported_events))
                )
            configured_count = 0
            orientation_by_type = {}
            for sequence_type in ("check_in", "check_out"):
                values = event_sequences.get(sequence_type) or []
                if not isinstance(values, list):
                    raise UserError(_("Each Event Sequence must be an array."))
                if values and len(values) < 2:
                    raise UserError(_("Each configured Event Sequence requires at least two Antennas."))
                seen_sequence_antennas = set()
                sequence_positions = []
                for order, raw_code in enumerate(values, start=1):
                    antenna_code = self._normalize_sync_code(raw_code)
                    antenna = antenna_by_code.get(antenna_code)
                    if not antenna or antenna.id not in seen_antenna_ids:
                        raise UserError(_("Event Sequence Antenna must exist in the Lane Timeline."))
                    if antenna.id in seen_sequence_antennas:
                        raise UserError(_("An Antenna can appear only once in one Event Sequence."))
                    seen_sequence_antennas.add(antenna.id)
                    sequence_positions.append(timeline_position_by_antenna[antenna.id])
                    sequence_specs.append({
                        "lane_code": lane_code,
                        "sequence_type": sequence_type,
                        "sequence": order,
                        "antenna_id": antenna.id,
                    })
                for previous, current in zip(sequence_positions, sequence_positions[1:]):
                    if abs(current - previous) != 1:
                        raise UserError(_("Event Sequence must follow adjacent points in the Antenna Timeline."))
                if len(sequence_positions) >= 2:
                    orientation_by_type[sequence_type] = (
                        1 if sequence_positions[1] > sequence_positions[0] else -1
                    )
                configured_count += bool(values)
            if (
                orientation_by_type.get("check_in")
                and orientation_by_type.get("check_out")
                and orientation_by_type["check_in"] == orientation_by_type["check_out"]
            ):
                raise UserError(
                    _("Check-in and Check-out Sequences must follow opposite Timeline directions.")
                )
            if state == "operational" and not configured_count:
                raise UserError(
                    _("Operational Lane %s requires at least one Check-in or Check-out Sequence.")
                    % lane_code
                )

        Lane = self.env["nsp.parking.lane"].sudo().with_context(active_test=False)
        existing_lanes = Lane.search([("parking_area_id", "=", parking.id)])
        lane_by_code = {lane.code: lane for lane in existing_lanes}
        for lane_code, values in lane_specs.items():
            lane = lane_by_code.get(lane_code)
            if lane:
                self._write_changed(lane, values)
            else:
                lane = Lane.create(values)
                lane_by_code[lane_code] = lane

        area_lanes = Lane.search([("parking_area_id", "=", parking.id)])
        Timeline = self.env["nsp.parking.lane.timeline"].sudo()
        Sequence = self.env["nsp.parking.lane.event.sequence"].sudo()

        # A Parking Runtime response is a complete immutable snapshot. Replace
        # child topology atomically instead of patching rows by Order. This
        # avoids transient SQL conflicts when two Antennas exchange positions
        # or an Event Sequence is reversed between published revisions.
        existing_timeline = (
            Timeline.search([("lane_id", "in", area_lanes.ids)])
            if area_lanes else Timeline.browse()
        )
        existing_sequences = (
            Sequence.search([("lane_id", "in", area_lanes.ids)])
            if area_lanes else Sequence.browse()
        )
        if existing_sequences:
            existing_sequences.unlink()
        if existing_timeline:
            existing_timeline.unlink()

        timeline_values = []
        for spec in timeline_specs:
            values = dict(spec)
            values["lane_id"] = lane_by_code[values.pop("lane_code")].id
            timeline_values.append(values)
        if timeline_values:
            Timeline.create(timeline_values)

        sequence_values = []
        for spec in sequence_specs:
            values = dict(spec)
            values["lane_id"] = lane_by_code[values.pop("lane_code")].id
            sequence_values.append(values)
        if sequence_values:
            Sequence.create(sequence_values)

        incoming_lane_codes = set(lane_specs)
        stale_lanes = area_lanes.filtered(
            lambda lane: lane.code not in incoming_lane_codes and lane.active
        )
        if stale_lanes:
            stale_lanes.mapped("timeline_line_ids").unlink()
            stale_lanes.mapped("event_sequence_ids").unlink()
            stale_lanes.write({"active": False})

        if parking.state == "operational":
            issues = parking._operational_issues()
            if issues:
                raise UserError("; ".join(str(issue) for issue in issues))
        return parking

    def _validate_operational_parking_topology(self):
        """Every active Antenna may belong to only one operational Lane."""
        self.ensure_one()
        rows = self.env["nsp.parking.lane.timeline"].sudo().search([
            ("lane_id.active", "=", True),
            ("lane_id.parking_area_id.state", "=", "operational"),
        ])
        lane_by_antenna = {}
        conflicts = []
        for row in rows:
            previous = lane_by_antenna.get(row.antenna_id.id)
            if previous and previous != row.lane_id:
                conflicts.append(
                    "%s: %s / %s" % (
                        row.antenna_id.display_name,
                        previous.display_name,
                        row.lane_id.display_name,
                    )
                )
            else:
                lane_by_antenna[row.antenna_id.id] = row.lane_id
        if conflicts:
            raise UserError(
                _("Operational Parking topology contains duplicated Antenna assignments: %s")
                % "; ".join(sorted(set(conflicts)))
            )
        return True

    def _reconcile_parking_config_snapshot(self, items):
        self.ensure_one()
        incoming_codes = {
            self._normalize_sync_code(item.get("parking_area_code"))
            for item in (items or [])
            if isinstance(item, dict) and item.get("parking_area_code")
        }
        Parking = self.env["nsp.parking.area"].sudo().with_context(active_test=False)
        stale = Parking.search([("code", "not in", list(incoming_codes))]) if incoming_codes else Parking.search([])
        if stale:
            stale.mapped("lane_ids.timeline_line_ids").unlink()
            stale.mapped("lane_ids.event_sequence_ids").unlink()
            stale.mapped("lane_ids").write({"active": False})
            stale.write({"state": "blocked"})
        return len(stale)

    @staticmethod
    def _normalize_sync_code(value):
        return str(value or "").strip().upper()
