# -*- coding: utf-8 -*-
import logging
import os
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
    def _resolve_topology(self, controller, serial_number, antenna_no):
        if not self.env["nsp.device.whitelist"].sudo().is_device_whitelisted(serial_number):
            if "nsp.notification" in self.env.registry.models:
                self.env["nsp.notification"].sudo().notify_device_not_whitelisted(
                    serial_number,
                    controller.controller_id,
                    details={"device_type": "rfid_reader"},
                )
            raise ValidationError(_("device_not_whitelisted"))

        device = self.env["nsp.device"].sudo().search([
            ("controller_id", "=", controller.id),
            ("serial_number", "=", serial_number),
        ], limit=1)
        if not device:
            raise ValidationError(_("device_not_found"))
        antenna = self.env["nsp.device.antenna"].sudo().search([
            ("device_id", "=", device.id),
            ("antenna_no", "=", antenna_no),
        ], limit=1)
        if not antenna:
            raise ValidationError(_("antenna_not_found"))
        mapping = self.env["nsp.parking.lane.antenna.mapping"].sudo().search([
            ("antenna_ref_id", "=", antenna.id),
            ("lane_id.active", "=", True),
        ], limit=1)
        if not mapping:
            raise ValidationError(_("no_antenna_rule"))
        lane = mapping.lane_id
        if lane.controller_id != controller:
            raise ValidationError(_("controller_not_in_scope"))
        if lane.direction == "both" and mapping.zone not in ("outside", "inside"):
            raise ValidationError(_("invalid_antenna_zone"))
        if lane.direction != "both" and mapping.zone:
            raise ValidationError(_("invalid_antenna_zone"))
        return antenna, lane, mapping.zone or False

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
            topology = self._resolve_topology(controller, serial_number, antenna_no)
            topology_cache[topology_key] = topology
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

        topology_cache = {}
        touched_lanes = self.env["nsp.parking.lane"].browse()
        for payload, card in detections:
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
    def _group_is_complete(self, lane, events, direction):
        has_vehicle = bool(events.filtered(
            lambda rec: rec.card_id.card_type == "vehicle_card"
        ))
        has_user = bool(events.filtered(
            lambda rec: rec.card_id.card_type == "user_card"
        ))
        return has_vehicle and (not lane._requires_user_tid(direction) or has_user)

    @api.model
    def _process_one_way_lane(self, lane, now, finalize_expired=True):
        transactions = self.env["nsp.parking.transaction"].browse()
        while True:
            first = self.search([
                ("lane_id", "=", lane.id),
                ("state", "=", "pending"),
                ("transaction_id", "=", False),
            ], order="detected_at asc, id asc", limit=1)
            if not first:
                break
            deadline = first.detected_at + timedelta(
                seconds=max(1, int(lane.grouping_window_seconds or 3))
            )
            group = self.search([
                ("lane_id", "=", lane.id),
                ("state", "=", "pending"),
                ("transaction_id", "=", False),
                ("detected_at", ">=", first.detected_at),
                ("detected_at", "<=", deadline),
            ], order="detected_at asc, id asc")
            if not self._group_is_complete(lane, group, lane.direction):
                if not finalize_expired or now < deadline:
                    break
            has_vehicle = bool(group.filtered(
                lambda rec: rec.card_id.card_type == "vehicle_card"
            ))
            if not has_vehicle:
                # Parking transactions are vehicle-centric. A group without a
                # registered vehicle card is not a parking movement.
                group.write({"state": "error"})
                continue
            try:
                with self.env.cr.savepoint():
                    transaction = self.env["nsp.parking.transaction"].sudo().create_from_detection_group(
                        group, resolved_direction=lane.direction
                    )
                    group.write({"state": "processed", "transaction_id": transaction.id})
                    transactions |= transaction
            except Exception:
                _logger.exception(
                    "Parking one-way detection group failed: lane=%s ids=%s",
                    lane.id, group.ids,
                )
                group.write({"state": "error"})
        return transactions

    @api.model
    def _first_card_transition(self, events, transition_window):
        """Return the first valid zone change for one card.

        Same-zone reads update the latest source sample. A long gap resets the
        source cluster, preventing an old detection from being paired with a new
        crossing. All events in the source cluster through destination are linked
        to the resulting transaction.
        """
        ordered = events.sorted(key=lambda rec: (rec.detected_at, rec.id))
        previous = False
        cluster_start = False
        for rec in ordered:
            if rec.zone not in ("outside", "inside"):
                continue
            if not previous:
                previous = rec
                cluster_start = rec
                continue
            delta = (rec.detected_at - previous.detected_at).total_seconds()
            if rec.zone == previous.zone:
                if delta > transition_window:
                    cluster_start = rec
                previous = rec
                continue
            if delta <= transition_window:
                direction = "entry" if previous.zone == "outside" else "exit"
                transition_events = ordered.filtered(
                    lambda item: cluster_start.detected_at <= item.detected_at <= rec.detected_at
                )
                return {
                    "card_id": rec.card_id.id,
                    "card_type": rec.card_id.card_type,
                    "direction": direction,
                    "start_at": cluster_start.detected_at,
                    "end_at": rec.detected_at,
                    "events": transition_events,
                }
            previous = rec
            cluster_start = rec
        return False

    @api.model
    def _available_two_way_transitions(self, lane):
        pending = self.search([
            ("lane_id", "=", lane.id),
            ("state", "=", "pending"),
            ("transaction_id", "=", False),
        ], order="detected_at asc, id asc", limit=2000)
        if not pending:
            return pending, []
        transition_window = max(1, int(lane.transition_window_seconds or 10))
        transitions = []
        for card_id in sorted(set(pending.mapped("card_id").ids)):
            card_events = pending.filtered(lambda rec: rec.card_id.id == card_id)
            if not card_events or card_events[:1].card_id.card_type != "vehicle_card":
                continue
            transition = self._first_card_transition(card_events, transition_window)
            if transition:
                transitions.append(transition)
        transitions.sort(key=lambda item: (item["end_at"], item["start_at"], item["card_id"]))
        return pending, transitions

    @api.model
    def _process_two_way_lane(self, lane, now, finalize_expired=True):
        transactions = self.env["nsp.parking.transaction"].browse()
        transition_window = max(1, int(lane.transition_window_seconds or 10))
        grouping_window = max(1, int(lane.grouping_window_seconds or 3))

        while True:
            pending, transitions = self._available_two_way_transitions(lane)
            if not pending:
                break

            if not transitions:
                # Ingestion may arrive as multiple HTTP requests. Never expire an
                # unmatched first-zone detection while handling a request; otherwise
                # request #1 could be discarded before request #2 reaches Edge.
                if not finalize_expired:
                    break
                first = pending[:1]
                # User-card reads may legitimately arrive before the vehicle
                # crossing. Keep them through one transition + grouping window.
                wait_seconds = transition_window
                if first.card_id.card_type == "user_card":
                    wait_seconds += grouping_window
                if now < first.detected_at + timedelta(seconds=wait_seconds):
                    break
                # A read that cannot be attached to a vehicle movement is not a
                # Parking Transaction and must not block later transitions.
                first.write({"state": "error"})
                continue

            # Vehicle RFID is mandatory and is the only movement anchor.
            # User RFID is identity evidence only; it does not determine direction.
            anchor = transitions[0]
            direction = anchor["direction"]
            group_start = anchor["start_at"] - timedelta(seconds=grouping_window)
            group_deadline = anchor["end_at"] + timedelta(seconds=grouping_window)

            selected_vehicle_transitions = [
                item for item in transitions
                if item["direction"] == direction
                and group_start <= item["end_at"] <= group_deadline
            ]
            group = self.browse()
            for item in selected_vehicle_transitions:
                group |= item["events"]

            user_events = pending.filtered(
                lambda rec: rec.card_id.card_type == "user_card"
                and group_start <= rec.detected_at <= group_deadline
            )
            group |= user_events

            if not self._group_is_complete(lane, group, direction):
                if not finalize_expired or now < group_deadline:
                    break

            try:
                with self.env.cr.savepoint():
                    transaction = self.env["nsp.parking.transaction"].sudo().create_from_detection_group(
                        group, resolved_direction=direction
                    )
                    group.write({"state": "processed", "transaction_id": transaction.id})
                    transactions |= transaction
            except Exception:
                _logger.exception(
                    "Parking Two-way transition processing failed: lane=%s direction=%s ids=%s",
                    lane.id, direction, group.ids,
                )
                group.write({"state": "error"})
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
