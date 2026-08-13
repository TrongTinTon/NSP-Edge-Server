# -*- coding: utf-8 -*-
import logging

from odoo import api, fields, models, _
from odoo.exceptions import AccessError


_logger = logging.getLogger(__name__)


class ParkingLog(models.Model):
    """Immutable Edge business history for a resolved Parking movement.

    Controller raw RFID reads are short-lived acquisition evidence. A Parking Log
    is created only after Edge has matched a contextual Lane Antenna Sequence and
    resolved the business outcome. Allowed logs establish Vehicle parking state;
    denied logs are audit history only and never mutate continuity.
    """

    _name = "nsp.parking.log"
    _description = "NSP Parking Log"
    _rec_name = "log_uid"
    _order = "event_time desc, id desc"
    # Append-only business history already owns event_time/log_uid. Odoo
    # create/write audit columns add no value here and would cost four columns
    # plus write_date cursor indexes on a high-volume table.
    _log_access = False

    log_uid = fields.Char(
        string="Log UID",
        required=True,
        copy=False,
        help="Deterministic Edge idempotency key derived from the source detection group.",
    )
    event_time = fields.Datetime(
        string="Event Time", required=True, index=True,
        help="UTC time when the matched Vehicle movement completed.",
    )
    event_type = fields.Selection(
        [("check_in", "Check-in"), ("check_out", "Check-out")],
        string="Event Type", required=True,
    )
    decision = fields.Selection(
        [("allowed", "Allowed"), ("denied", "Denied")],
        string="Decision", required=True, default="allowed",
    )
    reason_code = fields.Selection([
        ("missing_user_tid", "Missing User RFID Tag"),
        ("multiple_user_tags", "Multiple User RFID Tags"),
        ("user_tag_not_assigned", "User Tag Not Assigned (Legacy)"),
        ("unauthorized_vehicle_user", "Unauthorized Vehicle User"),
        ("vehicle_checked_in_other_area", "Vehicle Checked In at Another Parking Area"),
        ("parking_area_not_operational", "Parking Area Not Operational"),
        # Kept only so historical rows from pre-19.0.10.33 remain readable.
        ("vehicle_not_found", "Vehicle Not Found (Legacy)"),
        ("check_out_without_check_in", "Check-out Without Check-in (Legacy)"),
        ("unknown", "Unknown"),
    ], string="Decision Reason", copy=False)
    # Direct contextual FKs are intentionally retained. They avoid joins on the
    # hottest history queries and preserve stable business identity.
    parking_area_id = fields.Many2one(
        "nsp.parking.area", string="Parking Area",
        ondelete="restrict", readonly=True,
    )
    layout_lane_id = fields.Many2one(
        "nsp.parking.layout.lane", string="Lane Configuration",
        ondelete="restrict", readonly=True,
    )
    lane_id = fields.Many2one(
        "nsp.parking.lane", string="Lane",
        ondelete="restrict", readonly=True, index=True,
    )
    layout_revision = fields.Integer(
        string="Parking Layout Revision", default=0, readonly=True,
    )

    vehicle_id = fields.Many2one(
        "nsp.vehicle", string="Vehicle", ondelete="set null", index=True,
    )
    vehicle_tid = fields.Char(
        string="Vehicle TID", readonly=True,
        help="RFID evidence captured at the movement time; kept because assignments can change later.",
    )
    user_id = fields.Many2one(
        "nsp.user", string="User", ondelete="set null", index=True,
    )
    user_tid = fields.Char(
        string="User TID", readonly=True,
        help="Check-out RFID evidence captured at the movement time.",
    )
    borrow_id = fields.Many2one(
        "nsp.vehicle.borrow", string="Vehicle Borrow", ondelete="set null",
    )

    vehicle_display = fields.Char(string="Vehicle", compute="_compute_vehicle_display")

    _sql_constraints = [
        ("log_uid_unique", "unique(log_uid)", "Parking Log UID must be unique."),
    ]

    def init(self):
        self.env.cr.execute(
            "DROP INDEX IF EXISTS nsp_parking_log_reason_code_index"
        )
        # Latest allowed movement is the hot path for Vehicle continuity.
        self.env.cr.execute(
            """
            CREATE INDEX IF NOT EXISTS nsp_parking_log_vehicle_state_idx
                ON nsp_parking_log (vehicle_id, event_time DESC, id DESC)
             WHERE decision = 'allowed' AND vehicle_id IS NOT NULL
            """
        )
        # Live Monitor and operational history should never need a relational OR/join.
        self.env.cr.execute(
            """
            CREATE INDEX IF NOT EXISTS nsp_parking_log_area_event_idx
                ON nsp_parking_log (parking_area_id, event_time DESC, id DESC)
             WHERE parking_area_id IS NOT NULL
            """
        )
        # Physical duplicate suppression after a Lane sequence match.
        self.env.cr.execute(
            """
            CREATE INDEX IF NOT EXISTS nsp_parking_log_lane_vehicle_event_idx
                ON nsp_parking_log
                   (layout_lane_id, layout_revision, vehicle_id, event_time DESC, id DESC)
             WHERE vehicle_id IS NOT NULL
            """
        )

    @api.depends("vehicle_id", "vehicle_id.license_plate", "vehicle_tid")
    def _compute_vehicle_display(self):
        for record in self:
            record.vehicle_display = (
                (record.vehicle_id.license_plate if record.vehicle_id else "")
                or (record.vehicle_id.display_name if record.vehicle_id else "")
                or record.vehicle_tid
                or "-"
            )

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        try:
            # Live Monitor is presentation-only; a bus failure must not poison the
            # authoritative Parking business transaction.
            with self.env.cr.savepoint():
                records._broadcast_live_monitor()
        except Exception:
            _logger.exception("Unable to broadcast NSP Parking Live Monitor log")
        return records

    def write(self, vals):
        raise AccessError(_(
            "Parking Logs are immutable Edge business history. "
            "Create a correcting business event instead of modifying an existing log."
        ))

    def unlink(self):
        raise AccessError(_("Parking Logs are immutable and cannot be deleted."))
