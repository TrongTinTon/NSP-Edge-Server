# -*- coding: utf-8 -*-
from odoo import api, models


class ParkingDetectionEventSequence(models.Model):
    _inherit = "nsp.parking.detection.event"

    @api.model
    def _resolve_topology_batch(self, controller, detections):
        keys = {
            (
                str(payload.get("serial_number") or "").strip().upper(),
                int(payload.get("antenna_no") or 0),
            )
            for payload, _assignment in detections
        }
        keys.discard(("", 0))
        if not keys:
            return {}, {}
        serials = {serial for serial, _port in keys}
        devices = self.env["nsp.device"].sudo().search([
            ("controller_id", "=", controller.id),
            ("serial_number", "in", list(serials)),
            ("active", "=", True),
        ])
        device_by_serial = {str(device.serial_number or "").strip().upper(): device for device in devices}
        ports = {port for _serial, port in keys}
        antennas = self.env["nsp.device.antenna"].sudo().search([
            ("device_id", "in", devices.ids),
            ("antenna_no", "in", list(ports)),
            ("active", "=", True),
        ]) if devices and ports else self.env["nsp.device.antenna"].browse()
        antenna_by_key = {
            (str(antenna.device_id.serial_number or "").strip().upper(), int(antenna.antenna_no or 0)): antenna
            for antenna in antennas
        }
        Timeline = self.env["nsp.parking.lane.timeline"].sudo()
        timeline_rows = Timeline.search([
            ("lane_id.active", "=", True),
            ("antenna_id", "in", antennas.ids),
        ]) if antennas else Timeline.browse()
        lanes_by_antenna = {}
        for row in timeline_rows:
            lanes_by_antenna.setdefault(row.antenna_id.id, set()).add(row.lane_id.id)
        resolved, errors = {}, {}
        Lane = self.env["nsp.parking.lane"].sudo()
        for key in keys:
            serial, _port = key
            device = device_by_serial.get(serial)
            if not device:
                errors[key] = "device_not_found"
                continue
            antenna = antenna_by_key.get(key)
            if not antenna:
                errors[key] = "antenna_not_found"
                continue
            lane_ids = lanes_by_antenna.get(antenna.id, set())
            if not lane_ids:
                errors[key] = "no_antenna_timeline"
                continue
            if len(lane_ids) != 1:
                errors[key] = "ambiguous_antenna_lane"
                continue
            lane = Lane.browse(next(iter(lane_ids))).exists()
            if not lane or lane.controller_id != controller:
                errors[key] = "controller_not_in_scope"
                continue
            resolved[key] = (antenna, lane)
        return resolved, errors

    @api.model
    def _lane_max_duration(self, lane):
        if lane.timeline_line_ids:
            return lane.max_sequence_window()
        return super()._lane_max_duration(lane)

    @api.model
    def _build_vehicle_transitions(self, lane):
        if not lane.event_sequence_ids or not lane.timeline_line_ids:
            return super()._build_vehicle_transitions(lane)
        vehicle_events = self.search([
            ("lane_id", "=", lane.id),
            ("state", "=", "pending"),
            ("transaction_id", "=", False),
            ("vehicle_id", "!=", False),
        ], order="tag_id asc, detected_at asc, id asc")
        if not vehicle_events:
            return []

        timeline = lane.timeline_line_ids.sorted("sequence")
        allowed_by_pair = {}
        for index in range(1, len(timeline)):
            source = timeline[index - 1].antenna_id.id
            target = timeline[index].antenna_id.id
            allowed = lane.allowed_duration_for_step(timeline[index].sequence)
            allowed_by_pair[frozenset((source, target))] = max(0.001, allowed)

        sequence_specs = []
        for sequence_type, event_type in (("check_in", "check_in"), ("check_out", "check_out")):
            rows = lane.event_sequence_ids.filtered(
                lambda row: row.sequence_type == sequence_type
            ).sorted("sequence")
            if rows:
                sequence_specs.append((event_type, rows.mapped("antenna_id").ids))

        events_by_tag = {}
        for event in vehicle_events:
            events_by_tag.setdefault(event.tag_id.id, []).append(event)

        matches = []
        for tag_id, raw_events in events_by_tag.items():
            collapsed = []
            for event in raw_events:
                if collapsed and collapsed[-1].antenna_id == event.antenna_id:
                    continue
                collapsed.append(event)
            for event_type, expected_ids in sequence_specs:
                length = len(expected_ids)
                if length < 2 or len(collapsed) < length:
                    continue
                for offset in range(0, len(collapsed) - length + 1):
                    window = collapsed[offset:offset + length]
                    actual_ids = [event.antenna_id.id for event in window]
                    if actual_ids != expected_ids:
                        continue
                    valid = True
                    total_allowed = 0.0
                    for index in range(1, length):
                        pair = frozenset((actual_ids[index - 1], actual_ids[index]))
                        allowed = allowed_by_pair.get(pair)
                        gap = (window[index].detected_at - window[index - 1].detected_at).total_seconds()
                        if allowed is None or gap < 0 or gap > allowed:
                            valid = False
                            break
                        total_allowed += allowed
                    if not valid:
                        continue
                    recordset = self.browse([event.id for event in window])
                    matches.append({
                        "tag_id": tag_id,
                        "event_type": event_type,
                        "duration_seconds": max(total_allowed, lane.max_sequence_window()),
                        "start_at": window[0].detected_at,
                        "end_at": window[-1].detected_at,
                        "events": recordset,
                        "rule": False,
                    })
        matches.sort(key=lambda item: (item["end_at"], item["start_at"], item["tag_id"]))
        return matches
