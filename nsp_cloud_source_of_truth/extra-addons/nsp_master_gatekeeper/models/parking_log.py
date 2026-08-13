# -*- coding: utf-8 -*-
import logging

from psycopg2 import IntegrityError

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, ValidationError


_logger = logging.getLogger(__name__)


class ParkingLog(models.Model):
    """Immutable Cloud mirror of the final Parking business event produced at Edge.

    Cloud stores only stable business evidence. Reader/Controller diagnostics and
    Antenna timing belong to Edge Detection/Configuration data and are deliberately
    not duplicated into long-lived Parking Logs.
    """

    _name = "nsp.parking.log"
    _description = "NSP Parking Log"
    _rec_name = "log_uid"
    _order = "event_time desc, id desc"
    _log_access = False

    log_uid = fields.Char(string="Log UID", required=True, copy=False)
    event_time = fields.Datetime(string="Event Time", required=True, index=True)
    event_type = fields.Selection(
        [("check_in", "Check-in"), ("check_out", "Check-out")],
        string="Event Type", required=True,
    )
    decision = fields.Selection(
        [("allowed", "Allowed"), ("denied", "Denied")],
        string="Decision", required=True,
    )
    reason_code = fields.Selection([
        ("missing_user_tid", "Missing User RFID Tag"),
        ("multiple_user_tags", "Multiple User RFID Tags"),
        ("user_tag_not_assigned", "User Tag Not Assigned (Legacy)"),
        ("unauthorized_vehicle_user", "Unauthorized Vehicle User"),
        ("vehicle_checked_in_other_area", "Vehicle Checked In at Another Parking Area"),
        ("parking_area_not_operational", "Parking Area Not Operational"),
        ("vehicle_not_found", "Vehicle Not Found (Legacy)"),
        ("check_out_without_check_in", "Check-out Without Check-in (Legacy)"),
        ("unknown", "Unknown"),
    ], string="Decision Reason", copy=False)

    parking_area_id = fields.Many2one(
        "nsp.parking.area", string="Parking Area", ondelete="restrict", readonly=True,
    )
    layout_lane_id = fields.Many2one(
        "nsp.parking.layout.lane", string="Legacy Lane Configuration",
        ondelete="set null", readonly=True,
        help=(
            "Legacy compatibility reference only. New Parking Logs use the stable "
            "Parking Area + Lane + Layout Revision historical context and do not "
            "persist a mutable Lane Configuration reference."
        ),
    )
    lane_id = fields.Many2one(
        "nsp.parking.lane", string="Lane", ondelete="restrict", readonly=True, index=True,
    )
    layout_revision = fields.Integer(
        string="Parking Layout Revision", default=0, readonly=True,
    )

    vehicle_id = fields.Many2one(
        "nsp.vehicle", string="Vehicle", ondelete="set null", readonly=True, index=True,
    )
    vehicle_tid = fields.Char(
        string="Vehicle TID", readonly=True,
        help="RFID evidence captured at movement time; assignments can change later.",
    )
    user_id = fields.Many2one(
        "nsp.user", string="User", ondelete="set null", readonly=True, index=True,
    )
    user_tid = fields.Char(
        string="User TID", readonly=True,
        help="Check-out RFID evidence captured at movement time.",
    )
    borrow_id = fields.Many2one(
        "nsp.vehicle.borrow", string="Vehicle Borrow", ondelete="set null", readonly=True,
    )

    vehicle_display = fields.Char(string="Vehicle", compute="_compute_vehicle_display")

    _sql_constraints = [
        ("log_uid_unique", "unique(log_uid)", "Parking Log UID must be unique."),
    ]

    def init(self):
        # Purpose-built indexes only. Avoid a standalone index for every low-cardinality
        # field because each extra index increases write/WAL cost for immutable history.
        self.env.cr.execute("""
            CREATE INDEX IF NOT EXISTS nsp_parking_log_area_event_idx
                ON nsp_parking_log (parking_area_id, event_time DESC, id DESC)
             WHERE parking_area_id IS NOT NULL
        """)
        self.env.cr.execute("""
            CREATE INDEX IF NOT EXISTS nsp_parking_log_vehicle_event_idx
                ON nsp_parking_log (vehicle_id, event_time DESC, id DESC)
             WHERE vehicle_id IS NOT NULL
        """)

    @api.depends("vehicle_id", "vehicle_id.license_plate", "vehicle_tid")
    def _compute_vehicle_display(self):
        for record in self:
            record.vehicle_display = (
                (record.vehicle_id.license_plate if record.vehicle_id else "")
                or (record.vehicle_id.display_name if record.vehicle_id else "")
                or record.vehicle_tid
                or "-"
            )

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
            "decision": value("decision") or "",
            "reason_code": value("reason_code") or "",
            "parking_area_id": int(value("parking_area_id") or 0),
            "lane_id": int(value("lane_id") or 0),
            "layout_revision": int(value("layout_revision") or 0),
            "vehicle_id": int(value("vehicle_id") or 0),
            "vehicle_tid": str(value("vehicle_tid") or "").strip(),
            "user_id": int(value("user_id") or 0),
            "user_tid": str(value("user_tid") or "").strip(),
            "borrow_id": int(value("borrow_id") or 0),
        }

    @api.constrains("decision", "reason_code")
    def _check_decision_reason_consistency(self):
        for record in self:
            if record.decision == "allowed" and record.reason_code:
                raise ValidationError(_("Allowed Parking Logs cannot contain a Reason."))
            if record.decision == "denied" and not record.reason_code:
                raise ValidationError(_("Denied Parking Logs require a Reason."))

    @api.model
    def create_idempotent(self, vals, existing_by_uid=None):
        uid = str((vals or {}).get("log_uid") or "").strip()
        if not uid:
            raise ValueError("missing_log_uid")
        values = dict(vals, log_uid=uid)
        existing = (
            (existing_by_uid or {}).get(uid)
            if existing_by_uid is not None
            else self.search([("log_uid", "=", uid)], limit=1)
        )
        if existing:
            if self._business_values(existing) != self._business_values(values):
                raise ValueError("log_uid_conflict")
            return existing, True
        try:
            with self.env.cr.savepoint():
                record = self.create(values)
        except IntegrityError:
            record = self.search([("log_uid", "=", uid)], limit=1)
            if not record or self._business_values(record) != self._business_values(values):
                raise ValueError("log_uid_conflict")
            if existing_by_uid is not None:
                existing_by_uid[uid] = record
            return record, True
        if existing_by_uid is not None:
            existing_by_uid[uid] = record
        return record, False

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        try:
            with self.env.cr.savepoint():
                records._broadcast_live_monitor()
        except Exception:
            _logger.exception("Unable to broadcast Cloud Parking Live Monitor log")
        return records

    def _live_monitor_display_meta(self):
        self.ensure_one()
        if self.event_type == "check_in":
            # Check-in Live Monitor is informational only. Business denials remain
            # in Parking Logs but must never create a guard alert on the Check-in screen.
            if self.decision != "allowed":
                return {
                    "display_kind": "ignore",
                    "display_title": "",
                    "display_reason": "",
                }
            return {
                "display_kind": "entry",
                "display_title": _("MỜI VÀO"),
                "display_reason": "",
            }

        # Check-out is a guard-verification workflow. Successful exits are shown
        # in the left-side exit list; denied exits are prominent guard alerts.
        if self.decision == "allowed":
            return {
                "display_kind": "entry",
                "display_title": _("ĐƯỢC PHÉP LẤY XE"),
                "display_reason": "",
            }
        reasons = {
            "parking_area_not_operational": _("BÃI XE TẠM NGƯNG VẬN HÀNH"),
            "missing_user_tid": _("THIẾU THẺ NGƯỜI DÙNG"),
            "multiple_user_tags": _("PHÁT HIỆN NHIỀU THẺ NGƯỜI DÙNG"),
            "user_tag_not_assigned": _("THẺ NGƯỜI DÙNG CHƯA ĐƯỢC GÁN"),
            "unauthorized_vehicle_user": _("NGƯỜI QUÉT KHÔNG ĐƯỢC PHÉP LẤY XE"),
            "vehicle_checked_in_other_area": _("XE ĐANG Ở MỘT BÃI XE KHÁC"),
        }
        return {
            "display_kind": "alert",
            "display_title": _("KHÔNG ĐƯỢC PHÉP LẤY XE"),
            "display_reason": reasons.get(
                self.reason_code or "unknown",
                _("VUI LÒNG LIÊN HỆ BỘ PHẬN VẬN HÀNH"),
            ),
        }

    def _live_monitor_payload(self):
        self.ensure_one()
        area = self.parking_area_id
        vehicle = self.vehicle_id
        # Never fall back to Vehicle Owner for Check-out. The guard must see the
        # actually detected User; displaying the Owner when User RFID is missing
        # would create a dangerous false identity.
        gate_user = self.user_id if self.event_type == "check_out" and self.user_id else self.env["nsp.user"].browse()
        vehicle_type = vehicle.vehicle_type_id if vehicle else self.env["nsp.vehicle.type"].browse()
        vehicle_type_code = str(vehicle_type.code or "").strip().lower() if vehicle_type else ""
        license_plate = ((vehicle.license_plate if vehicle else "") or self.vehicle_tid or "-").strip().upper()
        employee_name = (gate_user.name or "").strip().upper() if gate_user else ""
        avatar_url = ""
        if gate_user:
            avatar_field = next(
                (name for name in ("avatar_128", "image_128", "image_1920") if name in gate_user._fields),
                "",
            )
            if avatar_field:
                avatar_url = f"/web/image/nsp.user/{gate_user.id}/{avatar_field}"
        return {
            "id": self.id,
            "log_uid": self.log_uid,
            "parking_area_id": area.id if area else False,
            "lane_name": self.lane_id.name if self.lane_id else "",
            "event_type": self.event_type,
            "event_time": fields.Datetime.to_string(self.event_time) if self.event_time else "",
            "decision": self.decision,
            "vehicle_key": str(vehicle.id if vehicle else (self.vehicle_tid or self.log_uid)),
            "vehicle_type": vehicle_type_code or "other",
            "license_plate": license_plate,
            "employee_name": employee_name,
            "avatar_url": avatar_url,
            "has_checkout_user": bool(gate_user),
            **self._live_monitor_display_meta(),
        }

    def _broadcast_live_monitor(self):
        bus = self.env["bus.bus"]
        for log in self:
            if not log.parking_area_id:
                continue
            bus._sendone("broadcast", "nsp_parking_live_log", log._live_monitor_payload())
        return True

    def write(self, vals):
        raise AccessError(_(
            "Parking Logs are immutable Cloud business history. "
            "Create a correcting business event instead of modifying an existing log."
        ))

    def unlink(self):
        raise AccessError(_("Parking Logs are immutable and cannot be deleted."))
