# -*- coding: utf-8 -*-
from uuid import NAMESPACE_URL, uuid5

from psycopg2 import IntegrityError

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class ParkingLogBusiness(models.Model):
    """Business state/decision behavior for the append-only Parking Log model."""

    _inherit = "nsp.parking.log"

    @api.model
    def _latest_allowed_log(self, vehicle):
        if not vehicle:
            return self.browse()
        return self.search([
            ("vehicle_id", "=", vehicle.id),
            ("decision", "=", "allowed"),
        ], order="event_time desc, id desc", limit=1)

    @api.model
    def _resolve_vehicle_movement(self, vehicle, event_time, parking_area=False):
        """Resolve movement once from the latest allowed Vehicle state.

        This method must be called while the Vehicle advisory transaction lock is
        held. It deliberately uses one indexed lookup. Denied logs never establish
        presence. Late historical detections are ignored instead of rewriting the
        already-established current state.
        """
        latest = self._latest_allowed_log(vehicle)
        if latest and event_time and latest.event_time and latest.event_time > event_time:
            return {
                "action": "ignore",
                "event_type": False,
                "reason_code": "stale_movement",
                "previous_log": latest,
            }

        event_type = "check_out" if latest and latest.event_type == "check_in" else "check_in"
        if event_type == "check_out" and parking_area and latest:
            previous_area = latest.parking_area_id
            if previous_area and previous_area != parking_area:
                return {
                    "action": "deny",
                    "event_type": event_type,
                    "reason_code": "vehicle_checked_in_other_area",
                    "previous_log": latest,
                }

        return {
            "action": "allow",
            "event_type": event_type,
            "reason_code": False,
            "previous_log": latest,
        }

    @api.model
    def _authorized_user_borrow_map(self, vehicle, event_time):
        """Return authorized User IDs mapped to the Borrow record, if any.

        Owner maps to an empty Borrow record. Active borrowers are loaded in one
        query and reused both for candidate readiness and final audit linkage.
        """
        Borrow = self.env["nsp.vehicle.borrow"].sudo()
        if not vehicle or not vehicle.active:
            return {}
        authorized = {}
        if vehicle.owner_id and vehicle.owner_id.active:
            authorized[vehicle.owner_id.id] = Borrow.browse()
        borrows = Borrow.search([
            ("vehicle_id", "=", vehicle.id),
            ("state", "=", "active"),
            ("returned_at", "=", False),
            ("valid_from", "<=", event_time),
            ("valid_to", ">=", event_time),
            ("borrower_id.active", "=", True),
        ])
        for borrow in borrows:
            if borrow.borrower_id:
                authorized[borrow.borrower_id.id] = borrow
        return authorized

    @api.model
    def _log_uid_for_group(self, layout_lane, event_type, detections):
        event_uids = sorted({str(uid) for uid in detections.mapped("event_uid") if uid})
        if not event_uids:
            event_uids = ["id:%s" % event_id for event_id in sorted(detections.ids)]
        area_code = str(layout_lane.parking_area_id.code or layout_lane.parking_area_id.id)
        lane_code = str(layout_lane.lane_id.code or layout_lane.lane_id.id)
        revision = int(layout_lane.parking_area_id.published_revision or 0)
        canonical = "|".join([
            "nsp.parking.log", area_code, lane_code, str(revision), event_type,
            *event_uids,
        ])
        return str(uuid5(NAMESPACE_URL, canonical))

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
            "parking_area_id": int(value("parking_area_id") or 0),
            "layout_lane_id": int(value("layout_lane_id") or 0),
            "lane_id": int(value("lane_id") or 0),
            "layout_revision": int(value("layout_revision") or 0),
            "event_time": event_time or "",
            "event_type": value("event_type") or "",
            "decision": value("decision") or "",
            "vehicle_id": int(value("vehicle_id") or 0),
            "vehicle_tid": str(value("vehicle_tid") or "").strip(),
            "user_id": int(value("user_id") or 0),
            "user_tid": str(value("user_tid") or "").strip(),
            "borrow_id": int(value("borrow_id") or 0),
            "reason_code": value("reason_code") or "",
        }

    @api.model
    def _create_idempotent(self, vals):
        uid = str(vals.get("log_uid") or "").strip()
        if not uid:
            raise ValidationError(_("missing_parking_log_uid"))
        vals = dict(vals, log_uid=uid)
        try:
            with self.env.cr.savepoint():
                return self.create(vals), False
        except IntegrityError:
            existing = self.search([("log_uid", "=", uid)], limit=1)
            if not existing:
                raise
            if self._business_values(existing) != self._business_values(vals):
                raise ValidationError(_(
                    "parking_log_uid_conflict: Log UID already exists with different business data."
                ))
            return existing, True

    @api.model
    def create_from_detection_group(
        self,
        detections,
        movement_state=False,
        authorized_borrow_map=False,
    ):
        """Create one business Parking Log from a matched Lane detection group."""
        detections = detections.filtered(lambda rec: not rec.error_code)
        if not detections:
            raise ValidationError(_("empty_detection_group"))

        layout_lane = detections[:1].layout_lane_id
        if not layout_lane:
            raise ValidationError(_("missing_lane_configuration"))
        lane = layout_lane.lane_id
        if any(rec.layout_lane_id != layout_lane for rec in detections):
            raise ValidationError(_("mixed_detection_group"))

        vehicle_events = detections.filtered(lambda rec: bool(rec.vehicle_id))
        if not vehicle_events:
            raise ValidationError(_("vehicle_identity_required_for_parking_log"))
        vehicles = vehicle_events.mapped("vehicle_id")
        if len(vehicles) != 1:
            raise ValidationError(_("mixed_vehicle_detection_group"))

        ordered_vehicle = vehicle_events.sorted(key=lambda rec: (rec.detected_at, rec.id))
        vehicle_event = ordered_vehicle[-1]
        vehicle = vehicle_event.vehicle_id
        event_time = vehicle_event.detected_at
        parking_area = layout_lane.parking_area_id
        layout_revision = int(parking_area.published_revision or 0)
        if layout_revision <= 0 or set(detections.mapped("layout_revision")) != {layout_revision}:
            raise ValidationError(_("parking_layout_revision_mismatch"))

        state = movement_state or False
        if not state:
            self._acquire_vehicle_continuity_lock(vehicle)
            state = self._resolve_vehicle_movement(vehicle, event_time, parking_area)
        if state.get("action") == "ignore":
            return self.browse()
        event_type = state.get("event_type")
        if event_type not in ("check_in", "check_out"):
            raise ValidationError(_("unresolved_parking_event_type"))

        reason_codes = []
        if parking_area.state != "operational":
            reason_codes.append("parking_area_not_operational")
        if state.get("action") == "deny":
            reason_codes.append(state.get("reason_code") or "unknown")

        user = self.env["nsp.user"].browse()
        user_tid = False
        borrow = self.env["nsp.vehicle.borrow"].browse()
        # A continuity/configuration denial is already final. Do not spend another
        # query/window waiting for User authorization that cannot change the outcome.
        final_context_denial = bool(reason_codes)
        if event_type == "check_out" and not final_context_denial:
            user_events = detections.filtered(lambda rec: bool(rec.user_id))
            unique_users = user_events.mapped("user_id")
            unique_tids = {event.tid for event in user_events if event.tid}
            if len(unique_users) > 1 or len(unique_tids) > 1:
                reason_codes.append("multiple_user_tags")
            elif not unique_users:
                reason_codes.append("missing_user_tid")
            else:
                user = unique_users[:1]
                user_event = user_events.sorted(key=lambda rec: (rec.detected_at, rec.id))[-1:]
                user_tid = user_event.tid
                authorized = authorized_borrow_map
                if authorized is False:
                    authorized = self._authorized_user_borrow_map(vehicle, event_time)
                if user.id not in authorized:
                    reason_codes.append("unauthorized_vehicle_user")
                else:
                    borrow = authorized[user.id]

        reason_code = reason_codes[0] if reason_codes else False
        vals = {
            "log_uid": self._log_uid_for_group(layout_lane, event_type, detections),
            "event_time": event_time,
            "event_type": event_type,
            "decision": "denied" if reason_codes else "allowed",
            "reason_code": reason_code,
            "parking_area_id": parking_area.id,
            "layout_lane_id": layout_lane.id,
            "lane_id": lane.id,
            "layout_revision": layout_revision,
            "vehicle_id": vehicle.id,
            "vehicle_tid": vehicle_event.tid or False,
            "user_id": user.id if user else False,
            "user_tid": user_tid or False,
            "borrow_id": borrow.id if borrow else False,
        }
        log, _duplicate = self._create_idempotent(vals)
        return log

    @api.model
    def _acquire_vehicle_continuity_lock(self, vehicle):
        vehicle = vehicle.exists()
        if not vehicle:
            return False
        self.env.cr.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            (f"nsp.parking:vehicle:{vehicle.id}",),
        )
        return True
