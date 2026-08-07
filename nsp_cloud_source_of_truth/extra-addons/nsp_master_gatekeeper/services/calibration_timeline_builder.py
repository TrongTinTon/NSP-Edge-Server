# -*- coding: utf-8 -*-
"""Pure Lane Calibration timeline construction.

The builder accepts plain dictionaries and returns plain dictionaries. It has no
ORM, environment, access-right, or persistence responsibility. Model code is
responsible for translating Odoo records to the input contract and for writing
any resulting records.
"""


class CalibrationTimelineBuilder:
    """Collapse consecutive RFID reads into ordered Reader-Port timeline steps."""

    @classmethod
    def build(cls, events, *, readers_by_serial=None, targets_by_tid=None):
        readers_by_serial = readers_by_serial or {}
        targets_by_tid = targets_by_tid or {}
        steps = []
        current = None

        for event in events:
            serial_number = str(event.get("serial_number") or "").strip().upper()
            tid = str(event.get("tid") or "").strip().upper()
            port_no = int(event.get("port_no") or 0)
            key = (tid, serial_number, port_no)

            if current and current["_key"] == key:
                current["last_seen_at"] = event.get("timestamp")
                current["read_count"] += 1
                continue

            if current:
                cls._append_step(steps, current)

            reader = readers_by_serial.get(serial_number, {})
            target = targets_by_tid.get(tid, {})
            current = {
                "_key": key,
                "_first_seconds": float(event.get("observed_seconds") or 0.0),
                "first_event_id": int(event.get("id") or 0),
                "sequence_no": len(steps) + 1,
                "first_seen_at": event.get("timestamp"),
                "last_seen_at": event.get("timestamp"),
                "tid": tid,
                "assignment_role": target.get("assignment_role", ""),
                "assigned_to": target.get("assigned_to", ""),
                "license_plate": target.get("license_plate", ""),
                "controller_code": reader.get("controller_code", ""),
                "serial_number": serial_number,
                "reader_name": reader.get("reader_name") or serial_number,
                "port_no": port_no,
                "read_count": 1,
            }

        if current:
            cls._append_step(steps, current)

        cls._calculate_durations(steps)
        return steps

    @staticmethod
    def _append_step(steps, step):
        step.pop("_key", None)
        steps.append(step)

    @staticmethod
    def _calculate_durations(steps):
        previous_seconds = None
        first_seconds = None
        for index, step in enumerate(steps, start=1):
            current_seconds = float(step.pop("_first_seconds", 0.0) or 0.0)
            if first_seconds is None:
                first_seconds = current_seconds
            step["sequence_no"] = index
            step["duration_from_previous"] = (
                0.0
                if previous_seconds is None
                else max(current_seconds - previous_seconds, 0.0)
            )
            step["elapsed_from_start"] = max(current_seconds - first_seconds, 0.0)
            previous_seconds = current_seconds
