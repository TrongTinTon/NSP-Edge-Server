# -*- coding: utf-8 -*-
import logging
import os
from datetime import timedelta

from psycopg2 import IntegrityError

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


_logger = logging.getLogger(__name__)


class ParkingDetectionEvent(models.Model):
    """Short-lived Edge RFID read used to build final parking transactions.

    Controller reports physical reads only. Edge resolves each Reader Port
    against the published Lane Reader Port Timeline, collapses repeated reads,
    and matches the resulting timeline against explicit Check-in/Check-out
    sequences. Raw detections remain on Edge and never synchronize to Cloud.
    """

    _name = "nsp.parking.detection.event"
    _description = "NSP Parking Detection Event"
    _rec_name = "event_uid"
    _order = "detected_at desc, id desc"
    _log_access = False

    event_uid = fields.Char(
        string="Detection UID", required=True, copy=False, readonly=True,
        help="Controller-generated idempotency key for one detected TID.",
    )
    detected_at = fields.Datetime(
        string="Detected At", required=True, index=True, readonly=True,
        help="UTC time reported by the Controller.",
    )
    lane_id = fields.Many2one(
        "nsp.parking.lane", string="Lane", required=True,
        ondelete="restrict", readonly=True, index=True,
    )
    reader_id = fields.Many2one(
        "nsp.device", string="Reader", required=True,
        ondelete="restrict", readonly=True, index=True,
    )
    port_no = fields.Integer(
        string="Port", required=True, readonly=True, index=True,
    )
    tid = fields.Char(
        string="RFID TID", required=True, readonly=True, index=True,
    )
    layout_revision = fields.Integer(
        string="Parking Layout Revision", default=0, readonly=True, index=True,
        help="Published Parking Layout revision used to resolve this detection.",
    )
    rssi_dbm = fields.Float(
        string="RSSI (dBm)", readonly=True,
        help="Optional signal strength reported by the Controller.",
    )
    user_id = fields.Many2one(
        "nsp.user", string="Resolved User", ondelete="restrict",
        readonly=True, index=True,
    )
    vehicle_id = fields.Many2one(
        "nsp.vehicle", string="Resolved Vehicle", ondelete="restrict",
        readonly=True, index=True,
    )
    state = fields.Selection(
        [
            ("pending", "Pending"),
            ("processed", "Processed"),
            ("error", "Error"),
        ],
        string="State", required=True, default="pending", copy=False, readonly=True,
    )
    error_code = fields.Selection(
        [
            ("layout_revision_superseded", "Layout Revision Superseded"),
            ("parking_area_not_operational", "Parking Area Not Operational"),
            ("sequence_timeout", "Sequence Timeout"),
            ("processing_error", "Processing Error"),
        ],
        string="Processing Error", readonly=True, copy=False, index=True,
    )
    error_message = fields.Text(string="Processing Message", readonly=True, copy=False)
    transaction_id = fields.Many2one(
        "nsp.parking.transaction", string="Parking Transaction",
        ondelete="set null", index=True, copy=False, readonly=True,
    )

    _sql_constraints = [
        ("event_uid_unique", "unique(event_uid)", "Detection UID must be unique."),
        (
            "parking_detection_port_range",
            "CHECK(port_no >= 1 AND port_no <= 16)",
            "Reader Port must be between 1 and 16.",
        ),
    ]

    def init(self):
        self.env.cr.execute(
            """
            DROP INDEX IF EXISTS nsp_parking_detection_pending_lane_idx;
            CREATE INDEX nsp_parking_detection_pending_lane_idx
                ON nsp_parking_detection_event (lane_id, layout_revision, detected_at, id)
             WHERE state = 'pending' AND transaction_id IS NULL
            """
        )
        self.env.cr.execute(
            """
            DROP INDEX IF EXISTS nsp_parking_detection_sequence_idx;
            CREATE INDEX nsp_parking_detection_sequence_idx
                ON nsp_parking_detection_event
                   (lane_id, layout_revision, tid, reader_id, port_no, detected_at, id)
             WHERE state = 'pending' AND transaction_id IS NULL
            """
        )
        self.env.cr.execute(
            """
            CREATE INDEX IF NOT EXISTS nsp_parking_detection_cleanup_idx
                ON nsp_parking_detection_event (detected_at)
             WHERE state IN ('processed', 'error')
            """
        )

    @api.model
    def _deployment_role(self):
        role = (
            self.env["ir.config_parameter"].sudo().get_param("nsp.deployment_role")
            or os.getenv("NSP_DEPLOYMENT_ROLE")
            or os.getenv("NSP_SERVER_ROLE")
            or "edge_server"
        ).strip().lower()
        return role if role in ("cloud", "edge_server") else "edge_server"

    @api.model
    def _ensure_edge_role(self):
        if self._deployment_role() != "edge_server":
            raise ValidationError(_("parking_detection_edge_only"))

    @api.model
    def _business_values(self, source):
        def value(name):
            if hasattr(source, "_fields"):
                field = source._fields.get(name)
                raw = source[name]
                return raw.id if field and field.type == "many2one" and raw else raw
            return source.get(name)

        detected_at = value("detected_at")
        if detected_at:
            detected_at = fields.Datetime.to_string(fields.Datetime.to_datetime(detected_at))
        return {
            "detected_at": detected_at or "",
            "lane_id": int(value("lane_id") or 0),
            "reader_id": int(value("reader_id") or 0),
            "port_no": int(value("port_no") or 0),
            "tid": str(value("tid") or "").strip(),
            "layout_revision": int(value("layout_revision") or 0),
            "rssi_dbm": float(value("rssi_dbm") or 0.0),
            "user_id": int(value("user_id") or 0),
            "vehicle_id": int(value("vehicle_id") or 0),
        }

    @api.model
    def create_idempotent(self, vals):
        uid = str(vals.get("event_uid") or "").strip()
        if not uid:
            raise ValidationError(_("missing_event_uid"))
        vals = dict(vals, event_uid=uid)
        existing = self.search([("event_uid", "=", uid)], limit=1)
        if existing:
            if self._business_values(existing) != self._business_values(vals):
                raise ValidationError(_(
                    "event_uid_conflict: Detection UID already exists with different data."
                ))
            return existing, True
        try:
            with self.env.cr.savepoint():
                return self.create(vals), False
        except IntegrityError:
            existing = self.search([("event_uid", "=", uid)], limit=1)
            if not existing:
                raise
            if self._business_values(existing) != self._business_values(vals):
                raise ValidationError(_(
                    "event_uid_conflict: Detection UID already exists with different data."
                ))
            return existing, True

    @api.model
    def _resolve_topology_batch(self, controller, detections):
        keys = {
            (
                str(payload.get("serial_number") or "").strip().upper(),
                int(payload.get("port_no") or 0),
            )
            for payload, _assignment in detections
        }
        keys.discard(("", 0))
        if not keys:
            return {}, {}

        serials = {serial for serial, _port in keys}
        devices = self.env["nsp.device"].sudo().search([
            ("controller_id", "=", controller.id),
            ("serial_number", "in", list(serials)),
            ("active", "=", True),
            ("cloud_removed", "=", False),
        ])
        device_by_serial = {
            str(device.serial_number or "").strip().upper(): device
            for device in devices
        }
        timeline_rows = self.env["nsp.parking.lane.timeline"].sudo().search([
            ("lane_id.active", "=", True),
            ("lane_id.parking_area_id.state", "=", "operational"),
            ("reader_id", "in", devices.ids),
            ("port_no", "in", list({port for _serial, port in keys})),
        ]) if devices else self.env["nsp.parking.lane.timeline"].browse()

        lanes_by_key = {}
        for row in timeline_rows:
            key = (
                str(row.reader_id.serial_number or "").strip().upper(),
                int(row.port_no or 0),
            )
            lanes_by_key.setdefault(key, set()).add(row.lane_id.id)

        resolved = {}
        errors = {}
        Lane = self.env["nsp.parking.lane"].sudo()
        for key in keys:
            serial, port_no = key
            device = device_by_serial.get(serial)
            if not device:
                errors[key] = "device_not_found"
                continue
            lane_ids = lanes_by_key.get(key, set())
            if not lane_ids:
                errors[key] = "no_reader_port_timeline"
                continue
            if len(lane_ids) != 1:
                errors[key] = "ambiguous_reader_port_lane"
                continue
            lane = Lane.browse(next(iter(lane_ids))).exists()
            if not lane or lane.controller_id != controller:
                errors[key] = "controller_not_in_scope"
                continue
            resolved[key] = (device, lane, port_no)
        return resolved, errors

    @api.model
    def _ingest_controller_detection(self, controller, payload, assignment, topology_cache):
        if not isinstance(payload, dict):
            raise ValidationError(_("invalid_payload"))

        event_uid = str(payload.get("event_uid") or "").strip()
        serial_number = str(payload.get("serial_number") or "").strip().upper()
        tid = self.env["nsp.rfid.runtime.assignment"]._normalize_tid(payload.get("tid"))
        try:
            port_no = int(payload.get("port_no") or 0)
        except Exception as exc:
            raise ValidationError(_("invalid_payload: port_no")) from exc
        try:
            rssi_dbm = float(payload.get("rssi_dbm") or 0.0)
        except (TypeError, ValueError):
            rssi_dbm = 0.0
        try:
            detected_at = fields.Datetime.to_string(
                fields.Datetime.to_datetime(payload.get("detected_at"))
            )
        except Exception:
            detected_at = False

        if not event_uid:
            raise ValidationError(_("missing_event_uid"))
        if not serial_number:
            raise ValidationError(_("serial_number is required"))
        if port_no < 1 or port_no > 16:
            raise ValidationError(_("port_no must be between 1 and 16"))
        if not detected_at:
            raise ValidationError(_("detected_at is required"))
        if not tid:
            raise ValidationError(_("tid is required"))
        if not assignment or assignment.tid != tid:
            raise ValidationError(_("rfid_tag_not_actively_assigned"))
        if bool(assignment.user_id) == bool(assignment.vehicle_id):
            raise ValidationError(_("invalid_rfid_assignment"))
        target = assignment.user_id or assignment.vehicle_id
        if not target.active:
            raise ValidationError(_("rfid_assignment_target_inactive"))

        topology = topology_cache.get((serial_number, port_no))
        if topology is None:
            raise ValidationError(_("no_reader_port_timeline"))
        reader, lane, port_no = topology
        parking_area = lane.parking_area_id
        self._acquire_parking_area_runtime_lock(parking_area, shared=True)
        parking_area.invalidate_recordset(["state", "published_revision"])
        layout_revision = int(parking_area.published_revision or 0)
        if parking_area.state != "operational" or layout_revision <= 0:
            raise ValidationError(_("parking_area_not_operational"))

        vals = {
            "event_uid": event_uid,
            "detected_at": detected_at,
            "lane_id": lane.id,
            "reader_id": reader.id,
            "port_no": port_no,
            "tid": tid,
            "layout_revision": layout_revision,
            "rssi_dbm": rssi_dbm,
            "user_id": assignment.user_id.id if assignment.user_id else False,
            "vehicle_id": assignment.vehicle_id.id if assignment.vehicle_id else False,
            "state": "pending",
        }
        record, duplicate = self.create_idempotent(vals)
        return record, duplicate, lane

    @api.model
    def ingest_controller_detections(self, controller, detections):
        """Persist one Controller batch, then process each touched Lane once."""
        self._ensure_edge_role()
        if not isinstance(detections, list):
            raise ValidationError(_("invalid_payload"))

        topology_cache, topology_errors = self._resolve_topology_batch(controller, detections)
        touched_lanes = self.env["nsp.parking.lane"].browse()
        for payload, assignment in detections:
            try:
                port_no = int(payload.get("port_no") or 0)
            except Exception:
                port_no = 0
            topology_key = (
                str(payload.get("serial_number") or "").strip().upper(),
                port_no,
            )
            topology_error = topology_errors.get(topology_key)
            if topology_error:
                _logger.warning(
                    "Parking detection ignored: controller=%s serial=%s port=%s tid=%s reason=%s",
                    controller.controller_id, topology_key[0], topology_key[1],
                    payload.get("tid"), topology_error,
                )
                continue
            try:
                with self.env.cr.savepoint():
                    _record, duplicate, lane = self._ingest_controller_detection(
                        controller, payload, assignment, topology_cache
                    )
                if not duplicate:
                    touched_lanes |= lane
            except ValidationError as exc:
                _logger.warning(
                    "Parking detection rejected at Edge: controller=%s event_uid=%s "
                    "serial=%s port=%s tid=%s reason=%s",
                    controller.controller_id,
                    payload.get("event_uid"),
                    payload.get("serial_number"),
                    payload.get("port_no"),
                    payload.get("tid"),
                    exc,
                )

        ordered_lane_ids = self._pending_lane_ids_in_event_order(touched_lanes.ids)
        for lane in self.env["nsp.parking.lane"].sudo().browse(ordered_lane_ids).exists():
            # Cross-request movement is normal. Ingestion never expires incomplete
            # sequences; finalization belongs to the periodic pending-event job.
            self._process_pending_for_lane(lane, finalize_expired=False)
        return True

    @api.model
    def _acquire_parking_area_runtime_lock(self, parking_area, shared=True):
        """Serialize runtime snapshot changes against detection decisions.

        Shared locks allow different Lanes in one Parking Area to process in
        parallel. Parking Layout snapshot application takes the exclusive form,
        so no detection can be accepted or classified across a revision change.
        """
        parking_area = parking_area.exists()
        if not parking_area:
            return False
        statement = (
            "SELECT pg_advisory_xact_lock_shared(hashtext(%s))"
            if shared
            else "SELECT pg_advisory_xact_lock(hashtext(%s))"
        )
        self.env.cr.execute(statement, (f"nsp.parking:area:{parking_area.id}",))
        return True

    @api.model
    def invalidate_pending_for_runtime_change(self, parking_area, incoming_revision, incoming_state):
        """Close pending detections that cannot be evaluated by the incoming runtime snapshot."""
        parking_area = parking_area.exists()
        if not parking_area:
            return 0
        try:
            revision = int(incoming_revision or 0)
        except (TypeError, ValueError):
            revision = 0
        domain = [
            ("lane_id.parking_area_id", "=", parking_area.id),
            ("state", "=", "pending"),
            ("transaction_id", "=", False),
        ]
        pending = self.sudo().search(domain)
        if not pending:
            return 0
        if incoming_state != "operational":
            values = {
                "state": "error",
                "error_code": "parking_area_not_operational",
                "error_message": _("Parking Area runtime is not operational."),
            }
            affected = pending
        else:
            affected = pending.filtered(lambda event: int(event.layout_revision or 0) != revision)
            values = {
                "state": "error",
                "error_code": "layout_revision_superseded",
                "error_message": _("Detection belongs to a superseded Parking Layout revision."),
            }
        if affected:
            affected.write(values)
        return len(affected)

    @api.model
    def _pending_lane_ids_in_event_order(self, lane_ids=False):
        """Return Lanes ordered by their earliest unconsumed detection.

        Lane iteration order must not decide Vehicle continuity. Processing the
        earliest pending physical reads first significantly reduces cross-Lane
        reordering when Check-in and Check-out arrive in the same backlog.
        """
        params = []
        lane_filter = ""
        normalized_ids = [int(value) for value in (lane_ids or []) if int(value) > 0]
        if normalized_ids:
            lane_filter = " AND lane_id = ANY(%s)"
            params.append(normalized_ids)
        self.env.cr.execute(
            f"""
            SELECT lane_id
              FROM nsp_parking_detection_event
             WHERE state = 'pending'
               AND transaction_id IS NULL
               {lane_filter}
             GROUP BY lane_id
             ORDER BY MIN(detected_at) ASC, MIN(id) ASC, lane_id ASC
            """,
            params,
        )
        return [row[0] for row in self.env.cr.fetchall()]

    @api.model
    def _pending_user_pool(self, lane):
        events = self.search([
            ("lane_id", "=", lane.id),
            ("state", "=", "pending"),
            ("transaction_id", "=", False),
            ("layout_revision", "=", int(lane.parking_area_id.published_revision or 0)),
            ("user_id", "!=", False),
        ], order="detected_at asc, id asc")
        return events

    @api.model
    def _authorized_user_ids(self, vehicle, event_time):
        """Return owner and active borrowers authorized at the movement time."""
        if not vehicle or not vehicle.active:
            return set()
        user_ids = set()
        if vehicle.owner_id and vehicle.owner_id.active:
            user_ids.add(vehicle.owner_id.id)
        borrows = self.env["nsp.vehicle.borrow"].sudo().search([
            ("vehicle_id", "=", vehicle.id),
            ("state", "=", "active"),
            ("returned_at", "=", False),
            ("valid_from", "<=", event_time),
            ("valid_to", ">=", event_time),
            ("borrower_id.active", "=", True),
        ])
        user_ids.update(borrows.mapped("borrower_id").ids)
        return user_ids

    @api.model
    def _user_candidates_from_pool(
        self, user_events, anchor_at, window_seconds, consumed_ids
    ):
        """Return all unused User reads inside the configured movement window.

        Repeated physical reads for one User remain one identity. Different User
        identities in the same window produce an explicit denied transaction.
        """
        if not user_events:
            return self.browse()
        window = max(0.001, float(window_seconds or 0.0))
        return user_events.filtered(
            lambda event: (
                event.id not in consumed_ids
                and event.state == "pending"
                and not event.transaction_id
                and abs((event.detected_at - anchor_at).total_seconds()) <= window
            )
        ).sorted(key=lambda event: (
            abs((event.detected_at - anchor_at).total_seconds()),
            event.detected_at,
            event.id,
        ))

    @api.model
    def _lane_max_duration(self, lane):
        return lane.max_sequence_window()

    @api.model
    def _expire_orphan_user_events(self, lane, now):
        cutoff = now - timedelta(seconds=self._lane_max_duration(lane))
        stale = self.search([
            ("lane_id", "=", lane.id),
            ("state", "=", "pending"),
            ("transaction_id", "=", False),
            ("layout_revision", "=", int(lane.parking_area_id.published_revision or 0)),
            ("user_id", "!=", False),
            ("detected_at", "<", cutoff),
        ])
        if stale:
            stale.write({
                "state": "error",
                "error_code": "sequence_timeout",
                "error_message": _("User RFID detection expired without a matching vehicle movement."),
            })


    @api.model
    def _build_vehicle_sequence_matches(self, lane):
        if not lane.event_sequence_ids or not lane.timeline_line_ids:
            return []
        vehicle_events = self.search([
            ("lane_id", "=", lane.id),
            ("state", "=", "pending"),
            ("transaction_id", "=", False),
            ("layout_revision", "=", int(lane.parking_area_id.published_revision or 0)),
            ("vehicle_id", "!=", False),
        ], order="tid asc, detected_at asc, id asc")
        if not vehicle_events:
            return []

        timeline = lane.timeline_line_ids.sorted("sequence")
        allowed_by_pair = {}
        timeline_keys = [
            (row.reader_id.id, int(row.port_no or 0))
            for row in timeline
        ]
        for index in range(1, len(timeline)):
            source = timeline_keys[index - 1]
            target = timeline_keys[index]
            allowed_by_pair[frozenset((source, target))] = max(
                0.001, lane.allowed_duration_for_step(timeline[index].sequence)
            )

        sequence_specs = []
        for event_type in ("check_in", "check_out"):
            rows = lane.event_sequence_ids.filtered(
                lambda row: row.sequence_type == event_type
            ).sorted("sequence")
            if rows:
                sequence_specs.append((
                    event_type,
                    [
                        (row.reader_id.id, int(row.port_no or 0))
                        for row in rows
                    ],
                ))

        events_by_tid = {}
        for event in vehicle_events:
            events_by_tid.setdefault(event.tid, []).append(event)

        matches = []
        for tid, raw_events in events_by_tid.items():
            collapsed = []
            for event in raw_events:
                key = (event.reader_id.id, int(event.port_no or 0))
                if collapsed:
                    previous = (
                        collapsed[-1].reader_id.id,
                        int(collapsed[-1].port_no or 0),
                    )
                    if previous == key:
                        continue
                collapsed.append(event)
            for event_type, expected_keys in sequence_specs:
                length = len(expected_keys)
                if length < 2 or len(collapsed) < length:
                    continue
                for offset in range(0, len(collapsed) - length + 1):
                    window = collapsed[offset:offset + length]
                    actual_keys = [
                        (event.reader_id.id, int(event.port_no or 0))
                        for event in window
                    ]
                    if actual_keys != expected_keys:
                        continue
                    total_allowed = 0.0
                    valid = True
                    for index in range(1, length):
                        allowed = allowed_by_pair.get(
                            frozenset((actual_keys[index - 1], actual_keys[index]))
                        )
                        gap = (
                            window[index].detected_at
                            - window[index - 1].detected_at
                        ).total_seconds()
                        if allowed is None or gap < 0 or gap > allowed:
                            valid = False
                            break
                        total_allowed += allowed
                    if not valid:
                        continue
                    matches.append({
                        "tid": tid,
                        "event_type": event_type,
                        "duration_seconds": max(0.001, total_allowed),
                        "start_at": window[0].detected_at,
                        "end_at": window[-1].detected_at,
                        "events": self.browse([event.id for event in window]),
                    })
        matches.sort(
            key=lambda item: (item["end_at"], item["start_at"], item["tid"])
        )
        return matches

    @api.model
    def _expire_stale_vehicle_events(self, lane, now):
        cutoff = now - timedelta(seconds=self._lane_max_duration(lane))
        stale = self.search([
            ("lane_id", "=", lane.id),
            ("state", "=", "pending"),
            ("transaction_id", "=", False),
            ("layout_revision", "=", int(lane.parking_area_id.published_revision or 0)),
            ("vehicle_id", "!=", False),
            ("detected_at", "<", cutoff),
        ])
        if stale:
            stale.write({
                "state": "error",
                "error_code": "sequence_timeout",
                "error_message": _("Vehicle RFID detections expired before a complete movement sequence was matched."),
            })

    def _create_transaction_for_vehicle(
        self,
        vehicle_events,
        event_type,
        user_events=False,
        observed_duration_seconds=False,
        allowed_duration_seconds=False,
    ):
        supporting_users = user_events or self.browse()
        group = vehicle_events | supporting_users if event_type == "check_out" else vehicle_events
        transaction = self.env["nsp.parking.transaction"].sudo().create_from_detection_group(
            group,
            resolved_event_type=event_type,
            observed_duration_seconds=observed_duration_seconds,
            allowed_duration_seconds=allowed_duration_seconds,
        )
        # Repeated Check-in/Check-out movements are intentionally consumed with
        # no business transaction. They are RFID acquisition noise, not a denied
        # parking decision.
        group.write({
            "state": "processed",
            "transaction_id": transaction.id if transaction else False,
        })
        return transaction

    @api.model
    def _process_sequence_matches(self, lane, now, finalize_expired=True):
        transactions = self.env["nsp.parking.transaction"].browse()
        matches = self._build_vehicle_sequence_matches(lane)
        if not matches:
            if finalize_expired:
                self._expire_stale_vehicle_events(lane, now)
                self._expire_orphan_user_events(lane, now)
            return transactions

        user_events = self._pending_user_pool(lane)
        vehicle_events = self.browse([
            event.id
            for match in matches
            for event in match["events"]
        ])
        consumed_user_ids = set()
        blocked_tids = set()

        for match in matches:
            tid = match["tid"]
            movement_events = match["events"].filtered(
                lambda rec: rec.state == "pending" and not rec.transaction_id
            )
            if (
                not movement_events
                or len(movement_events) != len(match["events"])
                or tid in blocked_tids
            ):
                continue

            event_type = match["event_type"]
            duration = match["duration_seconds"]

            # Use the calibrated sequence window as the physical debounce window too.
            # This prevents lingering reads from creating duplicate business transactions
            # without reintroducing a global fixed suppression value.
            vehicle_event = movement_events.filtered(
                lambda rec: bool(rec.vehicle_id)
            ).sorted(key=lambda rec: (rec.detected_at, rec.id))[-1:]
            vehicle_tid = vehicle_event.tid if vehicle_event else False
            recent = self.env["nsp.parking.transaction"].sudo().search([
                ("lane_id", "=", lane.id),
                ("layout_revision", "=", int(lane.parking_area_id.published_revision or 0)),
                ("event_type", "=", event_type),
                ("vehicle_tid", "=", vehicle_tid),
                ("event_time", ">=", match["end_at"] - timedelta(seconds=duration)),
                ("event_time", "<=", match["end_at"]),
            ], order="event_time desc, id desc", limit=1) if vehicle_tid else self.env["nsp.parking.transaction"].browse()
            if recent:
                movement_events.write({"state": "processed", "transaction_id": recent.id})
                continue

            vehicle = movement_events.mapped("vehicle_id")[:1]
            Transaction = self.env["nsp.parking.transaction"].sudo()
            continuity_action = continuity_code = False
            if vehicle:
                Transaction._acquire_vehicle_continuity_lock(vehicle)
                continuity_action, continuity_code, _continuity_message = (
                    Transaction._vehicle_continuity_decision(
                        vehicle,
                        event_type,
                        match["end_at"],
                        lane.parking_area_id,
                    )
                )

            matched_user_events = self.browse()
            if event_type == "check_out":
                deadline = match["end_at"] + timedelta(seconds=duration)
                deadline_reached = bool(finalize_expired and now >= deadline)
                if (
                    continuity_action == "ignore"
                    and continuity_code == "check_out_without_check_in"
                    and not deadline_reached
                ):
                    # Allow a short, configuration-bound reconciliation window
                    # for an earlier Check-in arriving from another Lane/request.
                    blocked_tids.add(tid)
                    continue

            if continuity_action == "ignore":
                # Invalid sequence data is consumed at the detection layer only.
                # It must not create a Parking Transaction or a Live Monitor event.
                movement_events.write({"state": "processed", "transaction_id": False})
                continue

            if event_type == "check_out":
                candidates = self._user_candidates_from_pool(
                    user_events,
                    match["end_at"],
                    duration,
                    consumed_user_ids,
                )
                candidate_users = candidates.mapped("user_id")
                authorized_user_ids = self._authorized_user_ids(vehicle, match["end_at"])
                has_one_authorized_identity = (
                    len(candidate_users) == 1
                    and candidate_users.id in authorized_user_ids
                )
                has_ambiguous_identities = len(candidate_users) > 1
                if not (
                    has_one_authorized_identity
                    or has_ambiguous_identities
                    or deadline_reached
                ):
                    # Wait for an owner/active borrower until the calibrated
                    # sequence window expires. Multiple identities are denied
                    # immediately because later reads cannot remove ambiguity.
                    blocked_tids.add(tid)
                    continue
                matched_user_events = candidates

            try:
                with self.env.cr.savepoint():
                    transaction = self._create_transaction_for_vehicle(
                        movement_events,
                        event_type,
                        user_events=matched_user_events,
                        observed_duration_seconds=max(
                            0.0,
                            (match["end_at"] - match["start_at"]).total_seconds(),
                        ),
                        allowed_duration_seconds=duration,
                    )
                    transactions |= transaction
                    consumed_user_ids.update(matched_user_events.ids)
            except Exception:
                _logger.exception(
                    "Parking sequence processing failed: lane=%s event_type=%s ids=%s",
                    lane.id, event_type, movement_events.ids,
                )
                message = _("Parking transaction processing failed. See Edge logs for details.")
                movement_events.write({
                    "state": "error",
                    "error_code": "processing_error",
                    "error_message": message,
                })
                if matched_user_events:
                    matched_user_events.write({
                        "state": "error",
                        "error_code": "processing_error",
                        "error_message": message,
                    })
                    consumed_user_ids.update(matched_user_events.ids)

        if finalize_expired:
            self._expire_stale_vehicle_events(lane, now)
            self._expire_orphan_user_events(lane, now)
        return transactions

    @api.model
    def _process_pending_for_lane(self, lane, now=None, finalize_expired=True):
        area = lane.parking_area_id
        self._acquire_parking_area_runtime_lock(area, shared=True)
        self.env.cr.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            (f"nsp.parking:lane:{lane.id}",),
        )
        area.invalidate_recordset(["state", "published_revision"])
        revision = int(area.published_revision or 0)
        if area.state != "operational" or revision <= 0:
            self.invalidate_pending_for_runtime_change(area, revision, area.state)
            return self.env["nsp.parking.transaction"].browse()
        self.invalidate_pending_for_runtime_change(area, revision, "operational")
        now = fields.Datetime.to_datetime(now or fields.Datetime.now())
        return self._process_sequence_matches(lane, now, finalize_expired=finalize_expired)

    @api.model
    def process_pending_events(self):
        if self._deployment_role() != "edge_server":
            return True
        now = fields.Datetime.now()
        lane_ids = self._pending_lane_ids_in_event_order()
        Lane = self.env["nsp.parking.lane"].sudo()
        for lane in Lane.browse(lane_ids).exists():
            self._process_pending_for_lane(lane, now=now)
        return True

    @api.model
    def cleanup_old_events(self):
        if self._deployment_role() != "edge_server":
            return True
        raw_days = self.env["ir.config_parameter"].sudo().get_param(
            "nsp.parking_detection_retention_days", "7"
        )
        try:
            retention_days = max(1, int(raw_days))
        except Exception:
            retention_days = 7
        terminal_cutoff = fields.Datetime.now() - timedelta(days=retention_days)
        old = self.search([
            ("state", "in", ["processed", "error"]),
            ("detected_at", "<", terminal_cutoff),
        ], order="detected_at asc, id asc", limit=20000)
        if old:
            old.unlink()
        return True
