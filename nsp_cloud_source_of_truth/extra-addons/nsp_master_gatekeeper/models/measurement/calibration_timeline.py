# -*- coding: utf-8 -*-
from odoo import fields, models, _
from odoo.exceptions import ValidationError

from ...services.calibration_timeline_builder import CalibrationTimelineBuilder


class NspMeasurementSessionTimeline(models.Model):
    _inherit = "nsp.measurement.session"

    def _event_timestamp(self, event):
        self.ensure_one()
        if not event.read_at:
            return None
        base = fields.Datetime.to_string(event.read_at).replace(" ", "T")
        return "%s.%03dZ" % (base, int(event.read_at_ms or 0))

    def _event_seconds(self, event):
        self.ensure_one()
        observed_at = fields.Datetime.to_datetime(event.read_at)
        if not observed_at:
            return 0.0
        return observed_at.timestamp() + (int(event.read_at_ms or 0) / 1000.0)

    def _build_detection_steps(self, events):
        self.ensure_one()
        readers_by_serial = {
            (line.reader_id.serial_number or "").strip().upper(): {
                "controller_code": line.controller_id.controller_id or "",
                "reader_name": line.reader_id.name or line.reader_id.serial_number or "",
            }
            for line in self.reader_line_ids
        }
        targets_by_tid = {
            (line.vehicle_tid or "").strip().upper(): {
                "assignment_role": "vehicle",
                "assigned_to": line.license_plate or "",
                "license_plate": line.license_plate or "",
            }
            for line in self.target_line_ids
            if line.vehicle_tid
        }
        event_values = []
        for event in events:
            event_values.append({
                "id": event.id,
                "tid": event.tid,
                "serial_number": event.serial_number,
                "port_no": int(event.port_no or 0),
                "timestamp": self._event_timestamp(event),
                "observed_seconds": self._event_seconds(event),
            })
        return CalibrationTimelineBuilder.build(
            event_values,
            readers_by_serial=readers_by_serial,
            targets_by_tid=targets_by_tid,
        )

    def _port_summary(self):
        self.ensure_one()
        rows = self.env["nsp.measurement.event"].sudo()._read_group(
            [("session_id", "=", self.id), ("revision", "=", self.revision)],
            ["tid", "serial_number", "port_no"],
            ["__count", "read_at:min", "read_at:max"],
            order="tid, serial_number, port_no",
        )
        return [
            {
                "tid": tid,
                "serial_number": serial_number,
                "port_no": int(port_no or 0),
                "read_count": int(count or 0),
                "first_read_at": first_read,
                "last_read_at": last_read,
            }
            for tid, serial_number, port_no, count, first_read, last_read in rows
        ]

    def _clear_detection_timeline(self):
        self.check_access("write")
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
        except (TypeError, ValueError) as exc:
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
            current_seconds = self._event_seconds(event)
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
                "reader_power_dbm": int(reader_line.reader_power_dbm or 0),
                "read_interval_ms": int(reader_line.read_interval_ms or 200),
                "tid_start_address": int(reader_line.reader_tid_addr or 0),
                "tid_length": int(reader_line.reader_tid_len or 4),
                "checkin_order": selection_order,
                "checkout_order": len(ordered_ids) - selection_order + 1,
            })

        if len(controller_ids) != 1 or len(edge_ids) != 1:
            raise ValidationError(_(
                "A Lane must be built from Detection Timeline rows belonging to one Server and one Controller."
            ))
        return selected, next(iter(edge_ids)), next(iter(controller_ids))
