import logging

from psycopg2 import IntegrityError

from odoo import api, fields, models, _
from odoo.exceptions import AccessError


_logger = logging.getLogger(__name__)


class ParkingTransaction(models.Model):
    _name = "nsp.parking.transaction"
    _description = "NSP Parking Transaction"
    _order = "event_time desc, id desc"

    transaction_uid = fields.Char(required=True, copy=False, index=True)
    event_time = fields.Datetime(required=True, index=True)
    event_type = fields.Selection(
        [("check_in", "Check-in"), ("check_out", "Check-out")],
        required=True,
        index=True,
    )
    controller_id = fields.Many2one("nsp.controller", ondelete="set null", index=True)
    controller_code = fields.Char(required=True, index=True)
    parking_area_id = fields.Many2one("nsp.parking.area", ondelete="set null", index=True)
    parking_area_code = fields.Char(required=True, index=True)
    layout_lane_id = fields.Many2one(
        "nsp.parking.layout.lane", string="Lane Configuration", ondelete="set null", index=True,
        help="Contextual Lane Configuration from the published Parking Layout used for this event.",
    )
    lane_id = fields.Many2one("nsp.parking.lane", ondelete="set null", index=True)
    lane_code = fields.Char(required=True, index=True)
    layout_revision = fields.Integer(default=0, readonly=True, index=True)
    sequence_path = fields.Char(readonly=True)
    observed_duration_seconds = fields.Float(readonly=True)
    allowed_duration_seconds = fields.Float(readonly=True)
    reader_id = fields.Many2one("nsp.device", ondelete="set null", index=True)
    serial_number = fields.Char(required=True, index=True)
    port_no = fields.Integer(required=True, index=True)
    status = fields.Selection(
        [("allowed", "Allowed"), ("denied", "Denied")],
        required=True,
        default="allowed",
        index=True,
    )
    error_code = fields.Selection(
        [
            ("missing_user_tid", "Missing User RFID Tag"),
            ("multiple_user_tags", "Multiple User RFID Tags"),
            ("vehicle_not_found", "Vehicle Not Found"),
            ("user_tag_not_assigned", "User Tag Not Assigned"),
            ("unauthorized_vehicle_user", "Unauthorized Vehicle User"),
            ("check_out_without_check_in", "Check-out Without Previous Check-in"),
            ("vehicle_checked_in_other_area", "Vehicle Checked In at Another Parking Area"),
            ("parking_area_not_operational", "Parking Area Not Operational"),
            ("unknown", "Unknown"),
        ],
        index=True,
        copy=False,
    )
    error_message = fields.Text(copy=False)
    vehicle_id = fields.Many2one("nsp.vehicle", ondelete="set null", index=True)
    vehicle_code = fields.Char(index=True)
    license_plate = fields.Char(index=True)
    vehicle_tid = fields.Char(index=True)
    user_id = fields.Many2one("nsp.user", ondelete="set null", index=True)
    user_code = fields.Char(index=True)
    user_tid = fields.Char(index=True)
    observed_user_codes = fields.Char(readonly=True)
    observed_user_tids = fields.Char(readonly=True)
    borrow_id = fields.Many2one("nsp.vehicle.borrow", ondelete="set null", index=True)
    borrow_code = fields.Char(index=True, readonly=True)
    parking_area_display = fields.Char(compute="_compute_display_values")
    lane_display = fields.Char(compute="_compute_display_values")
    vehicle_display = fields.Char(compute="_compute_display_values")

    _sql_constraints = [
        ("transaction_uid_unique", "unique(transaction_uid)", "Transaction UID must be unique."),
        (
            "transaction_port_range",
            "CHECK(port_no >= 1 AND port_no <= 16)",
            "Reader Port must be between 1 and 16.",
        ),
        (
            "transaction_duration_consistency",
            "CHECK(observed_duration_seconds >= 0 "
            "AND allowed_duration_seconds >= 0 "
            "AND ((allowed_duration_seconds = 0 AND observed_duration_seconds = 0) "
            "OR (allowed_duration_seconds > 0 "
            "AND observed_duration_seconds <= allowed_duration_seconds)))",
            "Observed Duration must be non-negative and cannot exceed Allowed Duration.",
        ),
    ]

    @api.depends(
        "parking_area_id",
        "parking_area_code",
        "lane_id",
        "lane_code",
        "vehicle_id",
        "vehicle_id.license_plate",
        "vehicle_code",
        "license_plate",
        "vehicle_tid",
    )
    def _compute_display_values(self):
        for record in self:
            record.parking_area_display = (
                record.parking_area_id.display_name
                if record.parking_area_id
                else record.parking_area_code or ""
            )
            record.lane_display = (
                record.lane_id.display_name if record.lane_id else record.lane_code or ""
            )
            record.vehicle_display = (
                record.vehicle_id.license_plate
                if record.vehicle_id and record.vehicle_id.license_plate
                else record.license_plate or record.vehicle_code or record.vehicle_tid or ""
            )

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        try:
            records._broadcast_live_monitor()
        except Exception:
            # Transaction persistence must never depend on a browser bus update.
            _logger.exception("Unable to broadcast Cloud Parking Live Monitor event")
        return records

    def _live_monitor_display_meta(self):
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

        if self.event_type == "check_in" and self.error_code not in (
            "vehicle_not_found", "parking_area_not_operational"
        ):
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
                self.error_code or "unknown",
                _("VUI LÒNG LIÊN HỆ BỘ PHẬN VẬN HÀNH"),
            ),
        }

    def _live_monitor_payload(self):
        self.ensure_one()
        area = self.parking_area_id or (
            self.layout_lane_id.parking_area_id if self.layout_lane_id else False
        )
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
            (gate_user.name or _("Unknown employee")).strip()
            if gate_user else _("Unknown employee")
        )
        avatar_url = ""
        if gate_user:
            avatar_field = next(
                (field_name for field_name in ("avatar_128", "image_128", "image_1920")
                 if field_name in gate_user._fields),
                "",
            )
            if avatar_field:
                avatar_url = f"/web/image/nsp.user/{gate_user.id}/{avatar_field}"
        return {
            "id": self.id,
            "transaction_uid": self.transaction_uid,
            "parking_area_id": area.id if area else False,
            "parking_area_name": area.name if area else self.parking_area_code or "",
            "branch_name": area.branch_id.name if area and area.branch_id else "",
            "lane_id": self.lane_id.id if self.lane_id else False,
            "lane_name": self.lane_id.name if self.lane_id else self.lane_code or "",
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
            "user_id": gate_user.id if gate_user else False,
            "avatar_url": avatar_url,
            **self._live_monitor_display_meta(),
        }

    def _broadcast_live_monitor(self):
        bus = self.env["bus.bus"]
        for transaction in self:
            area = transaction.parking_area_id or (
                transaction.layout_lane_id.parking_area_id
                if transaction.layout_lane_id else False
            )
            if not area:
                continue
            bus._sendone(
                "broadcast",
                "nsp_parking_live_transaction",
                transaction._live_monitor_payload(),
            )
        return True

    def write(self, vals):
        if self.env.context.get("nsp_parking_transaction_system_write"):
            return super().write(vals)
        raise AccessError(_(
            "Parking Transactions are immutable Cloud business history. "
            "Create a correcting transaction instead of changing an accepted Edge result."
        ))

    def unlink(self):
        if self.env.context.get("nsp_parking_transaction_system_unlink"):
            return super().unlink()
        raise AccessError(_("Parking Transactions are immutable and cannot be deleted."))

    @api.model
    def _error_catalog(self):
        return dict(self._fields["error_code"].selection)

    @api.model
    def _error_code_from_message(self, message):
        text = str(message or "").lower()
        checks = [
            ("missing user", "missing_user_tid"),
            ("multiple user", "multiple_user_tags"),
            ("ambiguous user", "multiple_user_tags"),
            ("vehicle not found", "vehicle_not_found"),
            ("not assigned", "user_tag_not_assigned"),
            ("unauthorized", "unauthorized_vehicle_user"),
            ("without previous check-in", "check_out_without_check_in"),
            ("other parking area", "vehicle_checked_in_other_area"),
            ("not operational", "parking_area_not_operational"),
        ]
        for marker, code in checks:
            if marker in text:
                return code
        return "unknown" if text else False

    @api.model
    def _normalize_error_code(self, code, message=False):
        value = str(code or "").strip()
        return value if value in self._error_catalog() else self._error_code_from_message(message)

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
            "event_time": event_time or "",
            "event_type": value("event_type") or "",
            "controller_code": str(value("controller_code") or "").strip(),
            "parking_area_code": str(value("parking_area_code") or "").strip(),
            "lane_code": str(value("lane_code") or "").strip(),
            "layout_revision": int(value("layout_revision") or 0),
            "sequence_path": str(value("sequence_path") or "").strip(),
            "observed_duration_seconds": round(float(value("observed_duration_seconds") or 0.0), 6),
            "allowed_duration_seconds": round(float(value("allowed_duration_seconds") or 0.0), 6),
            "serial_number": str(value("serial_number") or "").strip(),
            "port_no": int(value("port_no") or 0),
            "status": value("status") or "",
            "error_code": value("error_code") or "",
            "error_message": str(value("error_message") or "").strip(),
            "vehicle_code": str(value("vehicle_code") or "").strip(),
            "license_plate": str(value("license_plate") or "").strip(),
            "vehicle_tid": str(value("vehicle_tid") or "").strip(),
            "user_code": str(value("user_code") or "").strip(),
            "user_tid": str(value("user_tid") or "").strip(),
            "observed_user_codes": str(value("observed_user_codes") or "").strip(),
            "observed_user_tids": str(value("observed_user_tids") or "").strip(),
            "borrow_code": str(value("borrow_code") or "").strip(),
        }

    @api.model
    def _legacy_business_values(self, source):
        values = self._business_values(source)
        for field_name in (
            "layout_revision", "sequence_path", "observed_duration_seconds",
            "allowed_duration_seconds", "observed_user_codes",
            "observed_user_tids", "borrow_code",
        ):
            values.pop(field_name, None)
        return values

    @api.model
    def _backfill_legacy_snapshots(self, existing, vals):
        """Complete snapshot fields added after an older UID was accepted.

        This preserves idempotency for retries of transactions created before
        Layout Revision, Matched Sequence, and Borrow Code became mandatory.
        """
        if self._legacy_business_values(existing) != self._legacy_business_values(vals):
            return False
        updates = {}
        if not existing.layout_revision and int((vals or {}).get("layout_revision") or 0) > 0:
            updates["layout_revision"] = int(vals["layout_revision"])
        if not existing.sequence_path and str((vals or {}).get("sequence_path") or "").strip():
            updates["sequence_path"] = str(vals["sequence_path"]).strip()
        if not existing.observed_duration_seconds and float((vals or {}).get("observed_duration_seconds") or 0.0) > 0:
            updates["observed_duration_seconds"] = float(vals["observed_duration_seconds"])
        if not existing.allowed_duration_seconds and float((vals or {}).get("allowed_duration_seconds") or 0.0) > 0:
            updates["allowed_duration_seconds"] = float(vals["allowed_duration_seconds"])
        if not existing.observed_user_codes and str((vals or {}).get("observed_user_codes") or "").strip():
            updates["observed_user_codes"] = str(vals["observed_user_codes"]).strip()
        if not existing.observed_user_tids and str((vals or {}).get("observed_user_tids") or "").strip():
            updates["observed_user_tids"] = str(vals["observed_user_tids"]).strip()
        if not existing.borrow_code and str((vals or {}).get("borrow_code") or "").strip():
            updates["borrow_code"] = str(vals["borrow_code"]).strip()
            if (vals or {}).get("borrow_id"):
                updates["borrow_id"] = vals["borrow_id"]
        if updates:
            existing.with_context(nsp_parking_transaction_system_write=True).write(updates)
        return self._business_values(existing) == self._business_values(vals)

    @api.model
    def create_idempotent(self, vals, existing_by_uid=None):
        uid = str((vals or {}).get("transaction_uid") or "").strip()
        if not uid:
            raise ValueError("missing_transaction_uid")
        existing = (
            (existing_by_uid or {}).get(uid)
            if existing_by_uid is not None
            else self.search([("transaction_uid", "=", uid)], limit=1)
        )
        if existing:
            if (
                self._business_values(existing) != self._business_values(vals)
                and not self._backfill_legacy_snapshots(existing, vals)
            ):
                raise ValueError("transaction_uid_conflict")
            return existing, True
        try:
            with self.env.cr.savepoint():
                record = self.create(vals)
            if existing_by_uid is not None:
                existing_by_uid[uid] = record
            return record, False
        except IntegrityError:
            record = self.search([("transaction_uid", "=", uid)], limit=1)
            if record:
                if (
                    self._business_values(record) != self._business_values(vals)
                    and not self._backfill_legacy_snapshots(record, vals)
                ):
                    raise ValueError("transaction_uid_conflict")
                if existing_by_uid is not None:
                    existing_by_uid[uid] = record
                return record, True
            raise
