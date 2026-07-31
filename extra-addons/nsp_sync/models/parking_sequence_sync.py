# -*- coding: utf-8 -*-
from odoo import api, models, _
from odoo.exceptions import UserError


class NspSyncJobParkingSequence(models.Model):
    _inherit = "nsp.sync.job"

    def _apply_parking_config(self, item):
        lanes = item.get("lanes") if isinstance(item, dict) else False
        has_sequence_payload = bool(
            isinstance(lanes, list)
            and any(isinstance(lane, dict) and "timeline" in lane for lane in lanes)
        )
        if not has_sequence_payload:
            return super()._apply_parking_config(item)
        return self._apply_parking_sequence_config(item)

    def _apply_parking_sequence_config(self, item):
        self.ensure_one()
        if not isinstance(item, dict):
            raise UserError(_("Parking configuration item must be an object."))
        branch_code = str(item.get("branch_code") or "").strip().upper()
        area_code = str(item.get("parking_area_code") or "").strip().upper()
        if not branch_code or not area_code:
            raise UserError(_("Branch Code and Parking Area Code are required."))
        state = str(item.get("state") or "draft").strip().lower()
        if state not in ("draft", "operational", "maintenance", "blocked"):
            raise UserError(_("Invalid Parking Area state: %s") % state)

        branch = self.env["nsp.branch"].sudo().with_context(active_test=False).search(
            [("code", "=", branch_code)], limit=1
        )
        if not branch:
            raise UserError(_("Branch %s was not found in the current snapshot.") % branch_code)

        Parking = self.env["nsp.parking.area"].sudo()
        parking = Parking.search([("code", "=", area_code)], limit=1)
        parking_vals = {
            "code": area_code,
            "name": str(item.get("parking_area_name") or area_code).strip(),
            "branch_id": branch.id,
            "state": state,
        }
        if parking:
            self._write_changed(parking, parking_vals)
        else:
            parking = Parking.create(parking_vals)

        controllers = self.env["nsp.controller"].sudo().with_context(active_test=False).search([])
        controller_by_code = {record.controller_id: record for record in controllers}
        antennas = self.env["nsp.device.antenna"].sudo().with_context(active_test=False).search([])
        antenna_by_key = {
            (str(record.device_id.serial_number or "").strip().upper(), int(record.antenna_no or 0)): record
            for record in antennas
        }
        antenna_by_code = {
            str(record.technical_code or "").strip().upper(): record
            for record in antennas if record.technical_code
        }

        lane_specs = {}
        timeline_specs = []
        event_specs = []
        lanes_data = item.get("lanes") or []
        if not isinstance(lanes_data, list):
            raise UserError(_("Parking lanes must be an array."))

        for lane_item in lanes_data:
            if not isinstance(lane_item, dict):
                raise UserError(_("Parking lanes must contain objects."))
            supported = {
                "lane_code", "lane_name", "server_code", "controller_code", "readers",
                "direction", "timeline", "check_in_sequence", "check_out_sequence",
                "tolerance_type", "tolerance_value",
            }
            unsupported = set(lane_item) - supported
            if unsupported:
                raise UserError(_("Unsupported Parking Lane field(s): %s") % ", ".join(sorted(unsupported)))
            lane_code = str(lane_item.get("lane_code") or "").strip().upper()
            controller_code = str(lane_item.get("controller_code") or "").strip().upper()
            server_code = str(lane_item.get("server_code") or "").strip().upper()
            if not lane_code or lane_code in lane_specs or not controller_code:
                raise UserError(_("Parking Lane Code and Controller Code are required."))
            controller = controller_by_code.get(controller_code)
            if not controller or not controller.active or controller.cloud_removed:
                raise UserError(_("Controller %s is missing or inactive.") % controller_code)
            if server_code and (
                not controller.edge_server_id
                or controller.edge_server_id.edge_server_code != server_code
            ):
                raise UserError(_("Controller %(controller)s is not assembled under Server %(server)s.") % {
                    "controller": controller_code, "server": server_code,
                })
            direction = str(lane_item.get("direction") or "entry").strip().lower()
            if direction not in ("entry", "exit", "bidirectional"):
                raise UserError(_("Invalid Lane direction: %s") % direction)
            tolerance_type = str(lane_item.get("tolerance_type") or "percent").strip().lower()
            if tolerance_type not in ("percent", "seconds"):
                raise UserError(_("Invalid transition tolerance type."))
            try:
                tolerance_value = float(lane_item.get("tolerance_value") or 0.0)
            except (TypeError, ValueError) as exc:
                raise UserError(_("Invalid transition tolerance value.")) from exc
            if tolerance_value < 0:
                raise UserError(_("Transition tolerance cannot be negative."))

            lane_specs[lane_code] = {
                "parking_area_id": parking.id,
                "code": lane_code,
                "name": str(lane_item.get("lane_name") or lane_code).strip(),
                "controller_id": controller.id,
                "direction": direction,
                "tolerance_type": tolerance_type,
                "tolerance_value": tolerance_value,
                "active": True,
            }

            timeline = lane_item.get("timeline") or []
            if not isinstance(timeline, list):
                raise UserError(_("Lane Antenna Timeline must be an array."))
            if state == "operational" and len(timeline) < 2:
                raise UserError(_("Operational Lane %s requires at least two Timeline points.") % lane_code)
            seen_order = set()
            seen_antennas = set()
            for row in timeline:
                if not isinstance(row, dict):
                    raise UserError(_("Timeline rows must contain objects."))
                try:
                    sequence = int(row.get("sequence") or 0)
                    port_no = int(row.get("port_no") or 0)
                    duration = float(row.get("duration_from_previous") or 0.0)
                    cumulative = float(row.get("cumulative_time") or 0.0)
                except (TypeError, ValueError) as exc:
                    raise UserError(_("Invalid Timeline number or Duration.")) from exc
                serial = str(row.get("reader_serial_number") or "").strip().upper()
                antenna_code = str(row.get("antenna_code") or "").strip().upper()
                antenna = antenna_by_key.get((serial, port_no)) or antenna_by_code.get(antenna_code)
                if sequence <= 0 or port_no <= 0 or not antenna:
                    raise UserError(_("Timeline references an invalid Reader port or Antenna."))
                if antenna_code and antenna.technical_code != antenna_code:
                    raise UserError(_("Timeline Antenna Management Code does not match Reader port mapping."))
                if antenna.device_id.controller_id != controller:
                    raise UserError(_("Every Timeline Reader must belong to the Lane Controller."))
                if sequence in seen_order or antenna.id in seen_antennas:
                    raise UserError(_("Timeline order and Antenna must be unique per Lane."))
                seen_order.add(sequence)
                seen_antennas.add(antenna.id)
                timeline_specs.append({
                    "lane_code": lane_code,
                    "sequence": sequence,
                    "antenna_id": antenna.id,
                    "reader_id": antenna.device_id.id,
                    "port_no": port_no,
                    "duration_from_previous": max(0.0, duration),
                    "cumulative_time": max(0.0, cumulative),
                })

            for sequence_type, key in (("check_in", "check_in_sequence"), ("check_out", "check_out_sequence")):
                values = lane_item.get(key) or []
                if not isinstance(values, list):
                    raise UserError(_("Parking Event Sequence must be an array."))
                for sequence, raw_code in enumerate(values, start=1):
                    antenna_code = str(raw_code or "").strip().upper()
                    antenna = antenna_by_code.get(antenna_code)
                    if not antenna or antenna.id not in seen_antennas:
                        raise UserError(_("Event Sequence Antenna must exist in the Lane Timeline."))
                    event_specs.append({
                        "lane_code": lane_code,
                        "sequence_type": sequence_type,
                        "sequence": sequence,
                        "antenna_id": antenna.id,
                    })
            if state == "operational":
                if direction in ("entry", "bidirectional") and not lane_item.get("check_in_sequence"):
                    raise UserError(_("Operational Lane %s requires a Check-in Sequence.") % lane_code)
                if direction in ("exit", "bidirectional") and not lane_item.get("check_out_sequence"):
                    raise UserError(_("Lane %s requires a Check-out Sequence.") % lane_code)

        Lane = self.env["nsp.parking.lane"].sudo().with_context(active_test=False)
        area_lanes = Lane.search([("parking_area_id", "=", parking.id)])
        lane_by_code = {lane.code: lane for lane in area_lanes}
        for lane_code, values in lane_specs.items():
            lane = lane_by_code.get(lane_code)
            if lane:
                self._write_changed(lane, values)
            else:
                lane = Lane.create(values)
                lane_by_code[lane_code] = lane

        Timeline = self.env["nsp.parking.lane.timeline"].sudo()
        Sequence = self.env["nsp.parking.lane.event.sequence"].sudo()
        desired_timeline = set()
        existing_timeline = Timeline.search([("lane_id", "in", area_lanes.ids)]) if area_lanes else Timeline.browse()
        timeline_by_key = {(row.lane_id.code, row.sequence): row for row in existing_timeline}
        create_values = []
        for spec in timeline_specs:
            key = (spec["lane_code"], spec["sequence"])
            desired_timeline.add(key)
            values = {**spec, "lane_id": lane_by_code[spec["lane_code"]].id}
            values.pop("lane_code")
            existing = timeline_by_key.get(key)
            if existing:
                self._write_changed(existing, values)
            else:
                create_values.append(values)
        if create_values:
            Timeline.create(create_values)
        existing_timeline.filtered(lambda row: (row.lane_id.code, row.sequence) not in desired_timeline).unlink()

        desired_sequences = set()
        existing_sequences = Sequence.search([("lane_id", "in", area_lanes.ids)]) if area_lanes else Sequence.browse()
        sequence_by_key = {(row.lane_id.code, row.sequence_type, row.sequence): row for row in existing_sequences}
        create_values = []
        for spec in event_specs:
            key = (spec["lane_code"], spec["sequence_type"], spec["sequence"])
            desired_sequences.add(key)
            values = {**spec, "lane_id": lane_by_code[spec["lane_code"]].id}
            values.pop("lane_code")
            existing = sequence_by_key.get(key)
            if existing:
                self._write_changed(existing, values)
            else:
                create_values.append(values)
        if create_values:
            Sequence.create(create_values)
        existing_sequences.filtered(
            lambda row: (row.lane_id.code, row.sequence_type, row.sequence) not in desired_sequences
        ).unlink()

        incoming_codes = set(lane_specs)
        stale_lanes = area_lanes.filtered(lambda lane: lane.code not in incoming_codes and lane.active)
        if stale_lanes:
            stale_lanes.mapped("timeline_line_ids").unlink()
            stale_lanes.mapped("event_sequence_ids").unlink()
            stale_lanes.mapped("antenna_transition_ids").unlink()
            stale_lanes.write({"active": False})
        # New sequence configuration supersedes legacy pair rules.
        lane_by_code_values = self.env["nsp.parking.lane"].browse([lane.id for lane in lane_by_code.values()])
        lane_by_code_values.mapped("antenna_transition_ids").unlink()

        if parking.state == "operational":
            issues = parking._operational_issues()
            if issues:
                raise UserError("; ".join(str(issue) for issue in issues))
        return parking

    def _reconcile_parking_config_snapshot(self, items):
        result = super()._reconcile_parking_config_snapshot(items)
        incoming_codes = {
            str(item.get("parking_area_code") or "").strip().upper()
            for item in (items or [])
            if isinstance(item, dict) and item.get("parking_area_code")
        }
        Parking = self.env["nsp.parking.area"].sudo()
        stale = Parking.search([("code", "not in", list(incoming_codes))]) if incoming_codes else Parking.search([])
        if stale:
            stale.mapped("lane_ids.timeline_line_ids").unlink()
            stale.mapped("lane_ids.event_sequence_ids").unlink()
        return result
