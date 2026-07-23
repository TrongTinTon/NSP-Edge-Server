# -*- coding: utf-8 -*-
from datetime import datetime, timezone
from uuid import uuid4

from psycopg2 import IntegrityError

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class ParkingTransaction(models.Model):
    """Final parking business transaction created by an Edge Server.

    Controllers never create this model directly. They send individual RFID
    detections to ``nsp.parking.detection.event``; the Edge groups and classifies
    those detections, applies business rules, and creates one immutable final
    transaction. Only this final model is synchronized to Cloud.
    """

    _name = "nsp.parking.transaction"
    _description = "NSP Parking Transaction"
    _order = "event_time desc, id desc"

    transaction_uid = fields.Char(
        string="Transaction UID",
        required=True,
        copy=False,
        index=True,
        help="Edge-generated idempotency key for the final parking transaction.",
    )
    event_time = fields.Datetime(
        string="Event Time",
        required=True,
        index=True,
        help="UTC time of the grouped parking event.",
    )
    event_type = fields.Selection(
        [("check_in", "Check-in"), ("check_out", "Check-out")],
        string="Event Type",
        required=True,
        index=True,
    )
    controller_id = fields.Many2one(
        "nsp.controller", string="Controller", required=True,
        ondelete="restrict", index=True,
    )
    lane_id = fields.Many2one(
        "nsp.parking.lane", string="Lane", required=True,
        ondelete="restrict", index=True,
    )
    parking_area_id = fields.Many2one(
        "nsp.parking.area", string="Parking Area",
        related="lane_id.parking_area_id", readonly=True,
    )
    antenna_id = fields.Many2one(
        "nsp.device.antenna", string="Primary Antenna", required=True,
        ondelete="restrict", index=True,
    )
    device_id = fields.Many2one(
        "nsp.device", string="Reader",
        related="antenna_id.device_id", readonly=True,
    )
    serial_number = fields.Char(
        string="Reader Serial Number",
        related="antenna_id.device_id.serial_number", readonly=True,
    )
    antenna_no = fields.Integer(
        string="Antenna No",
        related="antenna_id.antenna_no", readonly=True,
    )
    primary_detection_id = fields.Many2one(
        "nsp.parking.detection.event",
        string="Primary Detection",
        ondelete="set null",
        copy=False,
        index=True,
    )
    detection_event_ids = fields.One2many(
        "nsp.parking.detection.event",
        "transaction_id",
        string="Source Detections",
        readonly=True,
    )
    detection_count = fields.Integer(
        string="Detection Count", compute="_compute_detection_count"
    )

    status = fields.Selection(
        [("allowed", "Allowed"), ("denied", "Denied")],
        string="Decision", required=True, default="allowed", index=True,
    )
    error_code = fields.Selection([
        ("missing_user_tid", "Missing User RFID Card"),
        ("multiple_vehicle_tid", "Multiple Vehicle RFID Cards"),
        ("vehicle_not_found", "Vehicle Not Found"),
        ("user_not_assigned", "User Card Not Assigned"),
        ("unauthorized_vehicle_user", "Unauthorized Vehicle User"),
        ("check_out_without_check_in", "Check-out Without Previous Check-in"),
        ("continuity_duplicate", "Duplicate Event Type"),
        ("parking_area_not_operational", "Parking Area Not Operational"),
        ("unknown", "Unknown"),
    ], string="Decision Reason", index=True, copy=False)
    error_message = fields.Text(string="Decision Message", copy=False)

    vehicle_id = fields.Many2one(
        "nsp.vehicle", string="Vehicle", ondelete="set null", index=True,
    )
    license_plate = fields.Char(
        string="License Plate", related="vehicle_id.license_plate", readonly=True,
    )
    vehicle_tid = fields.Char(string="Vehicle TID", index=True)
    user_id = fields.Many2one(
        "nsp.user", string="User", ondelete="set null", index=True,
    )
    user_tid = fields.Char(string="User TID", index=True)
    borrow_id = fields.Many2one(
        "nsp.vehicle.borrow", string="Vehicle Borrow",
        ondelete="set null", index=True,
    )

    parking_area_display = fields.Char(
        string="Parking Area", compute="_compute_display_values",
    )
    lane_display = fields.Char(string="Lane", compute="_compute_display_values")
    vehicle_display = fields.Char(string="Vehicle", compute="_compute_display_values")

    _sql_constraints = [
        ("transaction_uid_unique", "unique(transaction_uid)", "Transaction UID must be unique."),
    ]

    @api.depends("detection_event_ids")
    def _compute_detection_count(self):
        for rec in self:
            rec.detection_count = len(rec.detection_event_ids)

    @api.depends(
        "lane_id", "lane_id.display_name", "lane_id.name",
        "lane_id.parking_area_id", "lane_id.parking_area_id.name",
        "vehicle_id", "vehicle_id.display_name", "vehicle_id.license_plate", "vehicle_tid",
    )
    def _compute_display_values(self):
        for rec in self:
            area = rec.lane_id.parking_area_id if rec.lane_id else False
            rec.parking_area_display = (area.name or _("Parking")) if area else "-"
            rec.lane_display = (
                rec.lane_id.display_name or rec.lane_id.name or _("Lane")
            ) if rec.lane_id else "-"
            rec.vehicle_display = (
                rec.vehicle_id.license_plate or rec.vehicle_id.display_name
            ) if rec.vehicle_id else (rec.vehicle_tid or "-")

    @api.model
    def _safe_datetime_value(self, value, default_now=True):
        if not value:
            return fields.Datetime.now() if default_now else False
        text = str(value).strip().replace("T", " ")
        if not text:
            return fields.Datetime.now() if default_now else False
        try:
            parsed = fields.Datetime.to_datetime(text) or datetime.fromisoformat(text)
            if parsed and parsed.tzinfo:
                parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
            elif parsed:
                parsed = parsed.replace(tzinfo=None)
            return fields.Datetime.to_string(parsed) if parsed else (
                fields.Datetime.now() if default_now else False
            )
        except Exception:
            return fields.Datetime.now() if default_now else False

    @api.model
    def _error_catalog(self):
        return {
            "missing_user_tid": ("missing_tag", "critical"),
            "multiple_vehicle_tid": ("ambiguous_tag", "critical"),
            "vehicle_not_found": ("auth", "critical"),
            "user_not_assigned": ("auth", "critical"),
            "unauthorized_vehicle_user": ("borrow", "critical"),
            "check_out_without_check_in": ("continuity", "warning"),
            "continuity_duplicate": ("continuity", "warning"),
            "parking_area_not_operational": ("config", "critical"),
            "unknown": ("unknown", "warning"),
        }

    @api.model
    def _error_code_from_message(self, message):
        text = str(message or "").lower()
        mapping = (
            ("multiple vehicle", "multiple_vehicle_tid"),
            ("vehicle not found", "vehicle_not_found"),
            ("user tid is required", "missing_user_tid"),
            ("missing user", "missing_user_tid"),
            ("user tid is not assigned", "user_not_assigned"),
            ("borrow", "unauthorized_vehicle_user"),
            ("no previous check-in", "check_out_without_check_in"),
            ("already", "continuity_duplicate"),
            ("not operational", "parking_area_not_operational"),
        )
        for marker, code in mapping:
            if marker in text:
                return code
        return "unknown" if text else False

    @api.model
    def _normalize_error_code(self, code, message=False):
        raw = str(code or "").strip().lower().replace("-", "_").replace(" ", "_")
        if raw in self._error_catalog():
            return raw
        return self._error_code_from_message(message)

    @api.model
    def _primary_decision_error(self, error_items):
        catalog = self._error_catalog()
        rank = {"info": 0, "warning": 1, "error": 2, "critical": 3}
        normalized = []
        messages = []
        for raw_code, message in error_items or []:
            code = self._normalize_error_code(raw_code, message)
            if code:
                normalized.append(code)
            if message and str(message) not in messages:
                messages.append(str(message))
        if not normalized:
            return False, False
        primary = max(
            normalized,
            key=lambda code: rank.get(catalog.get(code, ("unknown", "warning"))[1], 0),
        )
        return primary, " ".join(messages) or False

    def _resolve_vehicle_by_tid(self, vehicle_tid):
        tid = str(vehicle_tid or "").strip()
        if not tid:
            return self.env["nsp.vehicle"].browse()
        line = self.env["nsp.vehicle.card"].sudo().search([
            ("card_id.tid", "=", tid),
            ("state", "=", "active"),
        ], limit=1)
        return line.vehicle_id if line else self.env["nsp.vehicle"].browse()

    def _resolve_user_by_tid(self, user_tid):
        tid = str(user_tid or "").strip()
        if not tid:
            return self.env["nsp.user"].browse()
        line = self.env["nsp.user.card"].sudo().search([
            ("card_id.tid", "=", tid),
            ("card_id.card_type", "=", "user_card"),
            ("state", "=", "active"),
        ], limit=1)
        return line.user_id if line else self.env["nsp.user"].browse()

    @api.model
    def _validate_vehicle_borrow_access(self, vehicle, user, event_time):
        Borrow = self.env["nsp.vehicle.borrow"].sudo()
        if not vehicle or not user or (vehicle.owner_id and vehicle.owner_id == user):
            return True, "", Borrow.browse()
        try:
            borrow = Borrow.find_valid_borrow(vehicle, borrower=user, borrow_time=event_time)
        except Exception:
            borrow = Borrow.browse()
        if not borrow:
            return False, _(
                "User is not the vehicle owner and has no active vehicle borrow permission."
            ), Borrow.browse()
        return True, "", borrow

    @api.model
    def _validate_vehicle_continuity(self, vehicle, event_type, event_time):
        if not vehicle or not event_type:
            return True, ""
        domain = [("vehicle_id", "=", vehicle.id), ("status", "=", "allowed")]
        if event_time:
            domain.append(("event_time", "<", event_time))
        last = self.search(domain, order="event_time desc, id desc", limit=1)
        if not last:
            if event_type == "check_out":
                return False, _(
                    "Continuity error: vehicle has no previous Check-in but a Check-out event was received."
                )
            return True, ""
        if last.event_type == event_type:
            label = dict(self._fields["event_type"].selection).get(event_type, event_type)
            return False, _(
                "Continuity error: last valid event for this vehicle is already %s."
            ) % label
        return True, ""

    @api.model
    def _resolve_event_type(self, direction):
        """Map a resolved physical movement direction to the business event."""
        event_type = {"entry": "check_in", "exit": "check_out"}.get(direction)
        if not event_type:
            raise ValidationError(_("unresolved_movement_direction"))
        return event_type

    @api.model
    def _business_values(self, source):
        def value(name):
            if hasattr(source, "_fields"):
                field = source._fields.get(name)
                raw = source[name]
                return raw.id if field and field.type == "many2one" and raw else raw
            return source.get(name)

        event_time = value("event_time")
        if event_time:
            event_time = fields.Datetime.to_string(fields.Datetime.to_datetime(event_time))
        return {
            "controller_id": int(value("controller_id") or 0),
            "lane_id": int(value("lane_id") or 0),
            "antenna_id": int(value("antenna_id") or 0),
            "event_time": event_time or "",
            "event_type": value("event_type") or "",
            "status": value("status") or "",
            "vehicle_id": int(value("vehicle_id") or 0),
            "vehicle_tid": str(value("vehicle_tid") or "").strip(),
            "user_id": int(value("user_id") or 0),
            "user_tid": str(value("user_tid") or "").strip(),
            "borrow_id": int(value("borrow_id") or 0),
            "error_code": value("error_code") or "",
            "error_message": str(value("error_message") or "").strip(),
        }

    @api.model
    def create_idempotent(self, vals):
        uid = str(vals.get("transaction_uid") or "").strip()
        if not uid:
            raise ValidationError(_("missing_transaction_uid"))
        vals = dict(vals, transaction_uid=uid)
        existing = self.search([("transaction_uid", "=", uid)], limit=1)
        if existing:
            if self._business_values(existing) != self._business_values(vals):
                raise ValidationError(_(
                    "transaction_uid_conflict: Transaction UID already exists with different transaction data."
                ))
            return existing, True
        try:
            with self.env.cr.savepoint():
                return self.create(vals), False
        except IntegrityError:
            existing = self.search([("transaction_uid", "=", uid)], limit=1)
            if not existing:
                raise
            if self._business_values(existing) != self._business_values(vals):
                raise ValidationError(_(
                    "transaction_uid_conflict: Transaction UID already exists with different transaction data."
                ))
            return existing, True

    @api.model
    def _best_detection(self, events):
        return events.sorted(key=lambda rec: (rec.detected_at, rec.id))[:1]

    @api.model
    def create_from_detection_group(self, detections, resolved_direction=False):
        """Create one vehicle-centric Parking Transaction.

        Check-in never uses User RFID. Check-out requires exactly one User RFID
        detection selected by the detection processor using nearest timestamp
        pairing and 1:1 consumption.
        """
        detections = detections.exists().filtered(lambda rec: rec.state == "pending")
        if not detections:
            raise ValidationError(_("empty_detection_group"))

        lane = detections[:1].lane_id
        controller = lane.controller_id
        if any(rec.lane_id != lane for rec in detections):
            raise ValidationError(_("mixed_detection_group"))

        direction = resolved_direction or lane.direction
        if direction not in ("entry", "exit"):
            raise ValidationError(_("unresolved_movement_direction"))
        if lane.direction != "both" and direction != lane.direction:
            raise ValidationError(_("movement_direction_not_allowed"))

        event_type = self._resolve_event_type(direction)
        vehicle_events = detections.filtered(
            lambda rec: rec.card_id.card_type == "vehicle_card"
        )
        if not vehicle_events:
            raise ValidationError(_("Vehicle RFID card/TID is required for every Parking Transaction."))

        vehicle_tids = sorted(set(vehicle_events.mapped("card_id.tid")))
        ordered_vehicle = vehicle_events.sorted(key=lambda rec: (rec.detected_at, rec.id))
        vehicle_event = ordered_vehicle[-1]
        event_time = vehicle_event.detected_at
        vehicle_tid = vehicle_event.card_id.tid
        vehicle = self._resolve_vehicle_by_tid(vehicle_tid)

        # Entry is intentionally vehicle-only. Even if User reads exist in the
        # same RF field, they are not part of Check-in business validation.
        user_event = self.env["nsp.parking.detection.event"].browse()
        user_tid = False
        user = self.env["nsp.user"].browse()
        if event_type == "check_out":
            user_events = detections.filtered(
                lambda rec: rec.card_id.card_type == "user_card"
            )
            user_event = user_events[:1]
            if user_event:
                user_tid = user_event.card_id.tid
                user = self._resolve_user_by_tid(user_tid)

        errors = []
        if lane.parking_area_id.state != "operational":
            errors.append(("parking_area_not_operational", _("Parking Area is not operational.")))
        if len(vehicle_tids) > 1:
            errors.append(("multiple_vehicle_tid", _("Multiple vehicle TIDs were detected in one transaction.")))
        if not vehicle:
            errors.append(("vehicle_not_found", _("Vehicle TID is not assigned to an active vehicle.")))

        borrow = self.env["nsp.vehicle.borrow"].browse()
        if event_type == "check_out":
            if not user_event:
                errors.append(("missing_user_tid", _("User RFID card/TID is required for Check-out.")))
            elif not user:
                errors.append(("user_not_assigned", _("User TID is not assigned to an active NSP User.")))
            elif vehicle:
                borrow_ok, borrow_error, borrow = self._validate_vehicle_borrow_access(
                    vehicle, user, event_time
                )
                if not borrow_ok:
                    errors.append(("unauthorized_vehicle_user", borrow_error))

        continuity_ok, continuity_error = self._validate_vehicle_continuity(
            vehicle, event_type, event_time
        )
        if not continuity_ok:
            errors.append((
                self._normalize_error_code(False, continuity_error) or "continuity_duplicate",
                continuity_error,
            ))

        reason_code, reason_message = self._primary_decision_error(errors)
        vals = {
            "transaction_uid": str(uuid4()),
            "event_time": event_time,
            "event_type": event_type,
            "controller_id": controller.id,
            "lane_id": lane.id,
            "antenna_id": vehicle_event.antenna_id.id,
            "primary_detection_id": vehicle_event.id,
            "status": "denied" if errors else "allowed",
            "error_code": reason_code or False,
            "error_message": reason_message or False,
            "vehicle_id": vehicle.id if vehicle else False,
            "vehicle_tid": vehicle_tid or False,
            "user_id": user.id if user else False,
            "user_tid": user_tid or False,
            "borrow_id": borrow.id if borrow else False,
        }
        transaction, _duplicate = self.create_idempotent(vals)
        return transaction
