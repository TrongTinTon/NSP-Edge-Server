# -*- coding: utf-8 -*-
from odoo import api, fields, models
from psycopg2 import IntegrityError


class ParkingTransaction(models.Model):
    """Cloud mirror of final Edge parking transactions.

    Cloud stores the immutable business result plus compact topology snapshots.
    Current master topology is linked when it still exists, but history never
    depends on those master rows remaining present.
    """

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

    # Optional live links for navigation/enrichment. Snapshot fields below are
    # authoritative for history and survive later master-data deletion.
    controller_id = fields.Many2one("nsp.controller", ondelete="set null", index=True)
    controller_code = fields.Char(required=True, index=True)
    parking_area_id = fields.Many2one("nsp.parking.area", ondelete="set null", index=True)
    parking_area_code = fields.Char(required=True, index=True)
    lane_id = fields.Many2one("nsp.parking.lane", ondelete="set null", index=True)
    lane_code = fields.Char(required=True, index=True)
    antenna_id = fields.Many2one("nsp.device.antenna", ondelete="set null", index=True)
    device_id = fields.Many2one("nsp.device", related="antenna_id.device_id", readonly=True)
    serial_number = fields.Char(required=True, index=True)
    antenna_no = fields.Integer(required=True, index=True)

    status = fields.Selection(
        [("allowed", "Allowed"), ("denied", "Denied")],
        required=True,
        default="allowed",
        index=True,
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
    ], index=True, copy=False)
    error_message = fields.Text(copy=False)

    vehicle_id = fields.Many2one("nsp.vehicle", ondelete="set null", index=True)
    license_plate = fields.Char(index=True)
    vehicle_tid = fields.Char(index=True)
    user_id = fields.Many2one("nsp.user", ondelete="set null", index=True)
    user_tid = fields.Char(index=True)
    borrow_id = fields.Many2one("nsp.vehicle.borrow", ondelete="set null", index=True)

    parking_area_display = fields.Char(compute="_compute_display_values")
    lane_display = fields.Char(compute="_compute_display_values")
    vehicle_display = fields.Char(compute="_compute_display_values")

    _sql_constraints = [
        ("transaction_uid_unique", "unique(transaction_uid)", "Transaction UID must be unique."),
        ("transaction_antenna_positive", "CHECK(antenna_no > 0)", "Antenna number must be greater than zero."),
    ]

    @api.depends(
        "parking_area_id", "parking_area_code", "lane_id", "lane_code",
        "vehicle_id", "vehicle_id.license_plate", "license_plate", "vehicle_tid",
    )
    def _compute_display_values(self):
        for rec in self:
            rec.parking_area_display = (
                rec.parking_area_id.display_name if rec.parking_area_id else rec.parking_area_code or ""
            )
            rec.lane_display = rec.lane_id.display_name if rec.lane_id else rec.lane_code or ""
            rec.vehicle_display = (
                rec.vehicle_id.license_plate if rec.vehicle_id and rec.vehicle_id.license_plate
                else rec.license_plate or rec.vehicle_tid or ""
            )

    @api.model
    def _error_catalog(self):
        return dict(self._fields["error_code"].selection)

    @api.model
    def _error_code_from_message(self, message):
        text = str(message or "").lower()
        checks = [
            ("missing user", "missing_employee_tid"),
            ("vehicle not found", "vehicle_not_found"),
            ("not assigned", "employee_tag_not_assigned"),
            ("unauthorized", "unauthorized_vehicle_user"),
            ("without previous check-in", "check_out_without_check_in"),
            ("duplicate", "continuity_duplicate"),
            ("not operational", "parking_area_not_operational"),
        ]
        for needle, code in checks:
            if needle in text:
                return code
        return "unknown" if text else False

    @api.model
    def _normalize_error_code(self, code, message=False):
        value = str(code or "").strip()
        return value if value in self._error_catalog() else self._error_code_from_message(message)

    @api.model
    def _resolve_vehicle_by_tid(self, vehicle_tid):
        assignment = self.env["nsp.rfid.tag.assignment"].sudo().active_for_tid(vehicle_tid)
        return assignment.vehicle_id if assignment and assignment.vehicle_id.active else self.env["nsp.vehicle"].browse()

    @api.model
    def _resolve_user_by_tid(self, user_tid):
        assignment = self.env["nsp.rfid.tag.assignment"].sudo().active_for_tid(user_tid)
        return assignment.user_id if assignment and assignment.user_id.active else self.env["nsp.user"].browse()

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
