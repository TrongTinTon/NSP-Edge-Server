# -*- coding: utf-8 -*-
from uuid import uuid4
import logging

from psycopg2 import IntegrityError

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


_logger = logging.getLogger(__name__)


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
    reader_id = fields.Many2one(
        "nsp.device", string="Reader", required=True,
        ondelete="restrict", index=True,
    )
    serial_number = fields.Char(
        string="Reader Serial Number",
        related="reader_id.serial_number", readonly=True,
    )
    port_no = fields.Integer(
        string="Port", required=True, readonly=True, index=True,
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
        ("missing_employee_tid", "Missing Employee RFID Tag"),
        ("vehicle_not_found", "Vehicle Not Found"),
        ("employee_tag_not_assigned", "Employee Tag Not Assigned"),
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
        (
            "parking_transaction_port_range",
            "CHECK(port_no >= 1 AND port_no <= 16)",
            "Reader Port must be between 1 and 16.",
        ),
    ]

    @api.constrains("controller_id", "lane_id", "reader_id", "port_no")
    def _check_reader_port_scope(self):
        for record in self:
            if record.lane_id.controller_id != record.controller_id:
                raise ValidationError(_("Transaction Controller must match the Lane Controller."))
            if record.reader_id.controller_id != record.controller_id:
                raise ValidationError(_("Transaction Reader must belong to the Transaction Controller."))
            allowed = {
                (line.reader_id.id, int(line.port_no or 0))
                for line in record.lane_id.timeline_line_ids
            }
            if (record.reader_id.id, int(record.port_no or 0)) not in allowed:
                raise ValidationError(_("Transaction Reader Port must exist in the Lane Timeline."))

    def init(self):
        self.env.cr.execute(
            """
            CREATE INDEX IF NOT EXISTS nsp_parking_tx_vehicle_continuity_idx
                ON nsp_parking_transaction (vehicle_id, event_time DESC, id DESC)
             WHERE status = 'allowed' AND vehicle_id IS NOT NULL
            """
        )

    @api.depends("detection_event_ids")
    def _compute_detection_count(self):
        counts = self.env["nsp.parking.detection.event"].sudo()._read_group(
            [("transaction_id", "in", self.ids)],
            ["transaction_id"],
            ["__count"],
        ) if self.ids else []
        by_transaction = {transaction.id: count for transaction, count in counts}
        for rec in self:
            rec.detection_count = by_transaction.get(rec.id, 0)

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
    def _error_catalog(self):
        return {
            "missing_employee_tid": ("missing_tag", "critical"),
            "vehicle_not_found": ("auth", "critical"),
            "employee_tag_not_assigned": ("auth", "critical"),
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
            ("vehicle not found", "vehicle_not_found"),
            ("user tid is required", "missing_employee_tid"),
            ("missing user", "missing_employee_tid"),
            ("user tid is not assigned", "employee_tag_not_assigned"),
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
            "reader_id": int(value("reader_id") or 0),
            "port_no": int(value("port_no") or 0),
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

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        try:
            records._broadcast_live_monitor()
        except Exception:
            # A display failure must never roll back a final Parking Transaction.
            _logger.exception("Unable to broadcast NSP Parking Live Monitor event")
        return records

    def _live_monitor_display_meta(self):
        """Return the gate-facing decision for the final transaction.

        Check-out is the security-critical decision: every denied Check-out is
        displayed as a stop alert. Benign duplicate Check-in reads remain hidden
        from drivers while staying auditable in Parking Transactions.
        """
        self.ensure_one()
        if self.status == "allowed":
            if self.event_type == "check_out":
                return {
                    "display_kind": "entry",
                    "display_title": _("ĐƯỢC PHÉP LẤY XE"),
                    "display_reason": "",
                }
            return {
                "display_kind": "entry",
                "display_title": _("MỜI VÀO"),
                "display_reason": "",
            }

        code = self.error_code or "unknown"
        _category, severity = self._error_catalog().get(code, ("unknown", "warning"))
        if self.event_type == "check_in" and severity != "critical":
            return {"display_kind": "ignore", "display_title": "", "display_reason": ""}

        reasons = {
            "vehicle_not_found": _("THẺ XE CHƯA ĐƯỢC CẤP"),
            "parking_area_not_operational": _("BÃI XE TẠM NGƯNG VẬN HÀNH"),
            "missing_employee_tid": _("THIẾU THẺ NHÂN VIÊN"),
            "employee_tag_not_assigned": _("THẺ NHÂN VIÊN CHƯA ĐƯỢC CẤP"),
            "unauthorized_vehicle_user": _("NGƯỜI QUÉT KHÔNG ĐƯỢC PHÉP LẤY XE"),
            "check_out_without_check_in": _("XE KHÔNG CÓ TRẠNG THÁI ĐANG TRONG BÃI"),
            "continuity_duplicate": _("TRẠNG THÁI XE KHÔNG HỢP LỆ"),
        }
        return {
            "display_kind": "alert",
            "display_title": _("KHÔNG ĐƯỢC PHÉP LẤY XE")
            if self.event_type == "check_out" else _("VUI LÒNG DỪNG LẠI"),
            "display_reason": reasons.get(
                code,
                _("VUI LÒNG LIÊN HỆ NHÂN VIÊN BẢO VỆ"),
            ),
        }

    def _live_monitor_payload(self):
        """Serialize one final transaction for the customer-facing monitor."""
        self.ensure_one()
        area = self.parking_area_id
        vehicle = self.vehicle_id
        owner = vehicle.owner_id if vehicle else self.env["nsp.user"].browse()
        gate_user = self.user_id if self.event_type == "check_out" and self.user_id else owner
        vehicle_type = vehicle.vehicle_type_id if vehicle else self.env["nsp.vehicle.type"].browse()
        vehicle_type_code = str(vehicle_type.code or "").strip().lower() if vehicle_type else ""
        license_plate = (self.license_plate or self.vehicle_tid or "-").strip().upper()
        employee_name = (
            (gate_user.name or _("Unknown employee")).strip().upper()
            if gate_user else _("Unknown employee").upper()
        )
        return {
            "id": self.id,
            "transaction_uid": self.transaction_uid,
            "parking_area_id": area.id if area else False,
            "parking_area_name": area.name if area else "",
            "branch_name": area.branch_id.name if area and area.branch_id else "",
            "lane_id": self.lane_id.id if self.lane_id else False,
            "lane_name": self.lane_id.name if self.lane_id else "",
            "event_type": self.event_type,
            "event_time": fields.Datetime.to_string(self.event_time) if self.event_time else "",
            "status": self.status,
            "is_valid": self.status == "allowed",
            "error_code": self.error_code or "",
            "message": self.error_message or "",
            "vehicle_id": vehicle.id if vehicle else False,
            "vehicle_key": str(vehicle.id if vehicle else (self.vehicle_tid or self.transaction_uid)),
            "vehicle_type": vehicle_type_code or "other",
            "license_plate": license_plate,
            "employee_name": employee_name,
            **self._live_monitor_display_meta(),
        }

    def _broadcast_live_monitor(self):
        """Broadcast final transactions to the Parking Live Monitor."""
        bus = self.env["bus.bus"]
        for transaction in self:
            if not transaction.parking_area_id:
                continue
            bus._sendone(
                "broadcast",
                "nsp_parking_live_transaction",
                transaction._live_monitor_payload(),
            )
        return True

    @api.model
    def create_idempotent(self, vals, existing_by_uid=None):
        """Create once by transaction_uid, optionally reusing a batch-prefetched map."""
        uid = str(vals.get("transaction_uid") or "").strip()
        if not uid:
            raise ValidationError(_("missing_transaction_uid"))
        vals = dict(vals, transaction_uid=uid)
        existing = (
            existing_by_uid.get(uid, self.browse())
            if existing_by_uid is not None
            else self.search([("transaction_uid", "=", uid)], limit=1)
        )
        if existing:
            if self._business_values(existing) != self._business_values(vals):
                raise ValidationError(_(
                    "transaction_uid_conflict: Transaction UID already exists with different transaction data."
                ))
            return existing, True
        try:
            with self.env.cr.savepoint():
                created = self.create(vals)
            if existing_by_uid is not None:
                existing_by_uid[uid] = created
            return created, False
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
    def create_from_detection_group(self, detections, resolved_event_type=False):
        """Create one vehicle-centric Parking Transaction.

        Event Type is resolved directly by the configured directed Reader Port transition.
        Check-in never uses User RFID. Check-out requires exactly one User RFID
        detection selected by the detection processor inside the configured event-sequence Duration.
        """
        detections = detections.filtered(lambda rec: rec.state == "pending")
        if not detections:
            raise ValidationError(_("empty_detection_group"))

        lane = detections[:1].lane_id
        controller = lane.controller_id
        if any(rec.lane_id != lane for rec in detections):
            raise ValidationError(_("mixed_detection_group"))

        event_type = str(resolved_event_type or "").strip().lower()
        if event_type not in ("check_in", "check_out"):
            raise ValidationError(_("unresolved_parking_event_type"))
        vehicle_events = detections.filtered(
            lambda rec: bool(rec.vehicle_id)
        )
        if not vehicle_events:
            raise ValidationError(_("Vehicle RFID Tag/TID is required for every Parking Transaction."))

        vehicle_records = vehicle_events.mapped("vehicle_id")
        if len(vehicle_records) != 1:
            raise ValidationError(_("mixed_vehicle_detection_group"))
        ordered_vehicle = vehicle_events.sorted(key=lambda rec: (rec.detected_at, rec.id))
        vehicle_event = ordered_vehicle[-1]
        event_time = vehicle_event.detected_at
        vehicle_tid = vehicle_event.tid
        vehicle = vehicle_event.vehicle_id

        # Entry is intentionally vehicle-only. Even if User reads exist in the
        # same RF field, they are not part of Check-in business validation.
        user_event = self.env["nsp.parking.detection.event"].browse()
        user_tid = False
        user = self.env["nsp.user"].browse()
        if event_type == "check_out":
            user_events = detections.filtered(
                lambda rec: bool(rec.user_id)
            )
            user_event = user_events[:1]
            if user_event:
                user_tid = user_event.tid
                user = user_event.user_id

        errors = []
        if lane.parking_area_id.state != "operational":
            errors.append(("parking_area_not_operational", _("Parking Area is not operational.")))
        if not vehicle:
            errors.append(("vehicle_not_found", _("Vehicle TID is not assigned to an active vehicle.")))

        borrow = self.env["nsp.vehicle.borrow"].browse()
        if event_type == "check_out":
            if not user_event:
                errors.append(("missing_employee_tid", _("Employee RFID Tag/TID is required for Check-out.")))
            elif not user:
                errors.append(("employee_tag_not_assigned", _("User TID is not assigned to an active NSP User.")))
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
            "reader_id": vehicle_event.reader_id.id,
            "port_no": int(vehicle_event.port_no or 0),
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
