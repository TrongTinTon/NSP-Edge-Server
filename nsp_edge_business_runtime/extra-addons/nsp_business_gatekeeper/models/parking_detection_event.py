# -*- coding: utf-8 -*-
import logging
import os
from datetime import timedelta

from psycopg2 import IntegrityError

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

from ..services.antenna_sequence_matcher import match_ordered_sequence_details


_logger = logging.getLogger(__name__)


class ParkingDetectionEvent(models.Model):
    """Short-lived Edge RFID read used to build final Parking Logs.

    Controller reports physical reads only. A physical Reader/Antenna may belong
    to multiple logical Lanes. Edge therefore fans one physical detection out to
    every candidate Lane and resolves the logical Lane only after a complete
    ordered Antenna Sequence matches. Raw detections remain on Edge and never
    synchronize to Cloud.
    """

    _name = "nsp.parking.detection.event"
    _description = "NSP Detection Log"
    _rec_name = "event_uid"
    _order = "detected_at desc, id desc"
    _log_access = False

    event_uid = fields.Char(
        string="Detection UID", required=True, copy=False, readonly=True,
        help="Controller-generated idempotency key for one detected TID.",
    )
    detected_at = fields.Datetime(
        string="Detected At", required=True, readonly=True,
        help="UTC time reported by the Controller.",
    )
    controller_id = fields.Many2one(
        "nsp.controller", string="Controller",
        ondelete="restrict", readonly=True,
        help="Stored only for unresolved raw observations; resolved candidates derive scope from Lane Configuration.",
    )
    serial_number = fields.Char(
        string="Reader Serial", readonly=True,
        help="Stored only when raw Reader identity cannot be represented by the resolved Reader relation.",
    )
    layout_lane_id = fields.Many2one(
        "nsp.parking.layout.lane", string="Lane Configuration",
        ondelete="restrict", readonly=True,
        help="Resolved contextual Lane candidate. Empty when raw detection cannot be mapped to runtime topology.",
    )
    reader_id = fields.Many2one(
        "nsp.device", string="Reader",
        ondelete="restrict", readonly=True,
        help="Resolved Reader Master identity. Empty when the reported serial is unknown to Edge.",
    )
    port_no = fields.Integer(
        string="Port", required=True, readonly=True,
    )
    tid = fields.Char(
        string="RFID TID", required=True, readonly=True,
    )
    layout_revision = fields.Integer(
        string="Parking Layout Revision", default=0, readonly=True,
        help="Published Parking Layout revision used to resolve this detection.",
    )
    user_id = fields.Many2one(
        "nsp.user", string="Resolved User", ondelete="restrict",
        readonly=True,
    )
    vehicle_id = fields.Many2one(
        "nsp.vehicle", string="Resolved Vehicle", ondelete="restrict",
        readonly=True,
    )
    error_code = fields.Selection(
        [
            ("rfid_assignment_not_found", "RFID Assignment Not Found"),
            ("invalid_rfid_assignment", "Invalid RFID Assignment"),
            ("rfid_assignment_target_inactive", "RFID Assignment Target Inactive"),
            ("device_not_found", "Reader Not Found"),
            ("no_reader_port_timeline", "No Reader/Port Timeline"),
            ("controller_not_in_scope", "Controller Not In Scope"),
            ("ambiguous_reader_port_layout", "Ambiguous Reader/Port Layout"),
            ("parking_area_not_operational", "Parking Area Not Operational"),
            ("vehicle_not_found", "Vehicle Not Found"),
            ("processing_error", "Processing Error"),
        ],
        string="Processing Result", readonly=True, copy=False,
    )
    _sql_constraints = [
        (
            "parking_detection_port_range",
            "CHECK(port_no >= 1 AND port_no <= 16)",
            "Reader Port must be between 1 and 16.",
        ),
    ]

    def init(self):
        # Detection is a high-write working buffer, not long-term history. Keep
        # only partial indexes that match the pending matcher and short-lived
        # error retention queries. Successful rows are deleted after consumption.
        self.env.cr.execute(
            """
            CREATE INDEX IF NOT EXISTS nsp_parking_detection_pending_lane_idx
                ON nsp_parking_detection_event (layout_lane_id, detected_at, id)
             WHERE error_code IS NULL AND layout_lane_id IS NOT NULL;

            CREATE INDEX IF NOT EXISTS nsp_parking_detection_pending_vehicle_idx
                ON nsp_parking_detection_event
                   (layout_lane_id, layout_revision, tid, detected_at, id)
             WHERE error_code IS NULL AND vehicle_id IS NOT NULL;

            CREATE INDEX IF NOT EXISTS nsp_parking_detection_pending_user_idx
                ON nsp_parking_detection_event
                   (layout_lane_id, layout_revision, detected_at, id)
             WHERE error_code IS NULL AND user_id IS NOT NULL;

            CREATE INDEX IF NOT EXISTS nsp_parking_detection_cleanup_idx
                ON nsp_parking_detection_event (detected_at, id)
             WHERE error_code IS NOT NULL;

            CREATE UNIQUE INDEX IF NOT EXISTS nsp_parking_detection_resolved_uid_lane_unique
                ON nsp_parking_detection_event (event_uid, layout_lane_id)
             WHERE layout_lane_id IS NOT NULL;

            CREATE UNIQUE INDEX IF NOT EXISTS nsp_parking_detection_unresolved_uid_unique
                ON nsp_parking_detection_event (event_uid)
             WHERE layout_lane_id IS NULL;
            """
        )

        # Drop legacy standalone indexes. They add write amplification without
        # serving a runtime query covered by the partial/composite indexes above.
        for index_name in (
            "nsp_parking_detection_event_detected_at_index",
            "nsp_parking_detection_event_controller_id_index",
            "nsp_parking_detection_event_serial_number_index",
            "nsp_parking_detection_event_layout_lane_id_index",
            "nsp_parking_detection_event_lane_id_index",
            "nsp_parking_detection_event_reader_id_index",
            "nsp_parking_detection_event_port_no_index",
            "nsp_parking_detection_event_tid_index",
            "nsp_parking_detection_event_layout_revision_index",
            "nsp_parking_detection_event_user_id_index",
            "nsp_parking_detection_event_vehicle_id_index",
            "nsp_parking_detection_event_error_code_index",
            "nsp_parking_detection_event_parking_log_id_index",
            "nsp_parking_detection_parking_log_idx",
        ):
            self.env.cr.execute('DROP INDEX IF EXISTS "%s"' % index_name)

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
        # Compare only the minimal immutable receipt/candidate identity. User/Vehicle
        # resolution is deliberately excluded because assignment state can change; a
        # transport retry must still be idempotent for the same physical observation.
        reader_id = int(value("reader_id") or 0)
        layout_lane_id = int(value("layout_lane_id") or 0)
        return {
            "detected_at": detected_at or "",
            "layout_lane_id": layout_lane_id,
            "reader_id": reader_id,
            "port_no": int(value("port_no") or 0),
            "tid": str(value("tid") or "").strip(),
            # Raw identity fields are needed only when contextual resolution failed.
            "controller_id": 0 if layout_lane_id else int(value("controller_id") or 0),
            "serial_number": "" if reader_id else str(value("serial_number") or "").strip().upper(),
        }

    @api.model
    def create_idempotent(self, vals):
        """Create one candidate or unresolved raw Detection Log idempotently.

        Resolved physical detections can fan out to several contextual Lanes, so
        their idempotency scope is (event_uid, layout_lane_id). An unresolved raw
        detection has no Lane yet and is unique by event_uid alone.
        """
        uid = str(vals.get("event_uid") or "").strip()
        if not uid:
            raise ValidationError(_("missing_event_uid"))
        vals = dict(vals, event_uid=uid)
        layout_lane_id = int(vals.get("layout_lane_id") or 0)
        domain = [("event_uid", "=", uid)]
        if layout_lane_id:
            domain.append(("layout_lane_id", "=", layout_lane_id))
        else:
            domain.append(("layout_lane_id", "=", False))
        # Normal path is INSERT-only. PostgreSQL unique indexes protect both
        # resolved Lane candidates and unresolved raw receipts.
        try:
            with self.env.cr.savepoint():
                return self.create(vals), False
        except IntegrityError:
            existing = self.search(domain, limit=1)
            if not existing:
                raise
            if self._business_values(existing) != self._business_values(vals):
                raise ValidationError(_(
                    "event_uid_conflict: Detection UID already exists with different source data."
                ))
            return existing, True

    @api.model
    def create_idempotent_batch(self, vals_list):
        """Create Detection rows in one ORM batch while preserving UID conflicts."""
        if not vals_list:
            return self.browse(), 0

        normalized = []
        for source in vals_list:
            vals = dict(source)
            uid = str(vals.get("event_uid") or "").strip()
            if not uid:
                raise ValidationError(_("missing_event_uid"))
            vals["event_uid"] = uid
            normalized.append(vals)

        resolved_uids = sorted({
            vals["event_uid"] for vals in normalized if vals.get("layout_lane_id")
        })
        unresolved_uids = sorted({
            vals["event_uid"] for vals in normalized if not vals.get("layout_lane_id")
        })
        existing = self.browse()
        if resolved_uids:
            existing |= self.search([
                ("event_uid", "in", resolved_uids),
                ("layout_lane_id", "!=", False),
            ])
        if unresolved_uids:
            existing |= self.search([
                ("event_uid", "in", unresolved_uids),
                ("layout_lane_id", "=", False),
            ])
        existing_by_key = {
            (record.event_uid, record.layout_lane_id.id or 0): record
            for record in existing
        }
        pending_by_key = {}
        new_values = []
        duplicate_count = 0

        for vals in normalized:
            key = (vals["event_uid"], int(vals.get("layout_lane_id") or 0))
            current = existing_by_key.get(key)
            if current:
                if self._business_values(current) != self._business_values(vals):
                    raise ValidationError(_(
                        "event_uid_conflict: Detection UID already exists with different source data."
                    ))
                duplicate_count += 1
                continue
            prior_vals = pending_by_key.get(key)
            if prior_vals is not None:
                if self._business_values(prior_vals) != self._business_values(vals):
                    raise ValidationError(_(
                        "event_uid_conflict: Duplicate Detection UID in the same batch has different source data."
                    ))
                duplicate_count += 1
                continue
            pending_by_key[key] = vals
            new_values.append(vals)

        if not new_values:
            return self.browse(), duplicate_count

        try:
            with self.env.cr.savepoint():
                created = self.create(new_values)
            return created, duplicate_count
        except IntegrityError:
            # A concurrent retry can win between the prefetch and batch INSERT.
            # Fall back only on this rare race; the normal path remains one batch.
            created = self.browse()
            race_duplicates = 0
            for vals in new_values:
                record, duplicate = self.create_idempotent(vals)
                if duplicate:
                    race_duplicates += 1
                else:
                    created |= record
            return created, duplicate_count + race_duplicates

    @api.model
    def _assignment_error_code(self, assignment, tid):
        if not assignment or assignment.tid != tid:
            return "rfid_assignment_not_found"
        if bool(assignment.user_id) == bool(assignment.vehicle_id):
            return "invalid_rfid_assignment"
        target = assignment.user_id or assignment.vehicle_id
        if not target or not target.active:
            return "rfid_assignment_target_inactive"
        return False

    @api.model
    def _unresolved_detection_values(
        self, controller, payload, error_code, reader=False
    ):
        """Return the minimal raw evidence kept when business resolution fails."""
        return {
            "event_uid": str(payload.get("event_uid") or "").strip(),
            "detected_at": payload.get("detected_at"),
            "controller_id": controller.id if controller else False,
            "serial_number": False if reader else str(payload.get("serial_number") or "").strip().upper(),
            "reader_id": reader.id if reader else False,
            "port_no": int(payload.get("port_no") or 0),
            "tid": self.env["nsp.rfid.runtime.assignment"]._normalize_tid(payload.get("tid")),
            "error_code": error_code
            if error_code in dict(self._fields["error_code"].selection)
            else "processing_error",
        }

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
            return {}, {}, {}

        serials = {serial for serial, _port in keys}
        # Reader identity is independent from Controller. Controller scope is
        # resolved only through contextual Lane Configuration below.
        devices = self.env["nsp.device"].sudo().search([
            ("serial_number", "in", list(serials)),
            ("active", "=", True),
            ("cloud_removed", "=", False),
        ])
        device_by_serial = {
            str(device.serial_number or "").strip().upper(): device
            for device in devices
        }
        timeline_rows = self.env["nsp.parking.layout.lane.sequence"].sudo().search([
            ("layout_lane_id.active", "=", True),
            ("layout_lane_id.parking_area_id.state", "=", "operational"),
            ("reader_id", "in", devices.ids),
            ("port_no", "in", list({port for _serial, port in keys})),
        ]) if devices else self.env["nsp.parking.layout.lane.sequence"].browse()

        lanes_by_key = {}
        scoped_lanes_by_key = {}
        for row in timeline_rows:
            key = (
                str(row.reader_id.serial_number or "").strip().upper(),
                int(row.port_no or 0),
            )
            lane = row.layout_lane_id
            lanes_by_key.setdefault(key, set()).add(lane.id)
            if lane.controller_id == controller:
                scoped_lanes_by_key.setdefault(key, set()).add(lane.id)

        resolved = {}
        errors = {}
        Lane = self.env["nsp.parking.layout.lane"].sudo()
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
            scoped_lane_ids = scoped_lanes_by_key.get(key, set())
            if not scoped_lane_ids:
                errors[key] = "controller_not_in_scope"
                continue
            lanes = Lane.browse(sorted(scoped_lane_ids))
            parking_areas = lanes.mapped("parking_area_id")
            if len(parking_areas) != 1:
                # Sharing Reader/Antenna points is valid only between logical
                # Lanes of the same operational Parking Layout.
                errors[key] = "ambiguous_reader_port_layout"
                continue
            resolved[key] = [
                (device, lane, port_no) for lane in lanes.sorted(key=lambda item: item.id)
            ]
        return resolved, errors, device_by_serial

    @api.model
    def _runtime_snapshot_for_topology(self, topology_cache):
        """Lock each touched Parking Area once and cache its runtime revision.

        A Controller batch can contain hundreds of reads that resolve to the same
        Lane/Area. Re-acquiring the same advisory lock and invalidating the Area on
        every candidate only adds SQL round-trips; the transaction lock already
        protects one immutable snapshot for the whole ingest transaction.
        """
        area_ids = sorted({
            lane.parking_area_id.id
            for topologies in topology_cache.values()
            for _reader, lane, _port_no in topologies
            if lane.parking_area_id
        })
        areas = self.env["nsp.parking.area"].sudo().browse(area_ids).exists()

        snapshots = {}
        for area in areas:
            self._acquire_parking_area_runtime_lock(area, shared=True)
        if areas:
            areas.invalidate_recordset(["state", "published_revision"])
        for area in areas:
            snapshots[area.id] = {
                "state": area.state,
                "revision": int(area.published_revision or 0),
            }
        return snapshots

    @api.model
    def _candidate_values(self, payload, assignment, topologies, runtime_snapshot):
        """Build resolved Lane candidates from one API-normalized observation."""
        values = []
        lane_ids = set()
        for reader, lane, port_no in topologies:
            runtime = runtime_snapshot.get(lane.parking_area_id.id, {})
            revision = int(runtime.get("revision") or 0)
            if runtime.get("state") != "operational" or revision <= 0:
                return [], set(), "parking_area_not_operational"
            values.append({
                "event_uid": payload["event_uid"],
                "detected_at": payload["detected_at"],
                "layout_lane_id": lane.id,
                "reader_id": reader.id,
                "port_no": port_no,
                "tid": payload["tid"],
                "layout_revision": revision,
                "user_id": assignment.user_id.id if assignment.user_id else False,
                "vehicle_id": assignment.vehicle_id.id if assignment.vehicle_id else False,
            })
            lane_ids.add(lane.id)
        return values, lane_ids, False

    @api.model
    def ingest_controller_detections(self, controller, detections):
        """Accept one normalized Controller batch into the Edge working buffer.

        Known RFID observations become short-lived Lane candidates; meaningful
        resolution failures for known TIDs become temporary error rows. Unknown TIDs
        are ignored before persistence. Successful candidates are consumed immediately
        after Parking business processing.
        """
        self._ensure_edge_role()
        if not isinstance(detections, list):
            raise ValidationError(_("invalid_payload"))

        # Unknown TIDs are acquisition noise for Parking runtime. They have no
        # business identity to match and must not occupy Detection Logs or trigger
        # topology resolution. Keep invalid/inactive *known* assignments below as
        # short-lived diagnostics because those indicate an Edge configuration issue.
        known_detections = []
        ignored_unknown_tid_detections = 0
        for payload, assignment in detections:
            tid = self.env["nsp.rfid.runtime.assignment"]._normalize_tid(payload.get("tid"))
            if not assignment or assignment.tid != tid:
                ignored_unknown_tid_detections += 1
                continue
            known_detections.append((payload, assignment))

        topology_cache, topology_errors, device_by_serial = self._resolve_topology_batch(
            controller, known_detections
        )
        runtime_snapshot = self._runtime_snapshot_for_topology(topology_cache)
        touched_lane_ids = set()
        candidate_values = []
        error_values = []
        stats = {
            "received": len(detections),
            "ignored_unknown_tid_detections": ignored_unknown_tid_detections,
            "candidate_records_created": 0,
            "error_records_created": 0,
            "duplicates": 0,
        }

        for payload, assignment in known_detections:
            serial = str(payload.get("serial_number") or "").strip().upper()
            try:
                port_no = int(payload.get("port_no") or 0)
            except (TypeError, ValueError):
                port_no = 0
            topology_key = (serial, port_no)
            tid = self.env["nsp.rfid.runtime.assignment"]._normalize_tid(payload.get("tid"))
            terminal_error = (
                self._assignment_error_code(assignment, tid)
                or topology_errors.get(topology_key)
            )
            if terminal_error:
                error_values.append(self._unresolved_detection_values(
                    controller, payload, terminal_error,
                    reader=device_by_serial.get(serial),
                ))
                _logger.debug(
                    "Parking raw detection unresolved: controller=%s event_uid=%s "
                    "serial=%s port=%s tid=%s reason=%s",
                    controller.controller_id, payload.get("event_uid"), serial,
                    port_no, tid, terminal_error,
                )
                continue

            values, lane_ids, runtime_error = self._candidate_values(
                payload, assignment, topology_cache[topology_key], runtime_snapshot
            )
            if runtime_error:
                error_values.append(self._unresolved_detection_values(
                    controller, payload, runtime_error,
                    reader=device_by_serial.get(serial),
                ))
                _logger.debug(
                    "Parking detection candidate rejected: controller=%s event_uid=%s "
                    "serial=%s port=%s tid=%s reason=%s",
                    controller.controller_id, payload.get("event_uid"), serial,
                    port_no, tid, runtime_error,
                )
                continue
            candidate_values.extend(values)
            touched_lane_ids.update(lane_ids)

        created_candidates, candidate_duplicates = self.create_idempotent_batch(
            candidate_values
        )
        created_errors, error_duplicates = self.create_idempotent_batch(error_values)
        stats["candidate_records_created"] = len(created_candidates)
        stats["error_records_created"] = len(created_errors)
        stats["duplicates"] = candidate_duplicates + error_duplicates

        ordered_lane_ids = self._pending_lane_ids_in_event_order(touched_lane_ids)
        for lane in self.env["nsp.parking.layout.lane"].sudo().browse(ordered_lane_ids).exists():
            # Acquisition and Parking business processing are deliberately isolated.
            # Once candidate detections are persisted, a matcher/business exception
            # must not roll back the acquisition batch or force Controller retries.
            try:
                with self.env.cr.savepoint():
                    self._process_pending_for_lane(lane, finalize_expired=False)
            except Exception:
                _logger.exception(
                    "Parking business processing deferred after raw detection ingest: "
                    "controller=%s layout_lane=%s pending_events_preserved=true",
                    controller.controller_id, lane.id,
                )
        stats["persisted"] = (
            stats["candidate_records_created"] + stats["error_records_created"]
        )
        return stats

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
        """Discard pending work that cannot belong to the incoming runtime snapshot."""
        parking_area = parking_area.exists()
        if not parking_area:
            return 0
        try:
            revision = int(incoming_revision or 0)
        except (TypeError, ValueError):
            revision = 0
        lane_ids = parking_area.layout_lane_ids.ids
        if not lane_ids:
            return 0

        if incoming_state == "operational":
            self.env.cr.execute(
                """
                DELETE FROM nsp_parking_detection_event
                 WHERE error_code IS NULL
                   AND layout_lane_id = ANY(%s)
                   AND layout_revision <> %s
                """,
                (lane_ids, revision),
            )
        else:
            self.env.cr.execute(
                """
                DELETE FROM nsp_parking_detection_event
                 WHERE error_code IS NULL
                   AND layout_lane_id = ANY(%s)
                """,
                (lane_ids,),
            )
        affected = self.env.cr.rowcount
        if affected:
            self.invalidate_model()
        return affected

    @api.model
    def _invalidate_lane_revision(self, lane, revision):
        self.env.cr.execute(
            """
            DELETE FROM nsp_parking_detection_event
             WHERE error_code IS NULL
               AND layout_lane_id = %s
               AND layout_revision <> %s
            """,
            (lane.id, int(revision or 0)),
        )
        affected = self.env.cr.rowcount
        if affected:
            self.invalidate_model()
        return affected

    @api.model
    def _pending_lane_ids_in_event_order(self, lane_ids=False):
        """Return Lanes ordered by their earliest pending detection."""
        params = []
        lane_filter = ""
        normalized_ids = [int(value) for value in (lane_ids or []) if int(value) > 0]
        if normalized_ids:
            lane_filter = " AND layout_lane_id = ANY(%s)"
            params.append(normalized_ids)
        self.env.cr.execute(
            f"""
            SELECT layout_lane_id
              FROM nsp_parking_detection_event
             WHERE error_code IS NULL
               AND layout_lane_id IS NOT NULL
               {lane_filter}
             GROUP BY layout_lane_id
             ORDER BY MIN(detected_at) ASC, MIN(id) ASC, layout_lane_id ASC
            """,
            params,
        )
        return [row[0] for row in self.env.cr.fetchall()]

    @api.model
    def _pending_user_pool(self, lane, start_at=False, end_at=False):
        domain = [
            ("layout_lane_id", "=", lane.id),
            ("error_code", "=", False),
            ("layout_revision", "=", int(lane.parking_area_id.published_revision or 0)),
            ("user_id", "!=", False),
        ]
        if start_at:
            domain.append(("detected_at", ">=", start_at))
        if end_at:
            domain.append(("detected_at", "<=", end_at))
        return self.search(domain, order="detected_at asc, id asc")

    @api.model
    def _user_candidates_from_pool(
        self, user_events, anchor_at, window_seconds, consumed_ids
    ):
        """Return unused User reads inside the configured movement window."""
        if not user_events:
            return self.browse()
        window = max(0.001, float(window_seconds or 0.0))
        return user_events.filtered(
            lambda event: (
                event.id not in consumed_ids
                and not event.error_code
                and abs((event.detected_at - anchor_at).total_seconds()) <= window
            )
        ).sorted(key=lambda event: (
            abs((event.detected_at - anchor_at).total_seconds()),
            event.detected_at,
            event.id,
        ))

    @api.model
    def _drop_expired_pending(self, lane, now, identity_field):
        """Delete incomplete RFID noise once it can no longer form a sequence."""
        cutoff = now - timedelta(seconds=lane.max_sequence_window())
        if identity_field not in ("user_id", "vehicle_id"):
            raise ValidationError(_("invalid_detection_identity_field"))
        self.env.cr.execute(
            f"""
            DELETE FROM nsp_parking_detection_event
             WHERE error_code IS NULL
               AND layout_lane_id = %s
               AND {identity_field} IS NOT NULL
               AND detected_at < %s
            """,
            (lane.id, cutoff),
        )
        deleted = self.env.cr.rowcount
        if deleted:
            self.invalidate_model()
        return deleted

    @api.model
    def _expire_orphan_user_events(self, lane, now):
        return self._drop_expired_pending(lane, now, "user_id")

    @api.model
    def _build_vehicle_sequence_matches(self, lane):
        """Match one Lane Configuration against each Vehicle TID raw timeline.

        Repeated RFID reads are interpreted inside the Lane-specific matcher.
        The raw timeline is never collapsed before Antenna Sequence and Max
        Duration rules are applied.
        """
        rows = lane.antenna_sequence_ids.sorted("sequence")
        if len(rows) < 2:
            return []
        vehicle_events = self.search([
            ("layout_lane_id", "=", lane.id),
            ("error_code", "=", False),
            ("layout_revision", "=", int(lane.parking_area_id.published_revision or 0)),
            ("vehicle_id", "!=", False),
        ], order="tid asc, detected_at asc, id asc")
        if not vehicle_events:
            return []

        expected_keys = [(row.reader_id.id, int(row.port_no or 0)) for row in rows]
        allowed_durations = [
            0.0 if index == 0 else max(0.001, float(row.duration_from_previous or 0.0))
            for index, row in enumerate(rows)
        ]

        events_by_tid = {}
        for event in vehicle_events:
            events_by_tid.setdefault(event.tid, []).append(event)

        matches = []
        total_allowed = max(0.001, sum(allowed_durations[1:]))
        for tid, raw_events in events_by_tid.items():
            match_details = match_ordered_sequence_details(
                raw_events,
                expected_keys,
                allowed_durations,
                key_of=lambda event: (event.reader_id.id, int(event.port_no or 0)),
                time_of=lambda event: event.detected_at,
            )
            for detail in match_details:
                path = detail["path"]
                consumed_events = detail["consumed_events"]
                matches.append({
                    "tid": tid,
                    "duration_seconds": total_allowed,
                    "start_at": path[0].detected_at,
                    "end_at": path[-1].detected_at,
                    "events": self.browse([event.id for event in path]),
                    "consume_events": self.browse([event.id for event in consumed_events]),
                })

        matches.sort(key=lambda item: (item["end_at"], item["start_at"], item["tid"]))
        return matches

    @api.model
    def _expire_stale_vehicle_events(self, lane, now):
        return self._drop_expired_pending(lane, now, "vehicle_id")

    def _consume_detection_group(self, events, vehicle_events=False):
        """Delete consumed reads, sibling Lane copies, and duplicate Vehicle reads."""
        events = events.exists()
        if not events:
            return 0
        vehicle_events = (vehicle_events or events).exists().filtered("vehicle_id")
        # ``vehicle_events`` can include repeated reads that the Lane matcher
        # intentionally ignored/replaced while constructing the successful path.
        # Include their physical event UIDs as well so sibling Lane fan-out copies
        # are removed together with the winning traversal.
        delete_events = (events | vehicle_events).exists()
        event_uids = sorted({uid for uid in delete_events.mapped("event_uid") if uid})
        vehicle_tids = {tid for tid in vehicle_events.mapped("tid") if tid}
        start_at = min(vehicle_events.mapped("detected_at")) if vehicle_events else False
        end_at = max(vehicle_events.mapped("detected_at")) if vehicle_events else False

        clauses = []
        params = []
        if event_uids:
            clauses.append("event_uid = ANY(%s)")
            params.append(event_uids)
        if len(vehicle_tids) == 1 and start_at and end_at:
            clauses.append(
                "(vehicle_id IS NOT NULL AND tid = %s AND detected_at >= %s AND detected_at <= %s)"
            )
            params.extend([next(iter(vehicle_tids)), start_at, end_at])
        if not clauses:
            clauses.append("id = ANY(%s)")
            params.append(events.ids)

        self.env.cr.execute(
            f"""
            DELETE FROM nsp_parking_detection_event
             WHERE error_code IS NULL
               AND ({' OR '.join(clauses)})
            """,
            params,
        )
        deleted = self.env.cr.rowcount
        if deleted:
            self.invalidate_model()
        return deleted

    def _create_log_for_vehicle(
        self,
        vehicle_events,
        movement_state,
        user_events=False,
        authorized_borrow_map=False,
        consume_vehicle_events=False,
        consume_user_events=False,
    ):
        event_type = movement_state.get("event_type")
        supporting_users = user_events or self.browse()
        group = vehicle_events | supporting_users if event_type == "check_out" else vehicle_events
        parking_log = self.env["nsp.parking.log"].sudo().create_from_detection_group(
            group,
            movement_state=movement_state,
            authorized_borrow_map=authorized_borrow_map,
        )
        self._consume_detection_group(
            group | (consume_user_events or self.browse()),
            vehicle_events=consume_vehicle_events or vehicle_events,
        )
        return parking_log

    @api.model
    def _process_sequence_matches(self, lane, now, finalize_expired=True):
        logs = self.env["nsp.parking.log"].browse()
        matches = self._build_vehicle_sequence_matches(lane)
        if not matches:
            if finalize_expired:
                self._expire_stale_vehicle_events(lane, now)
                self._expire_orphan_user_events(lane, now)
            return logs

        ParkingLog = self.env["nsp.parking.log"].sudo()
        layout_revision = int(lane.parking_area_id.published_revision or 0)
        max_window = max(0.001, float(lane.max_sequence_window() or 0.001))
        user_pool_start = min(item["end_at"] for item in matches) - timedelta(seconds=max_window)
        user_pool_end = max(item["end_at"] for item in matches) + timedelta(seconds=max_window)
        user_events = self._pending_user_pool(
            lane, start_at=user_pool_start, end_at=user_pool_end
        )
        consumed_user_ids = set()
        blocked_tids = set()

        for match in matches:
            tid = match["tid"]
            if tid in blocked_tids:
                continue
            source_events = match["events"].exists()
            consume_vehicle_events = match.get("consume_events", source_events).exists()
            if not source_events:
                continue

            vehicle_event = source_events.filtered("vehicle_id").sorted(
                key=lambda rec: (rec.detected_at, rec.id)
            )[-1:]
            vehicle = vehicle_event.vehicle_id if vehicle_event else self.env["nsp.vehicle"].browse()
            if not vehicle:
                source_events.filtered(lambda rec: not rec.error_code).write({
                    "error_code": "vehicle_not_found",
                })
                continue

            duration = max(0.001, float(match["duration_seconds"] or 0.001))
            ParkingLog._acquire_vehicle_continuity_lock(vehicle)

            # Re-check existence/error after waiting on the Vehicle lock. Successful
            # rows are physically deleted by the winning movement instead of being
            # retained as a processed technical history.
            source_events = source_events.exists()
            consume_vehicle_events = consume_vehicle_events.exists()
            source_events.invalidate_recordset(["error_code"])
            movement_events = source_events.filtered(lambda rec: not rec.error_code)
            if not movement_events or len(movement_events) != len(source_events):
                continue

            # Suppress a repeated physical crossing before resolving Check-in/Check-out.
            # The index matches this exact Lane + revision + Vehicle + time lookup.
            window_start = match["end_at"] - timedelta(seconds=duration)
            recent = ParkingLog.search([
                ("layout_lane_id", "=", lane.id),
                ("layout_revision", "=", layout_revision),
                ("vehicle_id", "=", vehicle.id),
                ("event_time", ">=", window_start),
                ("event_time", "<=", match["end_at"]),
            ], order="event_time desc, id desc", limit=1)
            if recent:
                self._consume_detection_group(
                    movement_events, vehicle_events=consume_vehicle_events
                )
                continue

            movement_state = ParkingLog._resolve_vehicle_movement(
                vehicle, match["end_at"], lane.parking_area_id
            )
            if movement_state.get("action") == "ignore":
                self._consume_detection_group(
                    movement_events, vehicle_events=consume_vehicle_events
                )
                continue

            event_type = movement_state.get("event_type")
            matched_user_events = self.browse()
            consume_user_events = self.browse()
            authorized_borrow_map = False
            if event_type == "check_out" and movement_state.get("action") != "deny":
                deadline = match["end_at"] + timedelta(seconds=duration)
                deadline_reached = bool(finalize_expired and now >= deadline)
                candidates = self._user_candidates_from_pool(
                    user_events, match["end_at"], duration, consumed_user_ids
                )
                # Gatekeeper operating rule: one Vehicle + one User may occupy a
                # Lane read zone at a time. As soon as a User read exists, select
                # the read nearest to Vehicle sequence completion and resolve
                # Owner/Borrower authorization immediately. Only the absence of a
                # User read keeps the Vehicle pending until the Lane window closes.
                nearest_user_event = candidates[:1]
                if not nearest_user_event:
                    if not deadline_reached:
                        blocked_tids.add(tid)
                        continue
                else:
                    matched_user_events = nearest_user_event
                    selected_user = nearest_user_event.user_id
                    # Consume all repeated reads of the selected User inside this
                    # crossing window so they cannot leak into the next Vehicle.
                    # Reads belonging to any other User remain untouched.
                    consume_user_events = candidates.filtered(
                        lambda event: event.user_id.id == selected_user.id
                    )
                    authorized_borrow_map = ParkingLog._authorized_user_borrow_map(
                        vehicle, match["end_at"]
                    )

            try:
                with self.env.cr.savepoint():
                    parking_log = self._create_log_for_vehicle(
                        movement_events,
                        movement_state,
                        user_events=matched_user_events,
                        authorized_borrow_map=authorized_borrow_map,
                        consume_vehicle_events=consume_vehicle_events,
                        consume_user_events=consume_user_events,
                    )
                    logs |= parking_log
                    consumed_user_ids.update(consume_user_events.ids)
            except Exception:
                _logger.exception(
                    "Parking Antenna Sequence processing failed: lane=%s event_type=%s ids=%s",
                    lane.id, event_type, movement_events.ids,
                )
                failed_vehicle_events = (movement_events | consume_vehicle_events).exists().filtered(
                    lambda rec: not rec.error_code
                )
                if failed_vehicle_events:
                    failed_vehicle_events.write({"error_code": "processing_error"})
                failed_user_events = consume_user_events.exists().filtered(
                    lambda rec: not rec.error_code
                )
                if failed_user_events:
                    failed_user_events.write({"error_code": "processing_error"})
                    consumed_user_ids.update(failed_user_events.ids)

        if finalize_expired:
            self._expire_stale_vehicle_events(lane, now)
            self._expire_orphan_user_events(lane, now)
        return logs

    @api.model
    def _process_pending_for_lane(self, lane, now=None, finalize_expired=True):
        area = lane.parking_area_id
        self._acquire_parking_area_runtime_lock(area, shared=True)
        # Runtime lock protects Layout revision changes. This separate processing
        # lock serializes logical Lanes inside one Parking Area so a fan-out copy
        # cannot be consumed concurrently by two Lane matchers.
        self.env.cr.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            (f"nsp.parking:area-processing:{area.id}",),
        )
        area.invalidate_recordset(["state", "published_revision"])
        revision = int(area.published_revision or 0)
        if area.state != "operational" or revision <= 0:
            self.invalidate_pending_for_runtime_change(area, revision, area.state)
            return self.env["nsp.parking.log"].browse()
        self._invalidate_lane_revision(lane, revision)
        now = fields.Datetime.to_datetime(now or fields.Datetime.now())
        return self._process_sequence_matches(lane, now, finalize_expired=finalize_expired)

    @api.model
    def process_pending_events(self):
        if self._deployment_role() != "edge_server":
            return True
        now = fields.Datetime.now()
        lane_ids = self._pending_lane_ids_in_event_order()
        Lane = self.env["nsp.parking.layout.lane"].sudo()
        for lane in Lane.browse(lane_ids).exists():
            self._process_pending_for_lane(lane, now=now)
        return True

    @api.model
    def cleanup_old_events(self):
        if self._deployment_role() != "edge_server":
            return True
        raw_hours = self.env["ir.config_parameter"].sudo().get_param(
            "nsp.parking_detection_error_retention_hours", "24"
        )
        try:
            retention_hours = max(1, min(int(raw_hours), 24 * 30))
        except (TypeError, ValueError):
            retention_hours = 24

        cutoff = fields.Datetime.now() - timedelta(hours=retention_hours)
        batch_size = 50000
        max_batches = 10
        deleted = 0
        for _batch in range(max_batches):
            self.env.cr.execute(
                """
                WITH doomed AS (
                    SELECT id
                      FROM nsp_parking_detection_event
                     WHERE error_code IS NOT NULL
                       AND detected_at < %s
                     ORDER BY detected_at ASC, id ASC
                     LIMIT %s
                )
                DELETE FROM nsp_parking_detection_event AS event
                 USING doomed
                 WHERE event.id = doomed.id
                """,
                (cutoff, batch_size),
            )
            count = self.env.cr.rowcount
            deleted += count
            if count < batch_size:
                break
        if deleted:
            self.invalidate_model()
            _logger.info(
                "Parking Detection cleanup deleted %s error rows older than %s",
                deleted, cutoff,
            )
        return True
