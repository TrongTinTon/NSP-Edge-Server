# -*- coding: utf-8 -*-
import logging
import os
from datetime import timedelta

from psycopg2 import IntegrityError

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


_logger = logging.getLogger(__name__)


class ParkingDetectionEvent(models.Model):
    """Short-lived Edge RFID read used to build final parking transactions.

    Controller reports physical reads only. Edge resolves each Reader/Antenna
    against the published Lane Antenna Timeline, collapses repeated reads, and
    matches the resulting timeline against explicit Check-in/Check-out antenna
    sequences. Raw detections remain on Edge and never synchronize to Cloud.
    """

    _name = "nsp.parking.detection.event"
    _description = "NSP Parking Detection Event"
    _rec_name = "event_uid"
    _order = "detected_at desc, id desc"
    _log_access = False

    event_uid = fields.Char(
        string="Detection UID", required=True, copy=False, readonly=True,
        help="Controller-generated idempotency key for one detected TID.",
    )
    detected_at = fields.Datetime(
        string="Detected At", required=True, index=True, readonly=True,
        help="UTC time reported by the Controller.",
    )
    lane_id = fields.Many2one(
        "nsp.parking.lane", string="Lane", required=True,
        ondelete="restrict", readonly=True, index=True,
    )
    antenna_id = fields.Many2one(
        "nsp.device.antenna", string="Antenna", required=True,
        ondelete="restrict", readonly=True, index=True,
    )
    tag_id = fields.Many2one(
        "nsp.rfid.tag", string="RFID Tag", required=True,
        ondelete="restrict", readonly=True, index=True,
    )
    user_id = fields.Many2one(
        "nsp.user", string="Resolved User", ondelete="restrict",
        readonly=True, index=True,
    )
    vehicle_id = fields.Many2one(
        "nsp.vehicle", string="Resolved Vehicle", ondelete="restrict",
        readonly=True, index=True,
    )
    state = fields.Selection(
        [
            ("pending", "Pending"),
            ("processed", "Processed"),
            ("error", "Error"),
        ],
        string="State", required=True, default="pending", copy=False, readonly=True,
    )
    transaction_id = fields.Many2one(
        "nsp.parking.transaction", string="Parking Transaction",
        ondelete="set null", index=True, copy=False, readonly=True,
    )

    _sql_constraints = [
        ("event_uid_unique", "unique(event_uid)", "Detection UID must be unique."),
    ]

    def init(self):
        self.env.cr.execute(
            """
            CREATE INDEX IF NOT EXISTS nsp_parking_detection_pending_lane_idx
                ON nsp_parking_detection_event (lane_id, detected_at, id)
             WHERE state = 'pending' AND transaction_id IS NULL
            """
        )
        self.env.cr.execute(
            """
            CREATE INDEX IF NOT EXISTS nsp_parking_detection_sequence_idx
                ON nsp_parking_detection_event
                   (lane_id, tag_id, antenna_id, detected_at, id)
             WHERE state = 'pending' AND transaction_id IS NULL
            """
        )
        self.env.cr.execute(
            """
            CREATE INDEX IF NOT EXISTS nsp_parking_detection_cleanup_idx
                ON nsp_parking_detection_event (detected_at)
             WHERE state IN ('processed', 'error')
            """
        )

    @api.model
    def _deployment_role(self):
        role = (
            self.env["ir.config_parameter"].sudo().get_param("nsp.deployment_role")
            or os.getenv("NSP_DEPLOYMENT_ROLE")
            or os.getenv("NSP_SERVER_ROLE")
            or "edge_server"
        ).strip().lower()
        return role if role in ("cloud", "edge_server") else "edge_server"

    @api.model
    def _ensure_edge_role(self):
        if self._deployment_role() != "edge_server":
            raise ValidationError(_("parking_detection_edge_only"))

    @api.model
    def _business_values(self, source):
        def value(name):
            if hasattr(source, "_fields"):
                field = source._fields.get(name)
                raw = source[name]
                return raw.id if field and field.type == "many2one" and raw else raw
            return source.get(name)

        detected_at = value("detected_at")
        if detected_at:
            detected_at = fields.Datetime.to_string(fields.Datetime.to_datetime(detected_at))
        return {
            "detected_at": detected_at or "",
            "lane_id": int(value("lane_id") or 0),
            "antenna_id": int(value("antenna_id") or 0),
            "tag_id": int(value("tag_id") or 0),
            "user_id": int(value("user_id") or 0),
            "vehicle_id": int(value("vehicle_id") or 0),
        }

    @api.model
    def create_idempotent(self, vals):
        uid = str(vals.get("event_uid") or "").strip()
        if not uid:
            raise ValidationError(_("missing_event_uid"))
        vals = dict(vals, event_uid=uid)
        existing = self.search([("event_uid", "=", uid)], limit=1)
        if existing:
            if self._business_values(existing) != self._business_values(vals):
                raise ValidationError(_(
                    "event_uid_conflict: Detection UID already exists with different data."
                ))
            return existing, True
        try:
            with self.env.cr.savepoint():
                return self.create(vals), False
        except IntegrityError:
            existing = self.search([("event_uid", "=", uid)], limit=1)
            if not existing:
                raise
            if self._business_values(existing) != self._business_values(vals):
                raise ValidationError(_(
                    "event_uid_conflict: Detection UID already exists with different data."
                ))
            return existing, True

    @api.model
    def _resolve_topology_batch(self, controller, detections):
        """Resolve each Reader port to exactly one active Lane Timeline."""
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
            ("cloud_removed", "=", False),
        ])
        device_by_serial = {
            str(device.serial_number or "").strip().upper(): device
            for device in devices
        }
        ports = {port for _serial, port in keys}
        antennas = self.env["nsp.device.antenna"].sudo().search([
            ("device_id", "in", devices.ids),
            ("antenna_no", "in", list(ports)),
            ("active", "=", True),
            ("cloud_removed", "=", False),
        ]) if devices and ports else self.env["nsp.device.antenna"].browse()
        antenna_by_key = {
            (
                str(antenna.device_id.serial_number or "").strip().upper(),
                int(antenna.antenna_no or 0),
            ): antenna
            for antenna in antennas
        }

        timeline_rows = self.env["nsp.parking.lane.timeline"].sudo().search([
            ("lane_id.active", "=", True),
            ("lane_id.parking_area_id.state", "=", "operational"),
            ("antenna_id", "in", antennas.ids),
        ]) if antennas else self.env["nsp.parking.lane.timeline"].browse()
        lane_ids_by_antenna = {}
        for row in timeline_rows:
            lane_ids_by_antenna.setdefault(row.antenna_id.id, set()).add(row.lane_id.id)

        resolved = {}
        errors = {}
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
            lane_ids = lane_ids_by_antenna.get(antenna.id, set())
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
    def _ingest_controller_detection(self, controller, payload, assignment, topology_cache):
        if not isinstance(payload, dict):
            raise ValidationError(_("invalid_payload"))

        event_uid = str(payload.get("event_uid") or "").strip()
        serial_number = str(payload.get("serial_number") or "").strip().upper()
        tid = self.env["nsp.rfid.tag"]._normalize_tid(payload.get("tid"))
        try:
            antenna_no = int(payload.get("antenna_no") or 0)
        except Exception as exc:
            raise ValidationError(_("invalid_payload: antenna_no")) from exc
        try:
            detected_at = fields.Datetime.to_string(
                fields.Datetime.to_datetime(payload.get("detected_at"))
            )
        except Exception:
            detected_at = False

        if not event_uid:
            raise ValidationError(_("missing_event_uid"))
        if not serial_number:
            raise ValidationError(_("serial_number is required"))
        if antenna_no <= 0:
            raise ValidationError(_("antenna_no is required"))
        if not detected_at:
            raise ValidationError(_("detected_at is required"))
        if not tid:
            raise ValidationError(_("tid is required"))
        if not assignment or assignment.state != "active" or assignment.tid != tid:
            raise ValidationError(_("rfid_tag_not_actively_assigned"))
        if bool(assignment.user_id) == bool(assignment.vehicle_id):
            raise ValidationError(_("invalid_rfid_assignment"))

        topology = topology_cache.get((serial_number, antenna_no))
        if topology is None:
            raise ValidationError(_("no_antenna_timeline"))
        antenna, lane = topology

        vals = {
            "event_uid": event_uid,
            "detected_at": detected_at,
            "lane_id": lane.id,
            "antenna_id": antenna.id,
            "tag_id": assignment.tag_id.id,
            "user_id": assignment.user_id.id if assignment.user_id else False,
            "vehicle_id": assignment.vehicle_id.id if assignment.vehicle_id else False,
            "state": "pending",
        }
        record, duplicate = self.create_idempotent(vals)
        return record, duplicate, lane

    @api.model
    def ingest_controller_detections(self, controller, detections):
        """Persist one Controller batch, then process each touched Lane once."""
        self._ensure_edge_role()
        if not isinstance(detections, list):
            raise ValidationError(_("invalid_payload"))

        topology_cache, topology_errors = self._resolve_topology_batch(controller, detections)
        touched_lanes = self.env["nsp.parking.lane"].browse()
        for payload, assignment in detections:
            try:
                antenna_no = int(payload.get("antenna_no") or 0)
            except Exception:
                antenna_no = 0
            topology_key = (
                str(payload.get("serial_number") or "").strip().upper(),
                antenna_no,
            )
            topology_error = topology_errors.get(topology_key)
            if topology_error:
                _logger.warning(
                    "Parking detection ignored: controller=%s serial=%s antenna=%s tid=%s reason=%s",
                    controller.controller_id, topology_key[0], topology_key[1],
                    payload.get("tid"), topology_error,
                )
                continue
            try:
                with self.env.cr.savepoint():
                    _record, duplicate, lane = self._ingest_controller_detection(
                        controller, payload, assignment, topology_cache
                    )
                if not duplicate:
                    touched_lanes |= lane
            except ValidationError as exc:
                _logger.warning(
                    "Parking detection rejected at Edge: controller=%s event_uid=%s "
                    "serial=%s antenna=%s tid=%s reason=%s",
                    controller.controller_id,
                    payload.get("event_uid"),
                    payload.get("serial_number"),
                    payload.get("antenna_no"),
                    payload.get("tid"),
                    exc,
                )

        for lane in touched_lanes:
            # Cross-request movement is normal. Ingestion never expires incomplete
            # sequences; finalization belongs to the periodic pending-event job.
            self._process_pending_for_lane(lane, finalize_expired=False)
        return True

    @api.model
    def _pending_user_pool(self, lane):
        events = self.search([
            ("lane_id", "=", lane.id),
            ("state", "=", "pending"),
            ("transaction_id", "=", False),
            ("user_id", "!=", False),
        ], order="detected_at asc, id asc")
        return events, [event.detected_at for event in events]

    @api.model
    def _authorized_user_ids(self, vehicle, event_time):
        """Return the owner and active borrowers allowed to take this vehicle out."""
        if not vehicle or not vehicle.active:
            return set()
        user_ids = set()
        if vehicle.owner_id and vehicle.owner_id.active:
            user_ids.add(vehicle.owner_id.id)
        borrows = self.env["nsp.vehicle.borrow"].sudo().search([
            ("vehicle_id", "=", vehicle.id),
            ("state", "=", "active"),
            ("returned_at", "=", False),
            ("valid_from", "<=", event_time),
            ("valid_to", ">=", event_time),
            ("borrower_id.active", "=", True),
        ])
        user_ids.update(borrows.mapped("borrower_id").ids)
        return user_ids

    @api.model
    def _nearest_user_from_pool(
        self,
        user_events,
        anchor_at,
        window_seconds,
        consumed_ids,
        authorized_user_ids=None,
        allow_unauthorized=False,
    ):
        """Choose the nearest unused Employee Tag read in the configured sequence window.

        When the owner or an active borrower is present, an authorized read wins
        over a closer unrelated employee read. If no authorized read exists, the
        nearest read is returned so the denied decision remains fully auditable.
        """
        if not user_events:
            return self.browse()
        window = max(0.001, float(window_seconds or 0.0))
        candidates = user_events.filtered(
            lambda event: (
                event.id not in consumed_ids
                and event.state == "pending"
                and not event.transaction_id
                and abs((event.detected_at - anchor_at).total_seconds()) <= window
            )
        ).sorted(key=lambda event: (
            abs((event.detected_at - anchor_at).total_seconds()),
            event.detected_at,
            event.id,
        ))
        if not candidates:
            return self.browse()
        authorized = set(authorized_user_ids or [])
        if authorized:
            eligible = candidates.filtered(lambda event: event.user_id.id in authorized)
            if eligible:
                return eligible[:1]
        if not allow_unauthorized:
            return self.browse()
        return candidates[:1]

    @api.model
    def _lane_max_duration(self, lane):
        return lane.max_sequence_window()

    @api.model
    def _expire_orphan_user_events(self, lane, now):
        cutoff = now - timedelta(seconds=self._lane_max_duration(lane))
        stale = self.search([
            ("lane_id", "=", lane.id),
            ("state", "=", "pending"),
            ("transaction_id", "=", False),
            ("user_id", "!=", False),
            ("detected_at", "<", cutoff),
        ])
        if stale:
            stale.write({"state": "error"})

    @api.model
    def _assignment_maps(self, events):
        return (
            {event.tag_id.id: event.vehicle_id for event in events if event.vehicle_id},
            {event.tag_id.id: event.user_id for event in events if event.user_id},
        )

    @api.model
    def _build_vehicle_sequence_matches(self, lane):
        """Match pending Vehicle reads against configured event sequences."""
        if not lane.event_sequence_ids or not lane.timeline_line_ids:
            return []
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
            allowed_by_pair[frozenset((source, target))] = max(
                0.001, lane.allowed_duration_for_step(timeline[index].sequence)
            )

        sequence_specs = []
        for event_type in ("check_in", "check_out"):
            rows = lane.event_sequence_ids.filtered(
                lambda row: row.sequence_type == event_type
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
                    total_allowed = 0.0
                    valid = True
                    for index in range(1, length):
                        allowed = allowed_by_pair.get(frozenset((actual_ids[index - 1], actual_ids[index])))
                        gap = (window[index].detected_at - window[index - 1].detected_at).total_seconds()
                        if allowed is None or gap < 0 or gap > allowed:
                            valid = False
                            break
                        total_allowed += allowed
                    if not valid:
                        continue
                    matches.append({
                        "tag_id": tag_id,
                        "event_type": event_type,
                        "duration_seconds": max(0.001, total_allowed),
                        "start_at": window[0].detected_at,
                        "end_at": window[-1].detected_at,
                        "events": self.browse([event.id for event in window]),
                    })
        matches.sort(key=lambda item: (item["end_at"], item["start_at"], item["tag_id"]))
        return matches

    @api.model
    def _expire_stale_vehicle_events(self, lane, now):
        cutoff = now - timedelta(seconds=self._lane_max_duration(lane))
        stale = self.search([
            ("lane_id", "=", lane.id),
            ("state", "=", "pending"),
            ("transaction_id", "=", False),
            ("vehicle_id", "!=", False),
            ("detected_at", "<", cutoff),
        ])
        if stale:
            stale.write({"state": "error"})

    @api.model
    def _create_transaction_for_vehicle(
        self, vehicle_events, event_type, user_event=False,
        vehicle_by_tag=None, user_by_tag=None,
    ):
        group = vehicle_events
        if event_type == "check_out" and user_event:
            group |= user_event
        transaction = self.env["nsp.parking.transaction"].sudo().create_from_detection_group(
            group,
            resolved_event_type=event_type,
            vehicle_by_tag=vehicle_by_tag,
            user_by_tag=user_by_tag,
        )
        group.write({"state": "processed", "transaction_id": transaction.id})
        return transaction

    @api.model
    def _process_sequence_matches(self, lane, now, finalize_expired=True):
        transactions = self.env["nsp.parking.transaction"].browse()
        matches = self._build_vehicle_sequence_matches(lane)
        if not matches:
            if finalize_expired:
                self._expire_stale_vehicle_events(lane, now)
                self._expire_orphan_user_events(lane, now)
            return transactions

        user_events, _user_times = self._pending_user_pool(lane)
        vehicle_events = self.browse([
            event.id
            for match in matches
            for event in match["events"]
        ])
        vehicle_by_tag, user_by_tag = self._assignment_maps(vehicle_events | user_events)
        consumed_user_ids = set()
        blocked_tag_ids = set()

        for match in matches:
            tag_id = match["tag_id"]
            movement_events = match["events"].filtered(
                lambda rec: rec.state == "pending" and not rec.transaction_id
            )
            if (
                not movement_events
                or len(movement_events) != len(match["events"])
                or tag_id in blocked_tag_ids
            ):
                continue

            event_type = match["event_type"]
            duration = match["duration_seconds"]

            # Use the calibrated sequence window as the physical debounce window too.
            # This prevents lingering reads from creating duplicate business transactions
            # without reintroducing a global fixed suppression value.
            vehicle_event = movement_events.filtered(
                lambda rec: bool(rec.vehicle_id)
            ).sorted(key=lambda rec: (rec.detected_at, rec.id))[-1:]
            vehicle_tid = vehicle_event.tag_id.tid if vehicle_event else False
            recent = self.env["nsp.parking.transaction"].sudo().search([
                ("lane_id", "=", lane.id),
                ("event_type", "=", event_type),
                ("vehicle_tid", "=", vehicle_tid),
                ("event_time", ">=", match["end_at"] - timedelta(seconds=duration)),
                ("event_time", "<=", match["end_at"]),
            ], order="event_time desc, id desc", limit=1) if vehicle_tid else self.env["nsp.parking.transaction"].browse()
            if recent:
                movement_events.write({"state": "processed", "transaction_id": recent.id})
                continue

            user_event = self.browse()
            if event_type == "check_out":
                vehicle = movement_events.mapped("vehicle_id")[:1]
                authorized_user_ids = self._authorized_user_ids(
                    vehicle,
                    match["end_at"],
                )
                deadline = match["end_at"] + timedelta(seconds=duration)
                deadline_reached = bool(finalize_expired and now >= deadline)
                user_event = self._nearest_user_from_pool(
                    user_events,
                    match["end_at"],
                    duration,
                    consumed_user_ids,
                    authorized_user_ids=authorized_user_ids,
                    allow_unauthorized=deadline_reached,
                )
                if not user_event and not deadline_reached:
                    # Keep the vehicle sequence pending until the configured
                    # Duration expires so an owner/active borrower read that
                    # arrives after the vehicle can still authorize Check-out.
                    blocked_tag_ids.add(tag_id)
                    continue

            try:
                with self.env.cr.savepoint():
                    transaction = self._create_transaction_for_vehicle(
                        movement_events,
                        event_type,
                        user_event=user_event,
                        vehicle_by_tag=vehicle_by_tag,
                        user_by_tag=user_by_tag,
                    )
                    transactions |= transaction
                    if user_event:
                        consumed_user_ids.add(user_event.id)
            except Exception:
                _logger.exception(
                    "Parking sequence processing failed: lane=%s event_type=%s ids=%s",
                    lane.id, event_type, movement_events.ids,
                )
                movement_events.write({"state": "error"})
                if user_event:
                    user_event.write({"state": "error"})
                    consumed_user_ids.add(user_event.id)

        if finalize_expired:
            self._expire_stale_vehicle_events(lane, now)
            self._expire_orphan_user_events(lane, now)
        return transactions

    @api.model
    def _process_pending_for_lane(self, lane, now=None, finalize_expired=True):
        self.env.cr.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            (f"nsp.parking:lane:{lane.id}",),
        )
        now = fields.Datetime.to_datetime(now or fields.Datetime.now())
        return self._process_sequence_matches(lane, now, finalize_expired=finalize_expired)

    @api.model
    def process_pending_events(self):
        if self._deployment_role() != "edge_server":
            return True
        now = fields.Datetime.now()
        self.env.cr.execute(
            """
            SELECT DISTINCT lane_id
              FROM nsp_parking_detection_event
             WHERE state = 'pending'
               AND transaction_id IS NULL
            """
        )
        lane_ids = [row[0] for row in self.env.cr.fetchall()]
        Lane = self.env["nsp.parking.lane"].sudo()
        for lane in Lane.browse(lane_ids).exists():
            self._process_pending_for_lane(lane, now=now)
        return True

    @api.model
    def cleanup_old_events(self):
        if self._deployment_role() != "edge_server":
            return True
        raw_days = self.env["ir.config_parameter"].sudo().get_param(
            "nsp.parking_detection_retention_days", "7"
        )
        try:
            retention_days = max(1, int(raw_days))
        except Exception:
            retention_days = 7
        terminal_cutoff = fields.Datetime.now() - timedelta(days=retention_days)
        old = self.search([
            ("state", "in", ["processed", "error"]),
            ("detected_at", "<", terminal_cutoff),
        ], order="detected_at asc, id asc", limit=20000)
        if old:
            old.unlink()
        return True
