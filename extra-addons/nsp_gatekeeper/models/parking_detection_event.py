# -*- coding: utf-8 -*-
import os
import logging
from datetime import timedelta

from psycopg2 import IntegrityError

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


_logger = logging.getLogger(__name__)


class ParkingDetectionEvent(models.Model):
    """One accepted RFID TID detection used by Edge parking processing.

    Repeated reader output is suppressed before persistence. This model is
    short-lived, never synchronized to Cloud, and contains only the fields
    required for grouping, idempotency, processing state, and transaction linkage.
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
    direction = fields.Selection(
        [
            ("entry", "Entry"),
            ("exit", "Exit"),
            ("both", "Two-way"),
        ],
        string="Mapped Direction", required=True, readonly=True,
        help="Server-side antenna mapping direction used by Edge. Two-way is a configuration value, not a measured travel direction.",
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
        """Add partial indexes for the high-volume Edge processing paths."""
        self.env.cr.execute(
            """
            CREATE INDEX IF NOT EXISTS nsp_parking_detection_pending_group_idx
                ON nsp_parking_detection_event
                   (lane_id, direction, detected_at, id)
             WHERE state = 'pending' AND transaction_id IS NULL
            """
        )
        self.env.cr.execute(
            """
            CREATE INDEX IF NOT EXISTS nsp_parking_detection_dedup_idx
                ON nsp_parking_detection_event
                   (lane_id, direction, card_id, detected_at DESC, id DESC)
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
            "direction": value("direction") or "",
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
        if mapping.lane_id.controller_id != controller:
            raise ValidationError(_("controller_not_in_scope"))
        return antenna, mapping.lane_id, mapping.direction

    @api.model
    def ingest_controller_detection(self, controller, payload, card):
        """Validate topology and persist one API-approved registered TID."""
        self._ensure_edge_role()
        if not isinstance(payload, dict):
            raise ValidationError(_("invalid_payload"))

        controller_code = str(payload.get("controller_code") or "").strip()
        if controller_code and controller_code != controller.controller_id:
            raise ValidationError(_("route_not_allowed"))
        event_uid = str(payload.get("event_uid") or "").strip()
        serial_number = str(payload.get("serial_number") or "").strip().upper()
        tid = self.env["nsp.rfid.card"]._normalize_tid(payload.get("tid"))
        try:
            antenna_no = int(payload.get("antenna_no") or 0)
        except Exception as exc:
            raise ValidationError(_("invalid_payload: antenna_no")) from exc
        detected_at = self.env["nsp.parking.transaction"]._safe_datetime_value(
            payload.get("detected_at"), default_now=False
        )
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

        antenna, lane, mapped_direction = self._resolve_topology(
            controller, serial_number, antenna_no
        )
        direction = mapped_direction
        if not card or card._name != "nsp.rfid.card" or not card.exists():
            raise ValidationError(_("invalid_rfid_card"))
        card.ensure_one()
        if card.tid != tid:
            raise ValidationError(_("invalid_rfid_card"))

        detected_dt = fields.Datetime.to_datetime(detected_at)
        grouping_window = max(1, int(lane.grouping_window_seconds or 3))

        vals = {
            "event_uid": event_uid,
            "detected_at": detected_at,
            "lane_id": lane.id,
            "antenna_id": antenna.id,
            "direction": direction,
            "card_id": card.id,
            "state": "pending",
        }

        # Serialize detections of the same TID before checking idempotency and
        # suppression. Repeated reader output is acknowledged but not stored.
        self.env.cr.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            (f"nsp.parking:dedup:{lane.id}:{direction}:{card.id}",),
        )
        existing = self.search([("event_uid", "=", event_uid)], limit=1)
        if existing:
            if self._business_values(existing) != self._business_values(vals):
                raise ValidationError(_(
                    "event_uid_conflict: Detection UID already exists with different data."
                ))
            return existing, True, self.env["nsp.parking.transaction"].browse()

        suppression = max(0, int(lane.duplicate_suppression_seconds or 0))
        deduplication_window = max(grouping_window, suppression)
        duplicate = self.browse()
        if deduplication_window:
            duplicate = self.search([
                ("lane_id", "=", lane.id),
                ("direction", "=", direction),
                ("card_id", "=", card.id),
                ("detected_at", ">=", detected_dt - timedelta(seconds=deduplication_window)),
                ("detected_at", "<=", detected_dt),
                ("state", "in", ["pending", "processed"]),
            ], order="detected_at desc, id desc", limit=1)
        if duplicate:
            return duplicate, True, self.env["nsp.parking.transaction"].browse()

        record, idempotent_duplicate = self.create_idempotent(vals)
        transactions = self._process_pending_for_lane(lane, direction)
        return record, idempotent_duplicate, transactions

    @api.model
    def _group_is_complete(self, lane, events):
        has_vehicle = bool(events.filtered(
            lambda rec: rec.card_id.card_type == "vehicle_card"
        ))
        has_user = bool(events.filtered(
            lambda rec: rec.card_id.card_type == "user_card"
        ))
        return (
            (not lane.required_vehicle_tid or has_vehicle)
            and (not lane.required_user_tid or has_user)
        )

    @api.model
    def _process_pending_for_lane(self, lane, direction, now=None):
        """Process complete groups immediately and expired incomplete groups."""
        self.env.cr.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            (f"nsp.parking:{lane.id}:{direction}",),
        )
        now = fields.Datetime.to_datetime(now or fields.Datetime.now())
        transactions = self.env["nsp.parking.transaction"].browse()
        while True:
            first = self.search([
                ("lane_id", "=", lane.id),
                ("direction", "=", direction),
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
                ("direction", "=", direction),
                ("state", "=", "pending"),
                ("transaction_id", "=", False),
                ("detected_at", ">=", first.detected_at),
                ("detected_at", "<=", deadline),
            ], order="detected_at asc, id asc")
            if not self._group_is_complete(lane, group) and now < deadline:
                break
            try:
                with self.env.cr.savepoint():
                    transaction = self.env["nsp.parking.transaction"].sudo().create_from_detection_group(group)
                    group.write({"state": "processed", "transaction_id": transaction.id})
                    transactions |= transaction
            except Exception:
                _logger.exception(
                    "Parking detection group processing failed: lane=%s direction=%s ids=%s",
                    lane.id, direction, group.ids,
                )
                group.write({"state": "error"})
        return transactions

    @api.model
    def process_pending_events(self):
        """Cron fallback for groups that expire without another detection."""
        if self._deployment_role() != "edge_server":
            return True
        now = fields.Datetime.now()
        self.env.cr.execute(
            """
            SELECT DISTINCT lane_id, direction
              FROM nsp_parking_detection_event
             WHERE state = 'pending'
               AND transaction_id IS NULL
            """
        )
        pairs = self.env.cr.fetchall()
        Lane = self.env["nsp.parking.lane"].sudo()
        for lane_id, direction in pairs:
            lane = Lane.browse(lane_id).exists()
            if lane:
                self._process_pending_for_lane(lane, direction, now=now)
        return True

    @api.model
    def cleanup_old_events(self):
        """Keep raw Edge detections only for a short, configurable period."""
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
