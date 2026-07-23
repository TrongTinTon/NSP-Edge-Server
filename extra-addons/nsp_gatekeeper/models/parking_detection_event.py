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
    """Short-lived Edge RFID detection used to build parking transactions.

    Controller reports only physical reads. Edge resolves Reader/Antenna/Lane,
    suppresses repeats on the same antenna, resolves Two-way movement from
    Outside/Inside zone transitions, groups the required cards, and creates the
    final ``nsp.parking.transaction``. Detection events never sync to Cloud.
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
        ondelete="restrict", readonly=True,
    )
    antenna_id = fields.Many2one(
        "nsp.device.antenna", string="Antenna", required=True,
        ondelete="restrict", readonly=True,
    )
    zone = fields.Selection(
        [("outside", "Outside"), ("inside", "Inside")],
        string="Zone", readonly=True,
        help="Snapshot of the Two-way antenna zone. Empty for one-way lanes.",
    )
    card_id = fields.Many2one(
        "nsp.rfid.card", string="RFID Card", required=True,
        ondelete="restrict", readonly=True,
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
                   (lane_id, card_id, zone, detected_at, id)
             WHERE state = 'pending' AND transaction_id IS NULL
            """
        )
        self.env.cr.execute(
            """
            CREATE INDEX IF NOT EXISTS nsp_parking_detection_repeat_idx
                ON nsp_parking_detection_event
                   (antenna_id, card_id, detected_at DESC, id DESC)
             WHERE state IN ('pending', 'processed')
            """
        )
        self.env.cr.execute(
            """
            CREATE INDEX IF NOT EXISTS nsp_parking_detection_lane_repeat_idx
                ON nsp_parking_detection_event
                   (lane_id, card_id, detected_at DESC, id DESC)
             WHERE state IN ('pending', 'processed')
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
            "zone": value("zone") or "",
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
        """Resolve all Reader/Antenna/Lane mappings for a Controller batch in four queries."""
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

        missing_whitelist = serials - allowed_serials
        if missing_whitelist:
            Notification = self.env["nsp.notification"].sudo()
            for serial in sorted(missing_whitelist):
                Notification.notify_device_not_whitelisted(
                    serial,
                    controller.controller_id,
                    details={"device_type": "rfid_reader"},
                )

        devices = self.env["nsp.device"].sudo().search([
            ("controller_id", "=", controller.id),
            ("serial_number", "in", list(allowed_serials)),
        ]) if allowed_serials else self.env["nsp.device"].browse()
        device_by_serial = {device.serial_number: device for device in devices}

        antenna_numbers = {antenna_no for _serial, antenna_no in keys}
        antennas = self.env["nsp.device.antenna"].sudo().search([
            ("device_id", "in", devices.ids),
            ("antenna_no", "in", list(antenna_numbers)),
        ]) if devices and antenna_numbers else self.env["nsp.device.antenna"].browse()
        antenna_by_key = {
            (antenna.device_id.serial_number, antenna.antenna_no): antenna
            for antenna in antennas
        }

        mappings = self.env["nsp.parking.lane.antenna.mapping"].sudo().search([
            ("antenna_ref_id", "in", antennas.ids),
            ("lane_id.active", "=", True),
        ]) if antennas else self.env["nsp.parking.lane.antenna.mapping"].browse()
        mapping_by_antenna = {mapping.antenna_ref_id.id: mapping for mapping in mappings}

        resolved = {}
        errors = {}
        for key in keys:
            serial, antenna_no = key
            if serial not in allowed_serials:
                errors[key] = "device_not_whitelisted"
                continue
            device = device_by_serial.get(serial)
            if not device:
                errors[key] = "device_not_found"
                continue
            antenna = antenna_by_key.get(key)
            if not antenna:
                errors[key] = "antenna_not_found"
                continue
            mapping = mapping_by_antenna.get(antenna.id)
            if not mapping:
                errors[key] = "no_antenna_rule"
                continue
            lane = mapping.lane_id
            if lane.controller_id != controller:
                errors[key] = "controller_not_in_scope"
                continue
            if lane.direction == "both" and mapping.zone not in ("outside", "inside"):
                errors[key] = "invalid_antenna_zone"
                continue
            if lane.direction != "both" and mapping.zone:
                errors[key] = "invalid_antenna_zone"
                continue
            resolved[key] = (antenna, lane, mapping.zone or False)
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

        topology_key = (serial_number, antenna_no)
        topology = topology_cache.get(topology_key)
        if topology is None:
            raise ValidationError(_("no_antenna_rule"))
        antenna, lane, zone = topology

        detected_dt = fields.Datetime.to_datetime(detected_at)
        vals = {
            "event_uid": event_uid,
            "detected_at": detected_at,
            "lane_id": lane.id,
            "antenna_id": antenna.id,
            "zone": zone,
            "card_id": card.id,
            "state": "pending",
        }

        # event_uid handles transport retries. Physical repeat suppression has
        # different scope by lane mode: One-way suppresses the same card across
        # the whole Lane, while Two-way suppresses only on the same antenna so
        # a cross-zone read remains available for movement resolution.
        repeat_scope = (
            f"antenna:{antenna.id}" if lane.direction == "both"
            else f"lane:{lane.id}"
        )
        self.env.cr.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            (f"nsp.parking:repeat:{repeat_scope}:{card.id}",),
        )
        existing = self.search([("event_uid", "=", event_uid)], limit=1)
        if existing:
            if self._business_values(existing) != self._business_values(vals):
                raise ValidationError(_(
                    "event_uid_conflict: Detection UID already exists with different data."
                ))
            return existing, True, lane

        repeat_suppression = max(1, int(lane.repeat_suppression_seconds or 1))
        repeat_domain = [
            ("card_id", "=", card.id),
            ("detected_at", ">=", detected_dt - timedelta(seconds=repeat_suppression)),
            ("detected_at", "<=", detected_dt),
            ("state", "in", ["pending", "processed"]),
        ]
        if lane.direction == "both":
            repeat_domain.append(("antenna_id", "=", antenna.id))
        else:
            repeat_domain.append(("lane_id", "=", lane.id))
        duplicate = self.search(
            repeat_domain, order="detected_at desc, id desc", limit=1
        )
        if duplicate:
            return duplicate, True, lane

        record, idempotent_duplicate = self.create_idempotent(vals)
        return record, idempotent_duplicate, lane

    @api.model
    def ingest_controller_detections(self, controller, detections):
        """Persist one Controller batch, then process each touched Lane once."""
        self._ensure_edge_role()
        if not isinstance(detections, list):
            raise ValidationError(_("invalid_payload"))

        topology_cache, topology_errors = self._resolve_topology_batch(controller, detections)
        touched_lanes = self.env["nsp.parking.lane"].browse()
        for payload, card in detections:
            topology_key = (
                str(payload.get("serial_number") or "").strip().upper(),
                int(payload.get("antenna_no") or 0),
            )
            topology_error = topology_errors.get(topology_key)
            if topology_error:
                _logger.warning(
                    "Parking detection ignored: controller=%s serial=%s antenna=%s tid=%s reason=%s",
                    controller.controller_id, topology_key[0], topology_key[1], payload.get("tid"), topology_error,
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
            # During API ingestion, never expire an incomplete movement/group.
            # A Controller may send detections from the same physical crossing in
            # separate HTTP requests. Keep pending state so the next request can
            # complete the transition/group. Timeout/finalization belongs to cron.
            self._process_pending_for_lane(lane, finalize_expired=False)
        return True

    @api.model
    def _pending_user_pool(self, lane):
        """Load pending User reads once for one lane-processing pass."""
        events = self.search([
            ("lane_id", "=", lane.id),
            ("state", "=", "pending"),
            ("transaction_id", "=", False),
            ("card_id.card_type", "=", "user_card"),
        ], order="detected_at asc, id asc")
        return events, [event.detected_at for event in events]

    @api.model
    def _nearest_user_from_pool(self, user_events, user_times, anchor_at, window_seconds, consumed_ids):
        """Return the nearest still-unused User read without another DB query."""
        if not user_events:
            return self.browse()
        window = max(1, int(window_seconds or 3))
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
    def _expire_orphan_user_events(self, lane, now):
        """Discard old User reads that were never paired with a Check-out."""
        cutoff = now - timedelta(seconds=max(1, int(lane.grouping_window_seconds or 3)))
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
        """Resolve active Vehicle/User card assignments for a pending Lane snapshot."""
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
    def _create_transaction_for_vehicle(
        self, lane, vehicle_events, direction, user_event=False,
        vehicle_by_card=None, user_by_card=None,
    ):
        group = vehicle_events
        if direction == "exit" and user_event:
            group |= user_event
        transaction = self.env["nsp.parking.transaction"].sudo().create_from_detection_group(
            group,
            resolved_direction=direction,
            vehicle_by_card=vehicle_by_card,
            user_by_card=user_by_card,
        )
        group.write({"state": "processed", "transaction_id": transaction.id})
        return transaction

    @api.model
    def _process_one_way_lane(self, lane, now, finalize_expired=True):
        """Process one-way vehicle reads with one vehicle query and one User pool query."""
        transactions = self.env["nsp.parking.transaction"].browse()
        pairing_window = max(1, int(lane.grouping_window_seconds or 3))
        vehicle_events = self.search([
            ("lane_id", "=", lane.id),
            ("state", "=", "pending"),
            ("transaction_id", "=", False),
            ("card_id.card_type", "=", "vehicle_card"),
        ], order="detected_at asc, id asc")
        if not vehicle_events:
            if finalize_expired:
                self._expire_orphan_user_events(lane, now)
            return transactions

        user_events, user_times = (
            self._pending_user_pool(lane)
            if lane.direction == "exit"
            else (self.browse(), [])
        )
        vehicle_by_card, user_by_card = self._assignment_maps(vehicle_events | user_events)
        consumed_user_ids = set()
        blocked_card_ids = set()

        for vehicle_event in vehicle_events:
            card_id = vehicle_event.card_id.id
            if card_id in blocked_card_ids or vehicle_event.state != "pending" or vehicle_event.transaction_id:
                continue

            user_event = self.browse()
            if lane.direction == "exit":
                user_event = self._nearest_user_from_pool(
                    user_events,
                    user_times,
                    vehicle_event.detected_at,
                    pairing_window,
                    consumed_user_ids,
                )
                deadline = vehicle_event.detected_at + timedelta(seconds=pairing_window)
                if not user_event and (not finalize_expired or now < deadline):
                    # Preserve ordering for this vehicle, but do not block other vehicles.
                    blocked_card_ids.add(card_id)
                    continue

            try:
                with self.env.cr.savepoint():
                    transaction = self._create_transaction_for_vehicle(
                        lane,
                        vehicle_event,
                        lane.direction,
                        user_event=user_event,
                        vehicle_by_card=vehicle_by_card,
                        user_by_card=user_by_card,
                    )
                    transactions |= transaction
                    if user_event:
                        consumed_user_ids.add(user_event.id)
            except Exception:
                _logger.exception(
                    "Parking one-way vehicle processing failed: lane=%s detection=%s",
                    lane.id, vehicle_event.id,
                )
                vehicle_event.write({"state": "error"})
                if user_event:
                    user_event.write({"state": "error"})
                    consumed_user_ids.add(user_event.id)

        if finalize_expired:
            self._expire_orphan_user_events(lane, now)
        return transactions

    @api.model
    def _two_way_transitions(self, lane):
        """Build all non-overlapping Vehicle zone transitions in one pass."""
        vehicle_events = self.search([
            ("lane_id", "=", lane.id),
            ("state", "=", "pending"),
            ("transaction_id", "=", False),
            ("card_id.card_type", "=", "vehicle_card"),
        ], order="card_id asc, detected_at asc, id asc")
        if not vehicle_events:
            return vehicle_events, []

        transition_window = max(1, int(lane.transition_window_seconds or 10))
        events_by_card = {}
        for event in vehicle_events:
            events_by_card.setdefault(event.card_id.id, []).append(event)

        transitions = []
        for card_id, events in events_by_card.items():
            cluster = []
            previous = None
            for event in events:
                if event.zone not in ("outside", "inside"):
                    continue
                if previous is None:
                    cluster = [event]
                    previous = event
                    continue

                gap = (event.detected_at - previous.detected_at).total_seconds()
                if gap > transition_window:
                    cluster = [event]
                    previous = event
                    continue

                cluster.append(event)
                if event.zone == previous.zone:
                    previous = event
                    continue

                transitions.append({
                    "card_id": card_id,
                    "direction": "entry" if previous.zone == "outside" else "exit",
                    "start_at": cluster[0].detected_at,
                    "end_at": event.detected_at,
                    "events": self.browse([item.id for item in cluster]),
                })
                # A physical read can belong to only one movement transition.
                cluster = []
                previous = None

        transitions.sort(key=lambda item: (item["end_at"], item["start_at"], item["card_id"]))
        return vehicle_events, transitions

    @api.model
    def _expire_stale_two_way_vehicle_events(self, lane, now):
        cutoff = now - timedelta(seconds=max(1, int(lane.transition_window_seconds or 10)))
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
    def _process_two_way_lane(self, lane, now, finalize_expired=True):
        """Resolve Two-way movements once per pending snapshot, not once per transaction."""
        transactions = self.env["nsp.parking.transaction"].browse()
        _vehicle_events, transitions = self._two_way_transitions(lane)
        if not transitions:
            if finalize_expired:
                self._expire_stale_two_way_vehicle_events(lane, now)
                self._expire_orphan_user_events(lane, now)
            return transactions

        pairing_window = max(1, int(lane.grouping_window_seconds or 3))
        user_events, user_times = self._pending_user_pool(lane)
        vehicle_events = self.browse(
            [event.id for transition in transitions for event in transition["events"]]
        )
        vehicle_by_card, user_by_card = self._assignment_maps(vehicle_events | user_events)
        consumed_user_ids = set()
        blocked_card_ids = set()

        for transition in transitions:
            card_id = transition["card_id"]
            movement_events = transition["events"].filtered(
                lambda rec: rec.state == "pending" and not rec.transaction_id
            )
            if not movement_events or card_id in blocked_card_ids:
                continue

            direction = transition["direction"]
            user_event = self.browse()
            if direction == "exit":
                user_event = self._nearest_user_from_pool(
                    user_events,
                    user_times,
                    transition["end_at"],
                    pairing_window,
                    consumed_user_ids,
                )
                deadline = transition["end_at"] + timedelta(seconds=pairing_window)
                if not user_event and (not finalize_expired or now < deadline):
                    # Keep this vehicle's later transitions behind the unresolved exit.
                    blocked_card_ids.add(card_id)
                    continue

            try:
                with self.env.cr.savepoint():
                    transaction = self._create_transaction_for_vehicle(
                        lane,
                        movement_events,
                        direction,
                        user_event=user_event,
                        vehicle_by_card=vehicle_by_card,
                        user_by_card=user_by_card,
                    )
                    transactions |= transaction
                    if user_event:
                        consumed_user_ids.add(user_event.id)
            except Exception:
                _logger.exception(
                    "Parking Two-way transition processing failed: lane=%s direction=%s ids=%s",
                    lane.id, direction, movement_events.ids,
                )
                movement_events.write({"state": "error"})
                if user_event:
                    user_event.write({"state": "error"})
                    consumed_user_ids.add(user_event.id)

        if finalize_expired:
            self._expire_stale_two_way_vehicle_events(lane, now)
            self._expire_orphan_user_events(lane, now)
        return transactions

    @api.model
    def _process_pending_for_lane(self, lane, now=None, finalize_expired=True):
        self.env.cr.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            (f"nsp.parking:lane:{lane.id}",),
        )
        now = fields.Datetime.to_datetime(now or fields.Datetime.now())
        if lane.direction == "both":
            return self._process_two_way_lane(
                lane, now, finalize_expired=finalize_expired
            )
        return self._process_one_way_lane(
            lane, now, finalize_expired=finalize_expired
        )

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
