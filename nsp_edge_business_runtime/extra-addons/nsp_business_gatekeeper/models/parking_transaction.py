# -*- coding: utf-8 -*-
from uuid import uuid4
import logging

from psycopg2 import IntegrityError

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, ValidationError


_logger = logging.getLogger(__name__)


_DURATION_EPSILON_SECONDS = 0.001


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
    controller_code = fields.Char(string="Controller Code", readonly=True, index=True)
    lane_id = fields.Many2one(
        "nsp.parking.lane", string="Lane", required=True,
        ondelete="restrict", index=True,
    )
    lane_code = fields.Char(string="Lane Code", readonly=True, index=True)
    parking_area_id = fields.Many2one(
        "nsp.parking.area", string="Parking Area",
        ondelete="restrict", readonly=True, index=True,
        help="Parking Area snapshot reference captured when the transaction is created.",
    )
    parking_area_code = fields.Char(string="Parking Area Code", readonly=True, index=True)
    layout_revision = fields.Integer(
        string="Parking Layout Revision", default=0, readonly=True, index=True,
    )
    sequence_path = fields.Char(
        string="Matched Sequence", readonly=True,
        help="Immutable Reader Port sequence used to classify the movement.",
    )
    observed_duration_seconds = fields.Float(
        string="Observed Duration (s)", readonly=True,
        help="Elapsed time between the first and last Vehicle detections in the matched sequence.",
    )
    allowed_duration_seconds = fields.Float(
        string="Allowed Duration (s)", readonly=True,
        help="Maximum calibrated duration, including Lane timing tolerance, used for the decision.",
    )
    reader_id = fields.Many2one(
        "nsp.device", string="Reader", required=True,
        ondelete="restrict", index=True,
    )
    serial_number = fields.Char(
        string="Reader Serial Number", readonly=True, index=True,
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
        ("missing_user_tid", "Missing User RFID Tag"),
        ("multiple_user_tags", "Multiple User RFID Tags"),
        ("vehicle_not_found", "Vehicle Not Found"),
        ("user_tag_not_assigned", "User Tag Not Assigned"),
        ("unauthorized_vehicle_user", "Unauthorized Vehicle User"),
        ("check_out_without_check_in", "Check-out Without Previous Check-in"),
        ("vehicle_checked_in_other_area", "Vehicle Checked In at Another Parking Area"),
        ("parking_area_not_operational", "Parking Area Not Operational"),
        ("unknown", "Unknown"),
    ], string="Decision Reason", index=True, copy=False)
    error_message = fields.Text(string="Decision Message", copy=False)

    vehicle_id = fields.Many2one(
        "nsp.vehicle", string="Vehicle", ondelete="set null", index=True,
    )
    vehicle_code = fields.Char(string="Vehicle Code", readonly=True, index=True)
    license_plate = fields.Char(string="License Plate", readonly=True, index=True)
    vehicle_tid = fields.Char(string="Vehicle TID", readonly=True, index=True)
    user_id = fields.Many2one(
        "nsp.user", string="User", ondelete="set null", index=True,
    )
    user_code = fields.Char(string="User Code", readonly=True, index=True)
    user_tid = fields.Char(string="User TID", readonly=True, index=True)
    observed_user_codes = fields.Char(
        string="Observed User Codes", readonly=True,
        help="Sorted User Codes detected in the Check-out authorization window.",
    )
    observed_user_tids = fields.Char(
        string="Observed User TIDs", readonly=True,
        help="Sorted User RFID TIDs detected in the Check-out authorization window.",
    )
    borrow_id = fields.Many2one(
        "nsp.vehicle.borrow", string="Vehicle Borrow",
        ondelete="set null", index=True,
    )
    borrow_code = fields.Char(string="Borrow Code", readonly=True, index=True)

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
        (
            "parking_transaction_duration_consistency",
            "CHECK(observed_duration_seconds >= 0 "
            "AND allowed_duration_seconds >= 0 "
            "AND ((allowed_duration_seconds = 0 AND observed_duration_seconds = 0) "
            "OR (allowed_duration_seconds > 0 "
            "AND observed_duration_seconds <= allowed_duration_seconds)))",
            "Observed Duration must be non-negative and cannot exceed Allowed Duration.",
        ),
    ]

    @api.constrains("controller_id", "parking_area_id", "lane_id", "reader_id", "port_no")
    def _check_reader_port_scope(self):
        for record in self:
            if record.parking_area_id and record.lane_id.parking_area_id != record.parking_area_id:
                raise ValidationError(_("Transaction Parking Area must match the Lane Parking Area."))
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
        "parking_area_id", "parking_area_id.name", "parking_area_code",
        "lane_id", "lane_id.display_name", "lane_id.name", "lane_code",
        "vehicle_id", "vehicle_id.display_name", "vehicle_id.license_plate",
        "license_plate", "vehicle_code", "vehicle_tid",
    )
    def _compute_display_values(self):
        for rec in self:
            area = rec.parking_area_id or (rec.lane_id.parking_area_id if rec.lane_id else False)
            rec.parking_area_display = (
                area.name if area and area.name else rec.parking_area_code or "-"
            )
            rec.lane_display = (
                rec.lane_id.display_name or rec.lane_id.name or rec.lane_code or _("Lane")
            ) if rec.lane_id else rec.lane_code or "-"
            rec.vehicle_display = (
                rec.license_plate
                or (rec.vehicle_id.license_plate if rec.vehicle_id else "")
                or rec.vehicle_code
                or rec.vehicle_tid
                or "-"
            )

    @api.model
    def _error_catalog(self):
        return {
            "missing_user_tid": ("missing_tag", "critical"),
            "multiple_user_tags": ("ambiguous_identity", "critical"),
            "vehicle_not_found": ("auth", "critical"),
            "user_tag_not_assigned": ("auth", "critical"),
            "unauthorized_vehicle_user": ("borrow", "critical"),
            "check_out_without_check_in": ("continuity", "warning"),
            "vehicle_checked_in_other_area": ("continuity", "critical"),
            "parking_area_not_operational": ("config", "critical"),
            "unknown": ("unknown", "warning"),
        }

    @api.model
    def _error_code_from_message(self, message):
        text = str(message or "").lower()
        mapping = (
            ("vehicle not found", "vehicle_not_found"),
            ("user tid is required", "missing_user_tid"),
            ("missing user", "missing_user_tid"),
            ("multiple user", "multiple_user_tags"),
            ("ambiguous user", "multiple_user_tags"),
            ("user tid is not assigned", "user_tag_not_assigned"),
            ("borrow", "unauthorized_vehicle_user"),
            ("no previous check-in", "check_out_without_check_in"),
            ("other parking area", "vehicle_checked_in_other_area"),
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
            _logger.exception(
                "Failed to validate Vehicle Borrow access",
                extra={
                    "vehicle_id": vehicle.id if vehicle else False,
                    "user_id": user.id if user else False,
                },
            )
            borrow = Borrow.browse()
        if not borrow:
            return False, _(
                "User is not the vehicle owner and has no active vehicle borrow permission."
            ), Borrow.browse()
        return True, "", borrow

    @api.model
    def _acquire_vehicle_continuity_lock(self, vehicle):
        """Serialize movement decisions for one Vehicle across all Parking Lanes."""
        vehicle = vehicle.exists()
        if not vehicle:
            return False
        self.env.cr.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            (f"nsp.parking:vehicle:{vehicle.id}",),
        )
        return True

    @api.model
    def _event_type_from_vehicle_state(self, vehicle, event_time=False):
        """Resolve Check-in/Check-out from the Vehicle parking state.

        A matched Antenna Sequence only proves that the Vehicle crossed a Lane.
        If the latest allowed movement leaves the Vehicle inside Parking
        (Check-in), this crossing is Check-out; otherwise it is Check-in.
        """
        if not vehicle:
            return "check_in"
        domain = [("vehicle_id", "=", vehicle.id), ("status", "=", "allowed")]
        if event_time:
            domain.append(("event_time", "<=", event_time))
        previous = self.search(domain, order="event_time desc, id desc", limit=1)
        return "check_out" if previous and previous.event_type == "check_in" else "check_in"

    @api.model
    def _vehicle_continuity_decision(
        self, vehicle, event_type, event_time, parking_area=False
    ):
        """Validate the alternating Vehicle movement sequence.

        Only allowed transactions establish Vehicle presence. The first valid
        business movement must be Check-in, then allowed movements must alternate
        strictly between Check-out and Check-in. Invalid or repeated physical
        movements are acquisition noise and do not create Parking Transactions.
        """
        if not vehicle or not event_type:
            return "allow", False, ""

        domain = [("vehicle_id", "=", vehicle.id), ("status", "=", "allowed")]
        if event_time:
            domain.append(("event_time", "<=", event_time))
        previous = self.search(domain, order="event_time desc, id desc", limit=1)

        if not previous:
            if event_type == "check_out":
                return (
                    "ignore",
                    "check_out_without_check_in",
                    _("Check-out ignored because the Vehicle has no previous allowed Check-in."),
                )
            return "allow", False, ""

        if previous.event_type == event_type:
            return (
                "ignore",
                "repeated_movement",
                _("Repeated parking movement ignored; Check-in and Check-out must alternate."),
            )

        if event_type == "check_out" and parking_area:
            previous_area = previous.parking_area_id or (
                previous.lane_id.parking_area_id if previous.lane_id else False
            )
            if previous_area and previous_area != parking_area:
                return (
                    "deny",
                    "vehicle_checked_in_other_area",
                    _(
                        "Continuity error: vehicle is checked in at another Parking Area (%s)."
                    ) % (previous_area.display_name or previous.parking_area_code or "-"),
                )

        return "allow", False, ""


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
            "controller_code": str(value("controller_code") or "").strip(),
            "lane_code": str(value("lane_code") or "").strip(),
            "parking_area_code": str(value("parking_area_code") or "").strip(),
            "layout_revision": int(value("layout_revision") or 0),
            "sequence_path": str(value("sequence_path") or "").strip(),
            "observed_duration_seconds": round(float(value("observed_duration_seconds") or 0.0), 6),
            "allowed_duration_seconds": round(float(value("allowed_duration_seconds") or 0.0), 6),
            "serial_number": str(value("serial_number") or "").strip(),
            "port_no": int(value("port_no") or 0),
            "event_time": event_time or "",
            "event_type": value("event_type") or "",
            "status": value("status") or "",
            "vehicle_code": str(value("vehicle_code") or "").strip(),
            "license_plate": str(value("license_plate") or "").strip(),
            "vehicle_tid": str(value("vehicle_tid") or "").strip(),
            "user_code": str(value("user_code") or "").strip(),
            "user_tid": str(value("user_tid") or "").strip(),
            "observed_user_codes": str(value("observed_user_codes") or "").strip(),
            "observed_user_tids": str(value("observed_user_tids") or "").strip(),
            "borrow_code": str(value("borrow_code") or "").strip(),
            "error_code": value("error_code") or "",
            "error_message": str(value("error_message") or "").strip(),
        }

    @api.model_create_multi
    def create(self, vals_list):
        lane_ids = {int(vals.get("lane_id") or 0) for vals in vals_list}
        lane_ids.discard(0)
        # Respect the caller environment. Internal ingestion already enters
        # this create path with its authenticated runtime scope.
        lanes = self.env["nsp.parking.lane"].browse(list(lane_ids)).exists()
        if lanes:
            lanes.check_access("read")
        lane_by_id = {lane.id: lane for lane in lanes}
        prepared = []
        for source in vals_list:
            vals = dict(source)
            lane = lane_by_id.get(int(vals.get("lane_id") or 0))
            if lane:
                vals.setdefault("parking_area_id", lane.parking_area_id.id)
                vals.setdefault("parking_area_code", lane.parking_area_id.code or "")
                vals.setdefault("lane_code", lane.code or "")
                vals.setdefault("controller_code", lane.controller_id.controller_id or "")
            prepared.append(vals)
        records = super().create(prepared)
        try:
            records._broadcast_live_monitor()
        except Exception:
            # A display failure must never roll back a final Parking Transaction.
            _logger.exception("Unable to broadcast NSP Parking Live Monitor event")
        return records

    def write(self, vals):
        if self.env.context.get("nsp_parking_transaction_system_write"):
            return super().write(vals)
        raise AccessError(_(
            "Parking Transactions are immutable Edge business history. "
            "Create a correcting transaction instead of changing a final decision."
        ))

    def unlink(self):
        if self.env.context.get("nsp_parking_transaction_system_unlink"):
            return super().unlink()
        raise AccessError(_("Parking Transactions are immutable and cannot be deleted."))

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
            "missing_user_tid": _("THIẾU THẺ NGƯỜI DÙNG"),
            "multiple_user_tags": _("PHÁT HIỆN NHIỀU THẺ NGƯỜI DÙNG"),
            "user_tag_not_assigned": _("THẺ NGƯỜI DÙNG CHƯA ĐƯỢC GÁN"),
            "unauthorized_vehicle_user": _("NGƯỜI QUÉT KHÔNG ĐƯỢC PHÉP LẤY XE"),
            "check_out_without_check_in": _("XE KHÔNG CÓ TRẠNG THÁI ĐANG TRONG BÃI"),
            "vehicle_checked_in_other_area": _("XE ĐANG Ở MỘT BÃI XE KHÁC"),
        }
        return {
            "display_kind": "alert",
            "display_title": _("KHÔNG ĐƯỢC PHÉP LẤY XE")
            if self.event_type == "check_out" else _("VUI LÒNG DỪNG LẠI"),
            "display_reason": reasons.get(
                code,
                _("VUI LÒNG LIÊN HỆ BỘ PHẬN VẬN HÀNH"),
            ),
        }

    def _live_monitor_payload(self):
        """Serialize one final transaction for the customer-facing monitor."""
        self.ensure_one()
        area = self.parking_area_id or (self.lane_id.parking_area_id if self.lane_id else False)
        vehicle = self.vehicle_id
        owner = vehicle.owner_id if vehicle else self.env["nsp.user"].browse()
        gate_user = self.user_id if self.event_type == "check_out" and self.user_id else owner
        vehicle_type = vehicle.vehicle_type_id if vehicle else self.env["nsp.vehicle.type"].browse()
        vehicle_type_code = str(vehicle_type.code or "").strip().lower() if vehicle_type else ""
        license_plate = (
            self.license_plate
            or (vehicle.license_plate if vehicle else "")
            or self.vehicle_tid
            or "-"
        ).strip().upper()
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
    def _sequence_path_for_lane(self, lane, event_type=False):
        rows = lane.antenna_sequence_ids.sorted("sequence")
        return ">".join(
            "%s:%s" % (
                row.reader_id.device_code or row.reader_id.serial_number or row.reader_id.id,
                int(row.port_no or 0),
            )
            for row in rows
        )

    @api.model
    def _allowed_duration_for_lane(self, lane, event_type=False):
        rows = lane.antenna_sequence_ids.sorted("sequence")
        return sum(
            lane.allowed_duration_for_step(row.sequence) for row in rows[1:]
        )

    @api.model
    def create_from_detection_group(
        self,
        detections,
        resolved_event_type=False,
        observed_duration_seconds=False,
        allowed_duration_seconds=False,
    ):
        """Create one vehicle-centric Parking Transaction.

        Event Type is resolved from current Vehicle parking state after the Lane
        Antenna Sequence has matched. Check-in never uses User RFID. Check-out
        requires exactly one User RFID
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
        if observed_duration_seconds is False:
            observed_duration_seconds = max(
                0.0,
                (ordered_vehicle[-1].detected_at - ordered_vehicle[0].detected_at).total_seconds(),
            )
        if allowed_duration_seconds is False:
            allowed_duration_seconds = self._allowed_duration_for_lane(lane, event_type)
        observed_duration_seconds = max(0.0, float(observed_duration_seconds or 0.0))
        allowed_duration_seconds = max(0.0, float(allowed_duration_seconds or 0.0))
        if allowed_duration_seconds <= 0:
            raise ValidationError(_("parking_sequence_allowed_duration_missing"))
        if observed_duration_seconds > allowed_duration_seconds + _DURATION_EPSILON_SECONDS:
            raise ValidationError(_("parking_sequence_duration_exceeds_configured_window"))
        # Datetime arithmetic and JSON/DB float conversion can differ by a few
        # microseconds. Clamp only that insignificant drift; never hide a real
        # sequence timeout.
        if observed_duration_seconds > allowed_duration_seconds:
            observed_duration_seconds = allowed_duration_seconds
        observed_duration_seconds = round(observed_duration_seconds, 6)
        allowed_duration_seconds = round(allowed_duration_seconds, 6)
        vehicle_tid = vehicle_event.tid
        vehicle = vehicle_event.vehicle_id
        layout_revision = int(lane.parking_area_id.published_revision or 0)
        detection_revisions = set(detections.mapped("layout_revision"))
        if layout_revision <= 0 or detection_revisions != {layout_revision}:
            raise ValidationError(_("parking_layout_revision_mismatch"))
        if vehicle:
            # Continuity is Vehicle-wide, not Lane-wide. Serialize decisions for
            # the same Vehicle even when different physical Lanes process concurrently.
            self._acquire_vehicle_continuity_lock(vehicle)

        # Entry is intentionally vehicle-only. Even if User reads exist in the
        # same RF field, they are not part of Check-in business validation.
        user_events = self.env["nsp.parking.detection.event"].browse()
        user_event = self.env["nsp.parking.detection.event"].browse()
        user_tid = False
        user = self.env["nsp.user"].browse()
        multiple_user_tags = False
        if event_type == "check_out":
            user_events = detections.filtered(lambda rec: bool(rec.user_id))
            unique_users = user_events.mapped("user_id")
            unique_tids = {event.tid for event in user_events if event.tid}
            multiple_user_tags = len(unique_users) > 1 or len(unique_tids) > 1
            if not multiple_user_tags and unique_users:
                user = unique_users[:1]
                user_event = user_events.sorted(
                    key=lambda rec: (rec.detected_at, rec.id)
                )[-1:]
                user_tid = user_event.tid

        errors = []
        if lane.parking_area_id.state != "operational":
            errors.append(("parking_area_not_operational", _("Parking Area is not operational.")))
        if not vehicle:
            errors.append(("vehicle_not_found", _("Vehicle TID is not assigned to an active vehicle.")))

        borrow = self.env["nsp.vehicle.borrow"].browse()
        if event_type == "check_out":
            if multiple_user_tags:
                errors.append((
                    "multiple_user_tags",
                    _("Check-out detected more than one User RFID identity in the configured sequence window."),
                ))
            elif not user_event:
                errors.append(("missing_user_tid", _("User RFID Tag/TID is required for Check-out.")))
            elif not user:
                errors.append(("user_tag_not_assigned", _("User TID is not assigned to an active NSP User.")))
            elif vehicle:
                borrow_ok, borrow_error, borrow = self._validate_vehicle_borrow_access(
                    vehicle, user, event_time
                )
                if not borrow_ok:
                    errors.append(("unauthorized_vehicle_user", borrow_error))

        continuity_action, continuity_code, continuity_message = (
            self._vehicle_continuity_decision(
                vehicle, event_type, event_time, lane.parking_area_id
            )
        )
        if continuity_action == "ignore":
            _logger.info(
                "Parking movement ignored by sequence state: vehicle=%s event_type=%s "
                "lane=%s revision=%s reason=%s message=%s",
                vehicle.id if vehicle else False,
                event_type,
                lane.id,
                layout_revision,
                continuity_code or "invalid_sequence",
                continuity_message or "",
            )
            return self.browse()
        if continuity_action == "deny":
            errors.append((continuity_code or "unknown", continuity_message))

        observed_user_codes = ",".join(sorted({
            str(event.user_id.user_code or "").strip().upper()
            for event in user_events
            if event.user_id and event.user_id.user_code
        }))
        observed_user_tids = ",".join(sorted({
            str(event.tid or "").strip()
            for event in user_events
            if event.tid
        }))

        reason_code, reason_message = self._primary_decision_error(errors)
        vals = {
            "transaction_uid": str(uuid4()),
            "event_time": event_time,
            "event_type": event_type,
            "controller_id": controller.id,
            "controller_code": controller.controller_id or "",
            "parking_area_id": lane.parking_area_id.id,
            "lane_id": lane.id,
            "lane_code": lane.code or "",
            "parking_area_code": lane.parking_area_id.code or "",
            "layout_revision": layout_revision,
            "sequence_path": self._sequence_path_for_lane(lane, event_type),
            "observed_duration_seconds": observed_duration_seconds,
            "allowed_duration_seconds": allowed_duration_seconds,
            "reader_id": vehicle_event.reader_id.id,
            "serial_number": vehicle_event.reader_id.serial_number or "",
            "port_no": int(vehicle_event.port_no or 0),
            "primary_detection_id": vehicle_event.id,
            "status": "denied" if errors else "allowed",
            "error_code": reason_code or False,
            "error_message": reason_message or False,
            "vehicle_id": vehicle.id if vehicle else False,
            "vehicle_code": vehicle.vehicle_code if vehicle else False,
            "license_plate": vehicle.license_plate if vehicle else False,
            "vehicle_tid": vehicle_tid or False,
            "user_id": user.id if user else False,
            "user_code": user.user_code if user else False,
            "user_tid": user_tid or False,
            "observed_user_codes": observed_user_codes or False,
            "observed_user_tids": observed_user_tids or False,
            "borrow_id": borrow.id if borrow else False,
            "borrow_code": borrow.borrow_code if borrow else False,
        }
        transaction, _duplicate = self.create_idempotent(vals)
        return transaction
