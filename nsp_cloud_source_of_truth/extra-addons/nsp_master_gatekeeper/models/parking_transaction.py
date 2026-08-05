from psycopg2 import IntegrityError

from odoo import api, fields, models, _
from odoo.exceptions import AccessError


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
    lane_id = fields.Many2one("nsp.parking.lane", ondelete="set null", index=True)
    lane_code = fields.Char(required=True, index=True)
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
            ("vehicle_not_found", "Vehicle Not Found"),
            ("user_tag_not_assigned", "User Tag Not Assigned"),
            ("unauthorized_vehicle_user", "Unauthorized Vehicle User"),
            ("check_out_without_check_in", "Check-out Without Previous Check-in"),
            ("continuity_duplicate", "Duplicate Event Type"),
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
    borrow_id = fields.Many2one("nsp.vehicle.borrow", ondelete="set null", index=True)
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
            ("vehicle not found", "vehicle_not_found"),
            ("not assigned", "user_tag_not_assigned"),
            ("unauthorized", "unauthorized_vehicle_user"),
            ("without previous check-in", "check_out_without_check_in"),
            ("duplicate", "continuity_duplicate"),
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
                if existing_by_uid is not None:
                    existing_by_uid[uid] = record
                return record, True
            raise
