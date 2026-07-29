# -*- coding: utf-8 -*-
import logging
import os
from bisect import bisect_left
from datetime import timedelta

from psycopg2 import IntegrityError

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


_logger = logging.getLogger(__name__)


class ParkingDetectionEvent(models.Model):
    """Short-lived Edge RFID read used to build final parking transactions.

    Controller reports only physical reads. Edge resolves the Reader/Antenna/Lane,
    then matches the same Vehicle RFID against a Cloud-configured directed antenna
    transition. A transition carries its own Event Type and Duration, so no fixed
    lane-wide grouping/transition window is required. Raw detections stay on
    Edge and never synchronize to Cloud.
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
    card_id = fields.Many2one(
        "nsp.rfid.card", string="RFID Card", required=True,
        ondelete="restrict", readonly=True, index=True,
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
            CREATE INDEX IF NOT EXISTS nsp_parking_detection_transition_idx
                ON nsp_parking_detection_event
                   (lane_id, card_id, antenna_id, detected_at, id)
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
            "card_id": int(value("card_id") or 0),
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
        """Resolve Reader/Antenna to one active Parking Lane for this Controller."""
        keys = {
            (
                str(payload.get("serial_number") or "").strip().upper(),
                int(payload.get("antenna_no") or 0),
            )
            for payload, _card in detections
        }
        keys.discard(("", 0))
        if not keys:
            return {}, {}

        serials = {serial for serial, _antenna_no in keys}
        allowed_serials = set(
            self.env["nsp.device.whitelist"].sudo().search([
                ("serial_number", "in", list(serials)),
            ]).mapped("serial_number")
        )

        devices = self.env["nsp.device"].sudo().search([
            ("controller_id", "=", controller.id),
            ("serial_number", "in", list(allowed_serials)),
            ("active", "=", True),
        ]) if allowed_serials else self.env["nsp.device"].browse()
        device_by_serial = {device.serial_number: device for device in devices}

        antenna_numbers = {antenna_no for _serial, antenna_no in keys}
        antennas = self.env["nsp.device.antenna"].sudo().search([
            ("device_id", "in", devices.ids),
            ("antenna_no", "in", list(antenna_numbers)),
            ("active", "=", True),
        ]) if devices and antenna_numbers else self.env["nsp.device.antenna"].browse()
        antenna_by_key = {
            (antenna.device_id.serial_number, antenna.antenna_no): antenna
            for antenna in antennas
        }

        Transition = self.env["nsp.parking.antenna.transition"].sudo()
        transitions = Transition.search([
            ("lane_id.active", "=", True),
            "|",
            ("from_antenna_id", "in", antennas.ids),
            ("to_antenna_id", "in", antennas.ids),
        ]) if antennas else Transition.browse()

        lanes_by_antenna = {}
        for transition in transitions:
            for antenna in (transition.from_antenna_id, transition.to_antenna_id):
                if antenna.id not in antennas.ids:
                    continue
                lanes_by_antenna.setdefault(antenna.id, set()).add(transition.lane_id.id)

        resolved = {}
        errors = {}
        Lane = self.env["nsp.parking.lane"].sudo()
        for key in keys:
            serial, _antenna_no = key
            if serial not in allowed_serials:
                errors[key] = "device_not_whitelisted"
                continue
            if serial not in device_by_serial:
                errors[key] = "device_not_found"
                continue
            antenna = antenna_by_key.get(key)
            if not antenna:
                errors[key] = "antenna_not_found"
                continue
            lane_ids = lanes_by_antenna.get(antenna.id, set())
            if not lane_ids:
                errors[key] = "no_antenna_transition"
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
    def _ingest_controller_detection(self, controller, payload, card, topology_cache):
        if not isinstance(payload, dict):
            raise ValidationError(_("invalid_payload"))

        event_uid = str(payload.get("event_uid") or "").strip()
        serial_number = str(payload.get("serial_number") or "").strip().upper()
        tid = self.env["nsp.rfid.card"]._normalize_tid(payload.get("tid"))
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
        if not card or card._name != "nsp.rfid.card" or not card.exists():
            raise ValidationError(_("invalid_rfid_card"))
        card.ensure_one()
        if card.tid != tid:
            raise ValidationError(_("invalid_rfid_card"))

        topology = topology_cache.get((serial_number, antenna_no))
        if topology is None:
            raise ValidationError(_("no_antenna_transition"))
        antenna, lane = topology

        vals = {
            "event_uid": event_uid,
            "detected_at": detected_at,
            "lane_id": lane.id,
            "antenna_id": antenna.id,
            "card_id": card.id,
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
        for payload, card in detections:
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
                        controller, payload, card, topology_cache
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
            # transitions; finalization belongs to the periodic pending-event job.
            self._process_pending_for_lane(lane, finalize_expired=False)
        return True

    @api.model
    def _pending_user_pool(self, lane):
        events = self.search([
            ("lane_id", "=", lane.id),
            ("state", "=", "pending"),
            ("transaction_id", "=", False),
            ("card_id.card_type", "=", "user_card"),
        ], order="detected_at asc, id asc")
        return events, [event.detected_at for event in events]

    @api.model
    def _nearest_user_from_pool(
        self, user_events, user_times, anchor_at, window_seconds, consumed_ids
    ):
        """Return nearest unused User read inside this transition's measured Duration."""
        if not user_events:
            return self.browse()
        window = max(0.001, float(window_seconds or 0.0))
        index = bisect_left(user_times, anchor_at)
        left = index - 1
        right = index

        while left >= 0 or right < len(user_events):
            candidates = []
            if left >= 0:
                candidates.append(user_events[left])
            if right < len(user_events):
                candidates.append(user_events[right])
            candidates.sort(
                key=lambda rec: (
                    abs((rec.detected_at - anchor_at).total_seconds()),
                    rec.detected_at,
                    rec.id,
                )
            )
            best = candidates[0]
            distance = abs((best.detected_at - anchor_at).total_seconds())
            if distance > window:
                return self.browse()
            if best.id not in consumed_ids and best.state == "pending" and not best.transaction_id:
                return best
            if left >= 0 and best.id == user_events[left].id:
                left -= 1
            else:
                right += 1
        return self.browse()

    @api.model
    def _lane_max_duration(self, lane):
        durations = lane.antenna_transition_ids.mapped("duration_seconds")
        return max([float(value or 0.0) for value in durations] or [1.0])

    @api.model
    def _expire_orphan_user_events(self, lane, now):
        cutoff = now - timedelta(seconds=self._lane_max_duration(lane))
        stale = self.search([
            ("lane_id", "=", lane.id),
            ("state", "=", "pending"),
            ("transaction_id", "=", False),
            ("card_id.card_type", "=", "user_card"),
            ("detected_at", "<", cutoff),
        ])
        if stale:
            stale.write({"state": "error"})

    @api.model
    def _assignment_maps(self, events):
        card_ids = events.mapped("card_id").ids
        if not card_ids:
            return {}, {}
        vehicle_lines = self.env["nsp.vehicle.card"].sudo().search([
            ("card_id", "in", card_ids),
            ("state", "=", "active"),
            ("vehicle_id.active", "=", True),
        ])
        user_lines = self.env["nsp.user.card"].sudo().search([
            ("card_id", "in", card_ids),
            ("state", "=", "active"),
            ("user_id.active", "=", True),
        ])
        return (
            {line.card_id.id: line.vehicle_id for line in vehicle_lines},
            {line.card_id.id: line.user_id for line in user_lines},
        )

    @api.model
    def _build_vehicle_transitions(self, lane):
        """Build non-overlapping directed transitions from pending Vehicle reads.

        The target read chooses the nearest earlier source read that matches a
        configured rule and is inside that rule's Duration. Repeated reads on one
        antenna therefore do not need a separate fixed suppression window.
        """
        rules = lane.antenna_transition_ids
        if not rules:
            return []

        vehicle_events = self.search([
            ("lane_id", "=", lane.id),
            ("state", "=", "pending"),
            ("transaction_id", "=", False),
            ("card_id.card_type", "=", "vehicle_card"),
        ], order="card_id asc, detected_at asc, id asc")
        if not vehicle_events:
            return []

        rules_by_target = {}
        for rule in rules:
            rules_by_target.setdefault(rule.to_antenna_id.id, []).append(rule)

        events_by_card = {}
        for event in vehicle_events:
            events_by_card.setdefault(event.card_id.id, []).append(event)

        transitions = []
        for card_id, events in events_by_card.items():
            used_ids = set()
            seen_by_antenna = {}
            for target in events:
                candidate_rules = rules_by_target.get(target.antenna_id.id, [])
                candidates = []
                for rule in candidate_rules:
                    for source in reversed(seen_by_antenna.get(rule.from_antenna_id.id, [])):
                        if source.id in used_ids:
                            continue
                        gap = (target.detected_at - source.detected_at).total_seconds()
                        if gap < 0:
                            continue
                        if gap <= float(rule.duration_seconds or 0.0):
                            candidates.append((gap, source.detected_at, source.id, rule.id, source, rule))
                            break
                if candidates and target.id not in used_ids:
                    _gap, _at, _source_id, _rule_id, source, rule = min(candidates)
                    used_ids.add(source.id)
                    used_ids.add(target.id)
                    transitions.append({
                        "card_id": card_id,
                        "event_type": rule.event_type,
                        "duration_seconds": float(rule.duration_seconds or 0.0),
                        "start_at": source.detected_at,
                        "end_at": target.detected_at,
                        "events": source | target,
                        "rule": rule,
                    })
                seen_by_antenna.setdefault(target.antenna_id.id, []).append(target)

        transitions.sort(
            key=lambda item: (item["end_at"], item["start_at"], item["card_id"])
        )
        return transitions

    @api.model
    def _expire_stale_vehicle_events(self, lane, now):
        cutoff = now - timedelta(seconds=self._lane_max_duration(lane))
        stale = self.search([
            ("lane_id", "=", lane.id),
            ("state", "=", "pending"),
            ("transaction_id", "=", False),
            ("card_id.card_type", "=", "vehicle_card"),
            ("detected_at", "<", cutoff),
        ])
        if stale:
            stale.write({"state": "error"})

    @api.model
    def _create_transaction_for_vehicle(
        self, vehicle_events, event_type, user_event=False,
        vehicle_by_card=None, user_by_card=None,
    ):
        group = vehicle_events
        if event_type == "check_out" and user_event:
            group |= user_event
        transaction = self.env["nsp.parking.transaction"].sudo().create_from_detection_group(
            group,
            resolved_event_type=event_type,
            vehicle_by_card=vehicle_by_card,
            user_by_card=user_by_card,
        )
        group.write({"state": "processed", "transaction_id": transaction.id})
        return transaction

    @api.model
    def _process_transitions(self, lane, now, finalize_expired=True):
        transactions = self.env["nsp.parking.transaction"].browse()
        transitions = self._build_vehicle_transitions(lane)
        if not transitions:
            if finalize_expired:
                self._expire_stale_vehicle_events(lane, now)
                self._expire_orphan_user_events(lane, now)
            return transactions

        user_events, user_times = self._pending_user_pool(lane)
        vehicle_events = self.browse([
            event.id
            for transition in transitions
            for event in transition["events"]
        ])
        vehicle_by_card, user_by_card = self._assignment_maps(vehicle_events | user_events)
        consumed_user_ids = set()
        blocked_card_ids = set()

        for transition in transitions:
            card_id = transition["card_id"]
            movement_events = transition["events"].filtered(
                lambda rec: rec.state == "pending" and not rec.transaction_id
            )
            if len(movement_events) != 2 or card_id in blocked_card_ids:
                continue

            event_type = transition["event_type"]
            duration = transition["duration_seconds"]

            # Use the configured transition Duration as the physical debounce window too.
            # This prevents lingering reads from creating duplicate business transactions
            # without reintroducing a global fixed suppression value.
            vehicle_event = movement_events.filtered(
                lambda rec: rec.card_id.card_type == "vehicle_card"
            ).sorted(key=lambda rec: (rec.detected_at, rec.id))[-1:]
            vehicle_tid = vehicle_event.card_id.tid if vehicle_event else False
            recent = self.env["nsp.parking.transaction"].sudo().search([
                ("lane_id", "=", lane.id),
                ("event_type", "=", event_type),
                ("vehicle_tid", "=", vehicle_tid),
                ("event_time", ">=", transition["end_at"] - timedelta(seconds=duration)),
                ("event_time", "<=", transition["end_at"]),
            ], order="event_time desc, id desc", limit=1) if vehicle_tid else self.env["nsp.parking.transaction"].browse()
            if recent:
                movement_events.write({"state": "processed", "transaction_id": recent.id})
                continue

            user_event = self.browse()
            if event_type == "check_out":
                user_event = self._nearest_user_from_pool(
                    user_events,
                    user_times,
                    transition["end_at"],
                    duration,
                    consumed_user_ids,
                )
                deadline = transition["end_at"] + timedelta(seconds=duration)
                if not user_event and (not finalize_expired or now < deadline):
                    blocked_card_ids.add(card_id)
                    continue

            try:
                with self.env.cr.savepoint():
                    transaction = self._create_transaction_for_vehicle(
                        movement_events,
                        event_type,
                        user_event=user_event,
                        vehicle_by_card=vehicle_by_card,
                        user_by_card=user_by_card,
                    )
                    transactions |= transaction
                    if user_event:
                        consumed_user_ids.add(user_event.id)
            except Exception:
                _logger.exception(
                    "Parking transition processing failed: lane=%s event_type=%s ids=%s",
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
        return self._process_transitions(lane, now, finalize_expired=finalize_expired)

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
