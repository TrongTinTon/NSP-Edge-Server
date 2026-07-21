# -*- coding: utf-8 -*-
import logging
from datetime import datetime, timezone

from psycopg2 import IntegrityError

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)


class ParkingTransaction(models.Model):
    _name = "nsp.parking.transaction"
    _description = "Parking entry/exit event"
    _order = "time_entered desc, id desc"

    transaction_uid = fields.Char(
        string="Transaction UID", required=True, copy=False, index=True,
        help="Controller-generated idempotency key for this parking event.",
    )
    time_entered = fields.Datetime(
        string="Event Time", required=True, index=True,
        help="UTC event time reported by the Controller.",
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
        "nsp.device.antenna", string="Antenna", required=True,
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
    direction = fields.Selection(
        [("entry", "Entry"), ("exit", "Exit")],
        required=True, index=True,
    )
    status = fields.Selection(
        [("allowed", "Allowed"), ("denied", "Denied")],
        string="Decision", required=True, default="allowed", index=True,
    )
    error_code = fields.Selection([
        ("missing_vehicle_tid", "Missing Vehicle RFID Card"),
        ("missing_user_tid", "Missing User RFID Card"),
        ("vehicle_card_unknown", "Vehicle Card Not In Master List"),
        ("vehicle_not_found", "Vehicle Not Found"),
        ("user_card_unknown", "User Card Not In Master List"),
        ("user_not_assigned", "User Card Not Assigned"),
        ("borrow_not_allowed", "Borrow Not Allowed"),
        ("continuity_entry_missing", "Exit Without Previous Entry"),
        ("continuity_duplicate", "Duplicate Direction"),
        ("parking_area_not_operational", "Parking Area Not Operational"),
        ("no_antenna_rule", "No Active Antenna Rule"),
        ("ambiguous_antenna_mapping", "Ambiguous Antenna Mapping"),
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
    borrow_request_id = fields.Many2one(
        "nsp.vehicle.borrow.request", string="Vehicle Borrow Request",
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

    @api.depends(
        "lane_id", "lane_id.display_name", "lane_id.name", "lane_id.code",
        "lane_id.parking_area_id", "lane_id.parking_area_id.name", "lane_id.parking_area_id.code",
        "vehicle_id", "vehicle_id.display_name", "vehicle_id.license_plate", "vehicle_tid",
    )
    def _compute_display_values(self):
        for rec in self:
            area = rec.lane_id.parking_area_id if rec.lane_id else False
            rec.parking_area_display = (
                (area.name or area.code) if area else "-"
            )
            rec.lane_display = (
                rec.lane_id.display_name or rec.lane_id.name or rec.lane_id.code
            ) if rec.lane_id else "-"
            rec.vehicle_display = (
                (rec.vehicle_id.license_plate or rec.vehicle_id.display_name)
                if rec.vehicle_id else (rec.vehicle_tid or "-")
            )

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
            "missing_vehicle_tid": ("missing_tag", "critical"),
            "missing_user_tid": ("missing_tag", "critical"),
            "vehicle_card_unknown": ("auth", "critical"),
            "vehicle_not_found": ("auth", "critical"),
            "user_card_unknown": ("auth", "critical"),
            "user_not_assigned": ("auth", "critical"),
            "borrow_not_allowed": ("borrow", "critical"),
            "continuity_entry_missing": ("continuity", "warning"),
            "continuity_duplicate": ("continuity", "warning"),
            "parking_area_not_operational": ("config", "critical"),
            "no_antenna_rule": ("config", "critical"),
            "ambiguous_antenna_mapping": ("config", "critical"),
            "unknown": ("unknown", "warning"),
        }

    @api.model
    def _error_code_from_message(self, message):
        text = str(message or "").lower()
        mapping = (
            ("vehicle tid is required", "missing_vehicle_tid"),
            ("missing vehicle", "missing_vehicle_tid"),
            ("vehicle card", "vehicle_card_unknown"),
            ("vehicle tid is not defined", "vehicle_card_unknown"),
            ("vehicle not found", "vehicle_not_found"),
            ("user tid is required", "missing_user_tid"),
            ("missing user", "missing_user_tid"),
            ("employee tid", "missing_user_tid"),
            ("user tid is not defined", "user_card_unknown"),
            ("user tid is not assigned", "user_not_assigned"),
            ("borrow", "borrow_not_allowed"),
            ("no previous entry", "continuity_entry_missing"),
            ("already", "continuity_duplicate"),
            ("not operational", "parking_area_not_operational"),
            ("no active", "no_antenna_rule"),
            ("ambiguous antenna", "ambiguous_antenna_mapping"),
        )
        for marker, code in mapping:
            if marker in text:
                return code
        return "unknown" if text else False

    @api.model
    def _normalize_error_code(self, code, message=False):
        raw = str(code or "").strip().lower().replace("-", "_").replace(" ", "_")
        aliases = {
            "missing_vehicle_card": "missing_vehicle_tid",
            "vehicle_tid_missing": "missing_vehicle_tid",
            "vehicle_missing": "missing_vehicle_tid",
            "missing_employee_tid": "missing_user_tid",
            "missing_employee_card": "missing_user_tid",
            "user_tid_missing": "missing_user_tid",
            "employee_tid_missing": "missing_user_tid",
            "user_not_active": "user_card_unknown",
            "user_vehicle_mismatch": "borrow_not_allowed",
            "vehicle_unknown": "vehicle_not_found",
        }
        raw = aliases.get(raw, raw)
        if raw in self._error_catalog():
            return raw
        return self._error_code_from_message(message)

    @api.model
    def _primary_decision_error(self, error_items):
        """Return one canonical reason code and one readable message.

        Parking Log stores a single business decision. If several checks fail,
        keep the highest-severity reason and combine messages for diagnostics.
        """
        catalog = self._error_catalog()
        rank = {"info": 0, "warning": 1, "error": 2, "critical": 3}
        normalized = []
        messages = []
        for item in error_items or []:
            if isinstance(item, (list, tuple)):
                raw_code = item[0] if item else False
                message = item[1] if len(item) > 1 else False
            else:
                raw_code = False
                message = item
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

    def _find_vehicle(self, vehicle_tid):
        tid = str(vehicle_tid or "").strip()
        if not tid:
            return self.env["nsp.vehicle"].browse()
        line = self.env["nsp.vehicle.card"].sudo().search([
            ("card_id.tid", "=", tid),
            ("state", "=", "active"),
        ], limit=1)
        return line.vehicle_id if line else self.env["nsp.vehicle"].browse()

    def _card_from_tid(self, tid, card_type):
        tid = str(tid or "").strip()
        if not tid:
            return self.env["nsp.rfid.card"].browse()
        return self.env["nsp.rfid.card"].sudo().search([
            ("tid", "=", tid),
            ("card_type", "=", card_type),
        ], limit=1)

    def _user_from_user_tid(self, user_tid):
        card = self._card_from_tid(user_tid, "user_card")
        if not card:
            return self.env["nsp.user"].browse(), card
        line = self.env["nsp.user.card"].sudo().search([
            ("card_id", "=", card.id),
            ("state", "=", "active"),
        ], limit=1)
        return (line.user_id if line else self.env["nsp.user"].browse()), card

    @api.model
    def _validate_vehicle_borrow_access(self, vehicle, user, event_time):
        Borrow = self.env["nsp.vehicle.borrow.request"].sudo()
        if not vehicle or not user or (vehicle.owner_id and vehicle.owner_id == user):
            return True, "", Borrow.browse()
        try:
            borrow = Borrow.find_valid_borrow(vehicle, borrower=user, borrow_time=event_time)
        except Exception:
            borrow = Borrow.browse()
        if not borrow:
            return False, _(
                "Vehicle is not currently borrowed by this user, or the borrow request is not approved/valid/returned."
            ), Borrow.browse()
        return True, "", borrow

    @api.model
    def _validate_vehicle_continuity(self, vehicle, direction, event_time):
        if not vehicle or not direction:
            return True, ""
        domain = [("vehicle_id", "=", vehicle.id), ("status", "=", "allowed")]
        if event_time:
            domain.append(("time_entered", "<", event_time))
        last = self.search(domain, order="time_entered desc, id desc", limit=1)
        if not last:
            if direction == "exit":
                return False, _(
                    "Continuity error: vehicle has no previous Entry but an Exit event was received."
                )
            return True, ""
        if last.direction == direction:
            label = dict(self._fields["direction"].selection).get(direction, direction)
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

        event_time = value("time_entered")
        if event_time:
            event_time = fields.Datetime.to_string(fields.Datetime.to_datetime(event_time))
        return {
            "controller_id": int(value("controller_id") or 0),
            "lane_id": int(value("lane_id") or 0),
            "antenna_id": int(value("antenna_id") or 0),
            "time_entered": event_time or "",
            "direction": value("direction") or "",
            "status": value("status") or "",
            "vehicle_id": int(value("vehicle_id") or 0),
            "vehicle_tid": str(value("vehicle_tid") or "").strip(),
            "user_id": int(value("user_id") or 0),
            "user_tid": str(value("user_tid") or "").strip(),
            "borrow_request_id": int(value("borrow_request_id") or 0),
            "error_code": value("error_code") or "",
            "error_message": str(value("error_message") or "").strip(),
        }

    @api.model
    def create_idempotent(self, vals):
        """Create an immutable parking event or return the exact duplicate."""
        uid = str(vals.get("transaction_uid") or "").strip()
        if not uid:
            raise ValidationError(_("missing_transaction_uid"))
        vals = dict(vals, transaction_uid=uid)
        existing = self.search([("transaction_uid", "=", uid)], limit=1)
        if existing:
            if self._business_values(existing) != self._business_values(vals):
                raise ValidationError(_(
                    "transaction_uid_conflict: Transaction UID already exists with different event data."
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
                    "transaction_uid_conflict: Transaction UID already exists with different event data."
                ))
            return existing, True

    @api.model
    def ingest_controller_log(self, controller, payload):
        """Validate and persist one Controller event.

        Controller sends only its device identity and detected TIDs. Server-side
        topology resolves Parking Area, Lane and direction.
        """
        if not isinstance(payload, dict):
            raise ValidationError(_("invalid_payload"))
        allowed_fields = {
            "transaction_uid", "controller_code", "serial_number", "antenna_no",
            "check_time", "vehicle_tid", "user_tid",
        }
        unsupported = sorted(set(payload) - allowed_fields)
        if unsupported:
            raise ValidationError(_(
                "invalid_payload: unsupported field(s): %s"
            ) % ", ".join(unsupported))

        controller_code = str(payload.get("controller_code") or "").strip()
        if controller_code and controller_code != controller.controller_id:
            raise ValidationError(_("route_not_allowed"))
        transaction_uid = str(payload.get("transaction_uid") or "").strip()
        serial_number = str(payload.get("serial_number") or "").strip().upper()
        try:
            antenna_no = int(payload.get("antenna_no") or 0)
        except Exception as exc:
            raise ValidationError(_("invalid_payload: antenna_no")) from exc
        event_time = self._safe_datetime_value(payload.get("check_time"), default_now=False)
        if not transaction_uid:
            raise ValidationError(_("missing_transaction_uid"))
        if not serial_number:
            raise ValidationError(_("serial_number is required"))
        if antenna_no <= 0:
            raise ValidationError(_("antenna_no is required"))
        if not event_time:
            raise ValidationError(_("check_time is required"))

        if not self.env["nsp.device.whitelist"].sudo().is_device_whitelisted(serial_number):
            if "nsp.notification" in self.env.registry.models:
                self.env["nsp.notification"].sudo().notify_device_not_whitelisted(
                    serial_number, controller.controller_id,
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

        mappings = self.env["nsp.parking.lane.antenna.mapping"].sudo().search([
            ("antenna_ref_id", "=", antenna.id),
            ("lane_id.active", "=", True),
        ])
        if not mappings:
            raise ValidationError(_("no_antenna_rule"))
        if len(mappings) > 1:
            raise ValidationError(_("ambiguous_antenna_mapping"))
        mapping = mappings[0]
        lane = mapping.lane_id
        parking_area = lane.parking_area_id
        direction = mapping.direction
        if lane.controller_id != controller:
            raise ValidationError(_("controller_not_in_scope"))
        if direction not in ("entry", "exit"):
            raise ValidationError(_("invalid_direction"))

        vehicle_tid = str(payload.get("vehicle_tid") or "").strip()
        user_tid = str(payload.get("user_tid") or "").strip()
        vehicle = self._find_vehicle(vehicle_tid)
        vehicle_card = self._card_from_tid(vehicle_tid, "vehicle_card") if vehicle_tid else self.env["nsp.rfid.card"].browse()
        user, user_card = self._user_from_user_tid(user_tid) if user_tid else (
            self.env["nsp.user"].browse(), self.env["nsp.rfid.card"].browse()
        )

        continuity_ok, continuity_error = self._validate_vehicle_continuity(
            vehicle, direction, event_time
        )
        borrow_ok, borrow_error, borrow_request = (
            self._validate_vehicle_borrow_access(vehicle, user, event_time)
            if user_tid and user else
            (True, "", self.env["nsp.vehicle.borrow.request"].browse())
        )

        errors = []
        if parking_area.state != "operational":
            errors.append(("parking_area_not_operational", _("Parking Area is not operational.")))
        if lane.required_vehicle_tid and not vehicle_tid:
            errors.append(("missing_vehicle_tid", _("Vehicle RFID card/TID was not detected.")))
        elif vehicle_tid and not vehicle_card:
            errors.append(("vehicle_card_unknown", _("Vehicle TID is not defined as a Vehicle Card.")))
        if vehicle_tid and not vehicle:
            errors.append(("vehicle_not_found", _("Vehicle could not be resolved from vehicle_tid.")))
        if lane.required_user_tid and not user_tid:
            errors.append(("missing_user_tid", _("User RFID card/TID was not detected.")))
        elif user_tid and not user_card:
            errors.append(("user_card_unknown", _("User TID is not defined as a User Card.")))
        elif user_tid and user_card and not user:
            errors.append(("user_not_assigned", _("User TID is not assigned to an NSP User.")))
        if not borrow_ok:
            errors.append(("borrow_not_allowed", borrow_error))
        if not continuity_ok:
            errors.append((
                self._normalize_error_code(False, continuity_error) or "continuity_duplicate",
                continuity_error,
            ))
        reason_code, reason_message = self._primary_decision_error(errors)

        vals = {
            "transaction_uid": transaction_uid,
            "time_entered": event_time,
            "controller_id": controller.id,
            "lane_id": lane.id,
            "antenna_id": antenna.id,
            "direction": direction,
            "status": "denied" if errors else "allowed",
            "error_code": reason_code or False,
            "error_message": reason_message or False,
            "vehicle_id": vehicle.id if vehicle else False,
            "vehicle_tid": vehicle_tid or False,
            "user_id": user.id if user else False,
            "user_tid": user_tid or False,
            "borrow_request_id": borrow_request.id if borrow_request else False,
        }
        return self.create_idempotent(vals)
