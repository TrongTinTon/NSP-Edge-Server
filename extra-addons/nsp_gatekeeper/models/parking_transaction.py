# -*- coding: utf-8 -*-
import hashlib
import logging
from datetime import datetime, timezone

from psycopg2 import IntegrityError

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError
from odoo.osv import expression

_logger = logging.getLogger(__name__)


class ParkingTransaction(models.Model):
    _name = "nsp.parking.transaction"
    _description = "Parking transactions received from the controller when vehicles enter/exit"
    _order = "time_entered desc, id desc"

    controller_id = fields.Many2one(
        "nsp.controller",
        string="Controller",
        help="Controller that sent this transaction",
        required=True,
        ondelete="cascade",
        index=True,
    )
    gate_id = fields.Many2one("nsp.gate", string="Gate", ondelete="set null", index=True)
    branch_id = fields.Many2one("nsp.branch", string="Branch", related="gate_id.branch_id", store=True, readonly=True, index=True)
    gate_code = fields.Char(string="Gate Code", index=True)
    lane_id = fields.Many2one("nsp.gate.lane", string="Lane", ondelete="set null", index=True)
    lane_code = fields.Char(string="Lane Code", index=True)
    lane_display = fields.Char(string="Lane", compute="_compute_lane_display")
    gate_display = fields.Char(
        string="Gate Name",
        compute="_compute_gate_display",
        help="Human-readable Gate name used by Vehicle In/Out Logs and the Parking Display. Falls back to Gate Code when the related Gate record is not linked.",
    )
    transaction_uid = fields.Char(string="Transaction UID", copy=False, index=True)
    controller_local_id = fields.Char(string="Controller Local ID", copy=False, index=True)
    time_entered = fields.Datetime(string="Event Time", help="Time that the vehicle entered/exited the parking lot", required=True, index=True)
    direction = fields.Selection([
        ("entry", "Entry"),
        ("exit", "Exit"),
    ], string="Direction", help="Whether the vehicle entered or exited", required=True, index=True)

    status = fields.Selection([
        ("allowed", "Allowed"),
        ("denied", "Denied"),
    ], string="Status", help="Whether the transaction was allowed or denied", default="allowed", index=True)
    error_message = fields.Text(string="Error Message")
    error_code = fields.Selection([
        ("missing_vehicle_tid", "Missing Vehicle RFID Card"), ("missing_user_tid", "Missing Employee RFID Card"),
        ("vehicle_card_unknown", "Vehicle Card Not In Master List"), ("vehicle_not_found", "Vehicle Not Found"),
        ("user_card_unknown", "Employee Card Not In Master List"), ("user_not_assigned", "Employee Card Not Assigned"),
        ("borrow_not_allowed", "Borrow Not Allowed"), ("continuity_entry_missing", "Exit Without Previous Entry"),
        ("continuity_duplicate", "Duplicate Direction"), ("gate_missing", "Gate Missing/Ambiguous"),
        ("gate_not_operational", "Gate Not Operational"), ("gate_config_not_applied", "Gate Config Not Applied"),
        ("gate_config_stale", "Gate Config Stale"), ("no_antenna_rule", "No Active Antenna Rule"),
        ("antenna_sequence_invalid", "Invalid Antenna Sequence"), ("detection_window_missed", "Detection Window Missed"),
        ("user_tid_wrong_antenna", "Employee Card Wrong Antenna"), ("controller_reported_error", "Controller Reported Error"),
        ("unknown", "Unknown"),
    ], string="Alert Code", index=True, copy=False, help="Standardized alert/error code used by IT dashboard and reports.")
    error_codes = fields.Char(string="Alert Codes", index=True, copy=False, help="Comma-separated list of all standardized error codes detected for this event.")
    error_category = fields.Selection([("missing_tag", "Missing Tag"), ("auth", "Authentication"), ("borrow", "Vehicle Borrow"), ("continuity", "Parking Continuity"), ("config", "Configuration"), ("device", "Device"), ("unknown", "Unknown")], string="Alert Category", index=True, copy=False)
    error_severity = fields.Selection([("info", "Info"), ("warning", "Warning"), ("error", "Error"), ("critical", "Critical")], string="Alert Severity", index=True, copy=False)
    alert_required = fields.Boolean(string="Requires Attention", default=False, index=True, copy=False)

    vehicle_id = fields.Many2one("nsp.vehicle", string="Vehicle", ondelete="set null", index=True)
    license_plate = fields.Char(string="License Plate", index=True)
    vehicle_tid = fields.Char(string="Vehicle TID", index=True)
    user_tid = fields.Char(string="User TID", index=True)
    vehicle_code = fields.Char(string="Vehicle Code", index=True, copy=False)
    user_id = fields.Many2one("nsp.user", string="User", ondelete="set null", index=True)
    user_code = fields.Char(string="User Code", index=True, copy=False)
    vehicle_display = fields.Char(string="Vehicle", compute="_compute_vehicle_display", help="Display value used by the parking screen and Vehicle In/Out Logs list.")

    device_id = fields.Many2one("nsp.device", string="Device", ondelete="set null", index=True)
    serial_number = fields.Char(string="Reader Serial Number", index=True)
    antenna_no = fields.Integer(string="Primary Antenna No", index=True)
    device_serial = fields.Char(string="Legacy Device Serial", index=True)
    antenna_id = fields.Integer(string="Legacy Antenna ID", index=True)
    payload_hash = fields.Char(string="Normalized Payload Hash", copy=False, index=True)
    antenna_sequence = fields.Char(string="Antenna Sequence", help="Detected antenna sequence for the vehicle TID")
    lane_rule_id = fields.Many2one("nsp.gate.lane.antenna.mapping", string="Lane Antenna Rule", ondelete="set null", index=True)
    effective_direction = fields.Selection([
        ("entry", "Entry"),
        ("exit", "Exit"),
        ("both", "Both"),
        ("unknown", "Unknown"),
    ], string="Effective Direction", default="unknown", index=True,
       help="Direction resolved from the Lane Antenna Mapping used for this event.")

    config_revision = fields.Integer(string="Controller Config Revision", index=True)

    vehicle_card_id = fields.Many2one(
        "nsp.rfid.card",
        string="Vehicle Card",
        ondelete="set null",
        domain=[("card_type", "=", "vehicle_card")],
        help="Vehicle card read in the transaction",
    )
    user_card_id = fields.Many2one(
        "nsp.rfid.card",
        string="User Card",
        ondelete="set null",
        domain=[("card_type", "=", "user_card")],
        help="User card read in the transaction",
    )
    borrow_request_id = fields.Many2one(
        "nsp.vehicle.borrow.request",
        string="Vehicle Borrow Request",
        ondelete="set null",
        help="Approved borrow request that allowed this non-owner user to use the vehicle.",
    )
    borrower_id = fields.Many2one("nsp.user", string="Borrower", ondelete="set null")

    @api.depends("gate_id", "gate_id.name", "gate_id.code", "controller_id", "gate_code")
    def _compute_gate_display(self):
        Gate = self.env["nsp.gate"].sudo()
        for rec in self:
            if rec.gate_id:
                rec.gate_display = rec.gate_id.name or rec.gate_id.code or rec.gate_code or "-"
                continue

            gate_code = (rec.gate_code or "").strip()
            gate = Gate.browse()
            if gate_code and rec.controller_id:
                gate = Gate.search(expression.AND([
                    [("controller_ids", "in", [rec.controller_id.id])],
                    ["|", ("code", "=ilike", gate_code), ("name", "=ilike", gate_code)],
                ]), limit=1)
            rec.gate_display = (gate.name if gate else "") or gate_code or "-"

    @api.depends("lane_id", "lane_id.name", "lane_id.code", "lane_code")
    def _compute_lane_display(self):
        for rec in self:
            if rec.lane_id:
                rec.lane_display = rec.lane_id.display_name or rec.lane_id.name or rec.lane_id.code or rec.lane_code or "-"
            else:
                rec.lane_display = rec.lane_code or "-"


    @api.depends("license_plate", "vehicle_tid", "vehicle_id")
    def _compute_vehicle_display(self):
        for rec in self:
            plate = (rec.license_plate or "").strip()
            tid = (rec.vehicle_tid or "").strip()
            vehicle_name = rec.vehicle_id.display_name if rec.vehicle_id else ""
            rec.vehicle_display = plate or vehicle_name or tid or "-"


    _sql_constraints = [
        ("transaction_uid_unique", "unique(transaction_uid)", "Transaction UID must be unique."),
    ]

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
            return fields.Datetime.to_string(parsed) if parsed else (fields.Datetime.now() if default_now else False)
        except Exception:
            return fields.Datetime.now() if default_now else False

    @api.model
    def _normalize_direction(self, value):
        raw = str(value or "").strip().lower()
        if raw in ("in", "entry", "enter", "checkin", "check_in", "vao", "entrance"):
            return "entry"
        if raw in ("out", "exit", "leave", "checkout", "check_out", "ra"):
            return "exit"
        return "entry"



    @api.model
    def _error_catalog(self):
        return {
            "missing_vehicle_tid": ("missing_tag", "critical"), "missing_user_tid": ("missing_tag", "critical"),
            "vehicle_card_unknown": ("auth", "critical"), "vehicle_not_found": ("auth", "critical"),
            "user_card_unknown": ("auth", "critical"), "user_not_assigned": ("auth", "critical"),
            "borrow_not_allowed": ("borrow", "critical"),
            "continuity_entry_missing": ("continuity", "warning"), "continuity_duplicate": ("continuity", "warning"),
            "gate_missing": ("config", "critical"), "gate_not_operational": ("config", "critical"),
            "gate_config_not_applied": ("config", "critical"), "gate_config_stale": ("config", "warning"),
            "no_antenna_rule": ("config", "critical"), "antenna_sequence_invalid": ("config", "warning"),
            "detection_window_missed": ("missing_tag", "warning"), "user_tid_wrong_antenna": ("missing_tag", "warning"),
            "controller_reported_error": ("device", "warning"), "unknown": ("unknown", "warning"),
        }

    @api.model
    def _error_code_from_message(self, message):
        text = str(message or "").lower()
        mapping = (("vehicle tid is required", "missing_vehicle_tid"), ("missing vehicle", "missing_vehicle_tid"),
                   ("vehicle card", "vehicle_card_unknown"), ("vehicle tid is not defined", "vehicle_card_unknown"),
                   ("vehicle not found", "vehicle_not_found"), ("user tid is required", "missing_user_tid"),
                   ("missing user", "missing_user_tid"), ("employee tid", "missing_user_tid"),
                   ("user tid is not defined", "user_card_unknown"), ("user tid is not assigned", "user_not_assigned"),
                   ("borrow", "borrow_not_allowed"), ("no previous entry", "continuity_entry_missing"),
                   ("already", "continuity_duplicate"), ("gate is missing", "gate_missing"), ("gate/port is missing", "gate_missing"),
                   ("not operational", "gate_not_operational"), ("config is not applied", "gate_config_not_applied"),
                   ("gate config is not applied", "gate_config_not_applied"), ("gate config has changed", "gate_config_stale"),
                   ("stale config", "gate_config_stale"), ("no active", "no_antenna_rule"),
                   ("antenna sequence", "antenna_sequence_invalid"), ("detection window", "detection_window_missed"),
                   ("exit-direction antenna", "user_tid_wrong_antenna"))
        for marker, code in mapping:
            if marker in text:
                return code
        return "unknown" if text else False

    @api.model
    def _normalize_error_code(self, code, message=False):
        raw = str(code or "").strip().lower().replace("-", "_").replace(" ", "_")
        aliases = {"missing_vehicle_card": "missing_vehicle_tid", "vehicle_tid_missing": "missing_vehicle_tid", "vehicle_missing": "missing_vehicle_tid", "missing_employee_tid": "missing_user_tid", "missing_employee_card": "missing_user_tid", "user_tid_missing": "missing_user_tid", "employee_tid_missing": "missing_user_tid", "user_not_active": "user_card_unknown", "user_vehicle_mismatch": "borrow_not_allowed", "vehicle_unknown": "vehicle_not_found"}
        raw = aliases.get(raw, raw)
        if raw in self._error_catalog():
            return raw
        return self._error_code_from_message(message)

    @api.model
    def _apply_error_metadata(self, vals, error_items):
        catalog = self._error_catalog()
        codes, messages = [], []
        for item in error_items or []:
            if isinstance(item, (list, tuple)):
                code = self._normalize_error_code(item[0] if item else False, item[1] if len(item) > 1 else False)
                message = item[1] if len(item) > 1 else False
            else:
                message = item
                code = self._normalize_error_code(False, message)
            if code and code not in codes:
                codes.append(code)
            if message:
                messages.append(str(message))
        if not codes and vals.get("status") == "denied":
            codes.append(self._normalize_error_code(vals.get("error_code"), vals.get("error_message")) or "unknown")
        if not codes:
            vals.update({"error_code": False, "error_codes": False, "error_category": False, "error_severity": False, "alert_required": False})
            return vals
        rank = {"info": 0, "warning": 1, "error": 2, "critical": 3}
        first = codes[0]
        category, severity = catalog.get(first, ("unknown", "warning"))
        for code in codes:
            cat, sev = catalog.get(code, ("unknown", "warning"))
            if rank.get(sev, 0) > rank.get(severity, 0):
                category, severity = cat, sev
        vals.update({"error_code": first, "error_codes": ",".join(codes), "error_category": category, "error_severity": severity, "alert_required": vals.get("status") == "denied" or severity in ("warning", "error", "critical")})
        if messages:
            vals["error_message"] = " ".join(messages)
        return vals




    def _find_vehicle(self, payload):
        Vehicle = self.env["nsp.vehicle"].sudo()
        vehicle_code = str(payload.get("vehicle_code") or "").strip()
        if vehicle_code:
            for field_name in ("vehicle_code", "code"):
                if field_name in Vehicle._fields:
                    vehicle = Vehicle.search([(field_name, "=", vehicle_code)], limit=1)
                    if vehicle:
                        return vehicle
        tid = str(payload.get("vehicle_tid") or "").strip()
        if tid:
            vehicle_card = self.env["nsp.vehicle.card"].sudo().search([("card_id.tid", "=", tid), ("state", "=", "active")], limit=1)
            if vehicle_card:
                return vehicle_card.vehicle_id
        return Vehicle.browse()

    def _card_from_tid(self, tid, card_type):
        """Return an existing Master Card only.

        Controller logs must use cards already defined in RFID Cards.
        Unknown TIDs are not auto-created here because that would bypass card
        assignment/approval rules.
        """
        tid = str(tid or "").strip()
        if not tid:
            return self.env["nsp.rfid.card"].browse()
        Card = self.env["nsp.rfid.card"].sudo()
        return Card.search([("tid", "=", tid), ("card_type", "=", card_type)], limit=1)

    def _user_from_user_tid(self, user_tid):
        user_card = self._card_from_tid(user_tid, "user_card")
        if not user_card:
            return self.env["nsp.user"].browse(), user_card
        try:
            user_card_line = self.env["nsp.user.card"].sudo().search([("card_id", "=", user_card.id), ("state", "=", "active")], limit=1)
            if user_card_line:
                return user_card_line.user_id, user_card
        except Exception:
            pass
        return self.env["nsp.user"].browse(), user_card








    @api.model
    def _normalized_payload_hash(self, controller, vals):
        business = {
            "controller_code": controller.controller_id if controller else "",
            "gate_code": vals.get("gate_code") or "",
            "lane_code": vals.get("lane_code") or "",
            "direction": vals.get("direction") or "",
            "check_time": str(vals.get("time_entered") or ""),
            "vehicle_tid": vals.get("vehicle_tid") or "",
            "user_tid": vals.get("user_tid") or "",
            "vehicle_code": vals.get("vehicle_code") or "",
            "user_code": vals.get("user_code") or "",
            "decision": vals.get("status") or "",
            "decision_reason_code": vals.get("error_code") or "",
        }
        raw = repr(sorted(business.items())).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()



    @api.model
    def _validate_vehicle_borrow_access(self, vehicle, user, event_time):
        """Validate non-owner user access through an approved vehicle borrow request.

        Owner is always allowed. A different user is allowed only when an approved
        borrow slip exists, is inside its valid time window, and has not been returned.
        """
        if not vehicle or not user:
            return True, "", self.env["nsp.vehicle.borrow.request"].browse() if "nsp.vehicle.borrow.request" in self.env.registry.models else False
        if vehicle.owner_id and vehicle.owner_id == user:
            return True, "", self.env["nsp.vehicle.borrow.request"].browse() if "nsp.vehicle.borrow.request" in self.env.registry.models else False
        if "nsp.vehicle.borrow.request" not in self.env.registry.models:
            return False, _("Vehicle borrow module is not installed; non-owner vehicle access is denied."), False
        Borrow = self.env["nsp.vehicle.borrow.request"].sudo()
        borrow_time = self._safe_datetime_value(event_time) if isinstance(event_time, str) else (event_time or fields.Datetime.now())
        try:
            borrow = Borrow.find_valid_borrow(vehicle, borrower=user, borrow_time=borrow_time)
        except Exception:
            borrow = Borrow.browse()
        if not borrow:
            return False, _("Vehicle is not currently borrowed by this user, or the borrow request is not approved/valid/returned."), Borrow.browse()
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
                return False, _("Continuity error: vehicle has no previous Entry but an Exit event was received.")
            return True, ""
        if last.direction == direction:
            return False, _("Continuity error: last valid event for this vehicle is already %s.") % dict(self._fields["direction"].selection).get(direction, direction)
        return True, ""

    @api.model
    def ingest_controller_log(self, controller, payload):
        """Persist one normalized Controller detection and evaluate it at Edge Server.

        Controller is responsible for Reader/Antenna collection and resolving
        lane/direction/detection rule. Edge Server is responsible for card,
        vehicle, user, borrow and continuity decisions. Raw antenna events are
        intentionally not accepted or stored by this API.
        """
        if not isinstance(payload, dict):
            raise ValidationError(_("invalid_payload"))

        allowed_fields = {
            "transaction_uid", "controller_code", "gate_code", "lane_code",
            "direction", "check_time", "vehicle_tid", "user_tid",
            "vehicle_code", "config_revision", "config_hash",
        }
        unsupported = sorted(set(payload) - allowed_fields)
        if unsupported:
            raise ValidationError(_(
                "invalid_payload: unsupported field(s): %s",
                ", ".join(unsupported),
            ))

        controller_code = str(payload.get("controller_code") or "").strip()
        if controller_code and controller_code != controller.controller_id:
            raise ValidationError(_("route_not_allowed"))

        transaction_uid = str(payload.get("transaction_uid") or "").strip()
        if not transaction_uid:
            raise ValidationError(_("missing_transaction_uid"))
        gate_code = str(payload.get("gate_code") or "").strip().upper()
        lane_code = str(payload.get("lane_code") or "").strip().upper()
        if not gate_code:
            raise ValidationError(_("gate_code is required"))
        if not lane_code:
            raise ValidationError(_("lane_code is required"))
        direction = self._normalize_direction(payload.get("direction"))
        if direction not in ("entry", "exit"):
            raise ValidationError(_("invalid_direction"))
        event_time = self._safe_datetime_value(payload.get("check_time"), default_now=False)
        if not event_time:
            raise ValidationError(_("check_time is required"))
        try:
            config_revision = int(payload.get("config_revision") or 0)
        except Exception as exc:
            raise ValidationError(_("invalid_payload: config_revision")) from exc
        if config_revision <= 0:
            raise ValidationError(_("config_revision is required"))

        Gate = self.env["nsp.gate"].sudo()
        gate = Gate.search([
            ("code", "=", gate_code),
            ("controller_ids", "in", [controller.id]),
        ], limit=1)
        if not gate:
            raise ValidationError(_("gate_not_found"))
        lane = self.env["nsp.gate.lane"].sudo().search([
            ("gate_id", "=", gate.id),
            ("code", "=", lane_code),
            ("active", "=", True),
        ], limit=1)
        if not lane:
            raise ValidationError(_("lane_not_found"))
        group = lane.antenna_group_ids.filtered(
            lambda rec: rec.active and rec.direction == direction
        )[:1]
        if not group:
            raise ValidationError(_("missing_antenna_group"))

        vehicle_tid = str(payload.get("vehicle_tid") or "").strip()
        user_tid = str(payload.get("user_tid") or "").strip()
        vehicle = self._find_vehicle({
            "vehicle_code": payload.get("vehicle_code"),
            "vehicle_tid": vehicle_tid,
        })
        vehicle_card = self._card_from_tid(vehicle_tid, "vehicle_card") if vehicle_tid else self.env["nsp.rfid.card"].browse()
        user, user_card = self._user_from_user_tid(user_tid) if user_tid else (
            self.env["nsp.user"].browse(), self.env["nsp.rfid.card"].browse()
        )

        def record_code(record, field_names):
            if not record:
                return False
            for field_name in field_names:
                if field_name in record._fields:
                    value = str(record[field_name] or "").strip()
                    if value:
                        return value
            return False

        vehicle_code = str(payload.get("vehicle_code") or "").strip() or record_code(vehicle, ("vehicle_code", "code"))
        user_code = record_code(user, ("user_code", "code"))
        license_plate = str(vehicle.license_plate or "").strip() if vehicle and "license_plate" in vehicle._fields else ""

        config_errors = []
        if gate.operation_state != "operational" or gate.gate_status != "active":
            config_errors.append(("gate_not_operational", _("Gate is not operational.")))
        gate._refresh_config_hash(bump_if_changed=False)
        if gate.config_state != "applied" or gate.applied_config_revision != gate.config_revision or gate.applied_config_hash != gate.config_hash:
            config_errors.append(("gate_config_not_applied", _("Gate configuration is not applied or is stale.")))
        if config_revision != gate.applied_config_revision:
            config_errors.append(("gate_config_stale", _("Controller is using a stale config_revision.")))
        config_hash = str(payload.get("config_hash") or "").strip()
        if config_hash and config_hash != gate.applied_config_hash:
            config_errors.append(("gate_config_stale", _("Controller is using a stale config_hash.")))
        mappings = group.antenna_mapping_ids.filtered(
            lambda rec: rec.is_active and rec.antenna_ref_id and rec.antenna_ref_id.is_active and rec.device_id.managed
        )
        if not mappings:
            config_errors.append(("no_antenna_rule", _("No enabled antenna mapping exists for this lane direction.")))

        continuity_ok, continuity_error = self._validate_vehicle_continuity(vehicle, direction, event_time)
        borrow_ok, borrow_error, borrow_request = self._validate_vehicle_borrow_access(vehicle, user, event_time) if user_tid and user else (
            True,
            "",
            self.env["nsp.vehicle.borrow.request"].browse() if "nsp.vehicle.borrow.request" in self.env.registry.models else False,
        )

        vals = {
            "controller_id": controller.id,
            "gate_id": gate.id,
            "gate_code": gate.code,
            "lane_id": lane.id,
            "lane_code": lane.code,
            "transaction_uid": transaction_uid,
            "time_entered": event_time,
            "direction": direction,
            "status": "allowed",
            "vehicle_id": vehicle.id if vehicle else False,
            "vehicle_code": vehicle_code or False,
            "license_plate": license_plate or False,
            "vehicle_tid": vehicle_tid or False,
            "user_id": user.id if user else False,
            "user_code": user_code or False,
            "user_tid": user_tid or False,
            "vehicle_card_id": vehicle_card.id if vehicle_card else False,
            "user_card_id": user_card.id if user_card else False,
            "borrow_request_id": borrow_request.id if borrow_request else False,
            "borrower_id": user.id if borrow_request and user else False,
            "effective_direction": direction,
        }

        decision_errors = list(config_errors)
        if lane.required_vehicle_tid and not vehicle_tid:
            decision_errors.append(("missing_vehicle_tid", _("Vehicle RFID card/TID was not detected.")))
        elif vehicle_tid and not vehicle_card:
            decision_errors.append(("vehicle_card_unknown", _("Vehicle TID is not defined as an active Vehicle Card.")))
        if vehicle_tid and not vehicle:
            decision_errors.append(("vehicle_not_found", _("Vehicle could not be resolved from vehicle_code or vehicle_tid.")))
        if lane.required_user_tid and not user_tid:
            decision_errors.append(("missing_user_tid", _("User RFID card/TID was not detected.")))
        elif user_tid and not user_card:
            decision_errors.append(("user_card_unknown", _("User TID is not defined as an active User Card.")))
        elif user_tid and user_card and not user:
            decision_errors.append(("user_not_assigned", _("User TID is not assigned to an NSP User.")))
        if not borrow_ok:
            decision_errors.append(("borrow_not_allowed", borrow_error))
        if not continuity_ok:
            decision_errors.append((self._normalize_error_code(False, continuity_error) or "continuity_duplicate", continuity_error))

        if decision_errors:
            vals["status"] = "denied"
        self._apply_error_metadata(vals, decision_errors)
        vals["payload_hash"] = self._normalized_payload_hash(controller, vals)

        existing = self.search([("transaction_uid", "=", transaction_uid)], limit=1)
        if existing:
            if existing.payload_hash and existing.payload_hash != vals["payload_hash"]:
                raise ValidationError(_("sync_uid_conflict: transaction_uid already exists with different business data."))
            if not existing.payload_hash:
                existing.write({"payload_hash": vals["payload_hash"]})
            return existing, True
        try:
            with self.env.cr.savepoint():
                record = self.create(vals)
            return record, False
        except IntegrityError:
            existing = self.search([("transaction_uid", "=", transaction_uid)], limit=1)
            if not existing:
                raise
            if existing.payload_hash and existing.payload_hash != vals["payload_hash"]:
                raise ValidationError(_("sync_uid_conflict: transaction_uid already exists with different business data."))
            if not existing.payload_hash:
                existing.write({"payload_hash": vals["payload_hash"]})
            return existing, True

