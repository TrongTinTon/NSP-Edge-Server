# -*- coding: utf-8 -*-
import logging
import os
from datetime import timedelta

from psycopg2 import IntegrityError

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


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
        help="Authenticated Controller that physically reported this RFID detection.",
    )
    serial_number = fields.Char(
        string="Reader Serial", readonly=True,
        help="Raw SDK Reader serial reported by the Controller. Kept even when Reader identity cannot be resolved.",
    )
    layout_lane_id = fields.Many2one(
        "nsp.parking.layout.lane", string="Lane Configuration",
        ondelete="restrict", readonly=True,
        help="Resolved contextual Lane candidate. Empty when raw detection cannot be mapped to runtime topology.",
    )
    lane_id = fields.Many2one(
        "nsp.parking.lane", string="Lane",
        related="layout_lane_id.lane_id", store=False, readonly=True,
        help="Presentation-only Lane Master resolved from the contextual Lane Configuration.",
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
    rssi_dbm = fields.Float(
        string="RSSI (dBm)", readonly=True,
        help="Optional signal strength reported by the Controller.",
    )
    user_id = fields.Many2one(
        "nsp.user", string="Resolved User", ondelete="restrict",
        readonly=True,
    )
    vehicle_id = fields.Many2one(
        "nsp.vehicle", string="Resolved Vehicle", ondelete="restrict",
        readonly=True,
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
            ("rfid_assignment_not_found", "RFID Assignment Not Found"),
            ("invalid_rfid_assignment", "Invalid RFID Assignment"),
            ("rfid_assignment_target_inactive", "RFID Assignment Target Inactive"),
            ("device_not_found", "Reader Not Found"),
            ("no_reader_port_timeline", "No Reader/Port Timeline"),
            ("controller_not_in_scope", "Controller Not In Scope"),
            ("ambiguous_reader_port_layout", "Ambiguous Reader/Port Layout"),
            ("layout_revision_superseded", "Layout Revision Superseded"),
            ("parking_area_not_operational", "Parking Area Not Operational"),
            ("sequence_timeout", "Sequence Timeout"),
            ("vehicle_not_found", "Vehicle Not Found"),
            ("processing_error", "Processing Error"),
            ("consumed_by_other_lane", "Consumed by Another Lane"),
            ("stale_movement", "Stale Movement Ignored"),
        ],
        string="Processing Result", readonly=True, copy=False,
    )
    error_message = fields.Char(
        string="Processing Message", compute="_compute_error_message", readonly=True,
    )
    parking_log_id = fields.Many2one(
        "nsp.parking.log", string="Parking Log",
        ondelete="set null", copy=False, readonly=True,
    )

    _sql_constraints = [
        (
            "event_uid_layout_lane_unique",
            "unique(event_uid, layout_lane_id)",
            "Detection UID must be unique per Lane Configuration.",
        ),
        (
            "parking_detection_port_range",
            "CHECK(port_no >= 1 AND port_no <= 16)",
            "Reader Port must be between 1 and 16.",
        ),
    ]

    def init(self):
        # Detection is a high-write short-retention table. Keep only indexes that
        # match runtime queries; standalone indexes on every searchable UI field
        # multiply INSERT/UPDATE cost without helping the parking hot path.
        self.env.cr.execute(
            """
            CREATE INDEX IF NOT EXISTS nsp_parking_detection_pending_lane_idx
                ON nsp_parking_detection_event (layout_lane_id, detected_at, id)
             WHERE state = 'pending' AND parking_log_id IS NULL;

            CREATE INDEX IF NOT EXISTS nsp_parking_detection_pending_vehicle_idx
                ON nsp_parking_detection_event
                   (layout_lane_id, layout_revision, tid, detected_at, id)
             WHERE state = 'pending'
               AND parking_log_id IS NULL
               AND vehicle_id IS NOT NULL;

            CREATE INDEX IF NOT EXISTS nsp_parking_detection_pending_user_idx
                ON nsp_parking_detection_event
                   (layout_lane_id, layout_revision, detected_at, id)
             WHERE state = 'pending'
               AND parking_log_id IS NULL
               AND user_id IS NOT NULL;
            """
        )
        self.env.cr.execute(
            """
            CREATE INDEX IF NOT EXISTS nsp_parking_detection_cleanup_idx
                ON nsp_parking_detection_event (detected_at, id)
             WHERE state IN ('processed', 'error')
            """
        )
        self.env.cr.execute(
            """
            CREATE INDEX IF NOT EXISTS nsp_parking_detection_parking_log_idx
                ON nsp_parking_detection_event (parking_log_id, id)
             WHERE parking_log_id IS NOT NULL
            """
        )
        self.env.cr.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS nsp_parking_detection_unresolved_uid_unique
                ON nsp_parking_detection_event (event_uid)
             WHERE layout_lane_id IS NULL
            """
        )

        # Drop legacy Odoo-created standalone indexes. Names follow Odoo's normal
        # <table>_<column>_index convention; IF EXISTS keeps upgrades portable.
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
        # Idempotency compares immutable physical source data only. Reader/Lane/User/
        # Vehicle resolution is Edge-derived and may legitimately change after the
        # original receipt; a transport retry must never reinterpret the same UID.
        return {
            "detected_at": detected_at or "",
            "controller_id": int(value("controller_id") or 0),
            "serial_number": str(value("serial_number") or "").strip().upper(),
            "layout_lane_id": int(value("layout_lane_id") or 0),
            "port_no": int(value("port_no") or 0),
            "tid": str(value("tid") or "").strip(),
            "rssi_dbm": float(value("rssi_dbm") or 0.0),
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
    def _detection_error_message(self, code):
        messages = {
            "rfid_assignment_not_found": _("RFID TID has no active runtime assignment on Edge."),
            "invalid_rfid_assignment": _("RFID runtime assignment must resolve to exactly one User or Vehicle."),
            "rfid_assignment_target_inactive": _("RFID runtime assignment target is inactive."),
            "device_not_found": _("Reader serial reported by Controller is not an active Reader identity on Edge."),
            "no_reader_port_timeline": _("Reader/Port is not present in an operational Lane Antenna Sequence."),
            "controller_not_in_scope": _("Reader/Port exists, but this Controller is not referenced by the Lane Configuration."),
            "ambiguous_reader_port_layout": _("Reader/Port resolves to more than one Parking Layout."),
            "parking_area_not_operational": _("Parking Area runtime is not operational."),
            "layout_revision_superseded": _("Detection belongs to a superseded Parking Layout revision."),
            "sequence_timeout": _("RFID detection expired before a complete movement could be resolved."),
            "vehicle_not_found": _("Vehicle identity is missing for the matched Antenna Sequence."),
            "processing_error": _("Parking Log processing failed. See Edge logs for details."),
            "consumed_by_other_lane": _("Consumed by another matching logical Lane."),
            "stale_movement": _("Movement is older than the latest allowed Parking Log and was ignored."),
        }
        return messages.get(code, False)

    @api.depends("state", "error_code")
    def _compute_error_message(self):
        for record in self:
            record.error_message = self._detection_error_message(record.error_code) or False

    @api.model
    def _persist_unresolved_detection(
        self, controller, payload, error_code, reader=False, assignment=False
    ):
        """Persist a valid transport observation even when business resolution fails."""
        vals = {
            "event_uid": str(payload.get("event_uid") or "").strip(),
            "detected_at": payload.get("detected_at"),
            "controller_id": controller.id if controller else False,
            "serial_number": str(payload.get("serial_number") or "").strip().upper(),
            "reader_id": reader.id if reader else False,
            "port_no": int(payload.get("port_no") or 0),
            "tid": self.env["nsp.rfid.runtime.assignment"]._normalize_tid(payload.get("tid")),
            "rssi_dbm": float(payload.get("rssi_dbm") or 0.0),
            "user_id": assignment.user_id.id if assignment and assignment.user_id else False,
            "vehicle_id": assignment.vehicle_id.id if assignment and assignment.vehicle_id else False,
            "state": "error",
            "error_code": error_code if error_code in dict(self._fields["error_code"].selection) else "processing_error",
        }
        return self.create_idempotent(vals)

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
        for row in timeline_rows:
            key = (
                str(row.reader_id.serial_number or "").strip().upper(),
                int(row.port_no or 0),
            )
            lanes_by_key.setdefault(key, set()).add(row.layout_lane_id.id)

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
            lanes = Lane.browse(sorted(lane_ids)).exists().filtered(
                lambda candidate: candidate.controller_id == controller
            )
            if not lanes:
                errors[key] = "controller_not_in_scope"
                continue
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
    def _ingest_controller_detection(
        self, controller, payload, assignment, topology_cache, runtime_snapshot
    ):
        if not isinstance(payload, dict):
            raise ValidationError(_("invalid_payload"))

        event_uid = str(payload.get("event_uid") or "").strip()
        serial_number = str(payload.get("serial_number") or "").strip().upper()
        tid = self.env["nsp.rfid.runtime.assignment"]._normalize_tid(payload.get("tid"))
        try:
            port_no = int(payload.get("port_no") or 0)
        except (TypeError, ValueError) as exc:
            raise ValidationError(_("invalid_payload: port_no")) from exc
        try:
            rssi_dbm = float(payload.get("rssi_dbm") or 0.0)
        except (TypeError, ValueError):
            rssi_dbm = 0.0
        try:
            detected_at = fields.Datetime.to_string(
                fields.Datetime.to_datetime(payload.get("detected_at"))
            )
        except (TypeError, ValueError):
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

        topologies = topology_cache.get((serial_number, port_no))
        if not topologies:
            raise ValidationError(_("no_reader_port_timeline"))

        records = self.browse()
        new_lanes = self.env["nsp.parking.layout.lane"].browse()
        duplicate_count = 0
        for reader, lane, candidate_port_no in topologies:
            parking_area = lane.parking_area_id
            runtime = runtime_snapshot.get(parking_area.id, {})
            layout_revision = int(runtime.get("revision") or 0)
            if runtime.get("state") != "operational" or layout_revision <= 0:
                raise ValidationError(_("parking_area_not_operational"))

            vals = {
                "event_uid": event_uid,
                "detected_at": detected_at,
                "controller_id": controller.id,
                "serial_number": serial_number,
                "layout_lane_id": lane.id,
                "reader_id": reader.id,
                "port_no": candidate_port_no,
                "tid": tid,
                "layout_revision": layout_revision,
                "rssi_dbm": rssi_dbm,
                "user_id": assignment.user_id.id if assignment.user_id else False,
                "vehicle_id": assignment.vehicle_id.id if assignment.vehicle_id else False,
                "state": "pending",
            }
            record, duplicate = self.create_idempotent(vals)
            records |= record
            if duplicate:
                duplicate_count += 1
            else:
                new_lanes |= lane
        return records, new_lanes, duplicate_count

    @api.model
    def ingest_controller_detections(self, controller, detections):
        """Persist every valid raw Controller observation before business resolution.

        Transport acceptance must never silently drop a detection. When RFID
        identity or Parking topology cannot be resolved, Edge stores one terminal
        Detection Log with an explicit reason. Only fully resolved Lane candidates
        enter sequence/business processing.
        """
        self._ensure_edge_role()
        if not isinstance(detections, list):
            raise ValidationError(_("invalid_payload"))

        topology_cache, topology_errors, device_by_serial = self._resolve_topology_batch(
            controller, detections
        )
        runtime_snapshot = self._runtime_snapshot_for_topology(topology_cache)
        touched_lanes = self.env["nsp.parking.layout.lane"].browse()
        stats = {
            "received": len(detections),
            "candidate_records_created": 0,
            "error_records_created": 0,
            "duplicates": 0,
        }

        for payload, assignment in detections:
            serial = str(payload.get("serial_number") or "").strip().upper()
            try:
                port_no = int(payload.get("port_no") or 0)
            except (TypeError, ValueError):
                port_no = 0
            topology_key = (serial, port_no)
            tid = self.env["nsp.rfid.runtime.assignment"]._normalize_tid(payload.get("tid"))
            assignment_error = self._assignment_error_code(assignment, tid)
            topology_error = topology_errors.get(topology_key)

            # Persist transport receipt even when Edge cannot resolve it into a
            # business candidate. Assignment errors take precedence because they
            # explain why the TID cannot participate in Parking processing at all.
            terminal_error = assignment_error or topology_error
            if terminal_error:
                record, duplicate = self._persist_unresolved_detection(
                    controller, payload, terminal_error,
                    reader=device_by_serial.get(serial), assignment=assignment,
                )
                if duplicate:
                    stats["duplicates"] += 1
                else:
                    stats["error_records_created"] += 1
                _logger.warning(
                    "Parking raw detection persisted as unresolved: controller=%s "
                    "event_uid=%s serial=%s port=%s tid=%s reason=%s",
                    controller.controller_id, payload.get("event_uid"), serial,
                    port_no, tid, terminal_error,
                )
                continue

            try:
                with self.env.cr.savepoint():
                    records, new_lanes, duplicates = self._ingest_controller_detection(
                        controller, payload, assignment, topology_cache, runtime_snapshot
                    )
                touched_lanes |= new_lanes
                stats["duplicates"] += duplicates
                stats["candidate_records_created"] += max(0, len(records) - duplicates)
            except ValidationError as exc:
                # A valid transport observation must remain visible even when a
                # late runtime/configuration check rejects candidate creation.
                text = str(exc or "")
                known_code = next((code for code in (
                    "parking_area_not_operational",
                    "rfid_assignment_not_found",
                    "invalid_rfid_assignment",
                    "rfid_assignment_target_inactive",
                    "device_not_found",
                    "no_reader_port_timeline",
                    "controller_not_in_scope",
                    "ambiguous_reader_port_layout",
                ) if code in text), "processing_error")
                record, duplicate = self._persist_unresolved_detection(
                    controller, payload, known_code,
                    reader=device_by_serial.get(serial), assignment=assignment,
                )
                if duplicate:
                    stats["duplicates"] += 1
                else:
                    stats["error_records_created"] += 1
                _logger.warning(
                    "Parking detection candidate rejected but raw receipt preserved: "
                    "controller=%s event_uid=%s serial=%s port=%s tid=%s reason=%s",
                    controller.controller_id, payload.get("event_uid"), serial,
                    port_no, tid, exc,
                )

        ordered_lane_ids = self._pending_lane_ids_in_event_order(touched_lanes.ids)
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
        """Close pending detections that cannot be evaluated by a runtime snapshot.

        Query directly by contextual Lane IDs so PostgreSQL can use the partial
        pending indexes. Never load all pending rows and filter them in Python.
        """
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
        domain = [
            ("layout_lane_id", "in", lane_ids),
            ("state", "=", "pending"),
            ("parking_log_id", "=", False),
        ]
        if incoming_state == "operational":
            domain.append(("layout_revision", "!=", revision))
            values = {
                "state": "error",
                "error_code": "layout_revision_superseded",
            }
        else:
            values = {
                "state": "error",
                "error_code": "parking_area_not_operational",
            }
        affected = self.sudo().search(domain)
        if affected:
            affected.write(values)
        return len(affected)

    @api.model
    def _invalidate_lane_revision(self, lane, revision):
        stale = self.sudo().search([
            ("layout_lane_id", "=", lane.id),
            ("state", "=", "pending"),
            ("parking_log_id", "=", False),
            ("layout_revision", "!=", int(revision or 0)),
        ])
        if stale:
            stale.write({
                "state": "error",
                "error_code": "layout_revision_superseded",
            })
        return len(stale)

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
            lane_filter = " AND layout_lane_id = ANY(%s)"
            params.append(normalized_ids)
        self.env.cr.execute(
            f"""
            SELECT layout_lane_id
              FROM nsp_parking_detection_event
             WHERE state = 'pending'
               AND parking_log_id IS NULL
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
            ("state", "=", "pending"),
            ("parking_log_id", "=", False),
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
        """Return all unused User reads inside the configured movement window.

        Repeated physical reads for one User remain one identity. Different User
        identities in the same window produce an explicit denied Parking Log.
        """
        if not user_events:
            return self.browse()
        window = max(0.001, float(window_seconds or 0.0))
        return user_events.filtered(
            lambda event: (
                event.id not in consumed_ids
                and event.state == "pending"
                and not event.parking_log_id
                and abs((event.detected_at - anchor_at).total_seconds()) <= window
            )
        ).sorted(key=lambda event: (
            abs((event.detected_at - anchor_at).total_seconds()),
            event.detected_at,
            event.id,
        ))

    @api.model
    def _expire_orphan_user_events(self, lane, now):
        cutoff = now - timedelta(seconds=lane.max_sequence_window())
        stale = self.search([
            ("layout_lane_id", "=", lane.id),
            ("state", "=", "pending"),
            ("parking_log_id", "=", False),
            ("layout_revision", "=", int(lane.parking_area_id.published_revision or 0)),
            ("user_id", "!=", False),
            ("detected_at", "<", cutoff),
        ])
        if stale:
            stale.write({
                "state": "error",
                "error_code": "sequence_timeout",
            })


    @api.model
    def _build_vehicle_sequence_matches(self, lane):
        """Match the one published Antenna Sequence for each Vehicle TID.

        Direction is deliberately absent here. A successful match means only
        that the Vehicle crossed this Lane; Check-in/Check-out is resolved later
        from the Vehicle parking state.
        """
        rows = lane.antenna_sequence_ids.sorted("sequence")
        if len(rows) < 2:
            return []
        vehicle_events = self.search([
            ("layout_lane_id", "=", lane.id),
            ("state", "=", "pending"),
            ("parking_log_id", "=", False),
            ("layout_revision", "=", int(lane.parking_area_id.published_revision or 0)),
            ("vehicle_id", "!=", False),
        ], order="tid asc, detected_at asc, id asc")
        if not vehicle_events:
            return []

        expected_keys = [
            (row.reader_id.id, int(row.port_no or 0)) for row in rows
        ]
        allowed_durations = [
            0.0 if index == 0 else max(0.001, float(row.duration_from_previous or 0.0))
            for index, row in enumerate(rows)
        ]

        events_by_tid = {}
        for event in vehicle_events:
            events_by_tid.setdefault(event.tid, []).append(event)

        matches = []
        length = len(expected_keys)
        for tid, raw_events in events_by_tid.items():
            collapsed = []
            for event in raw_events:
                key = (event.reader_id.id, int(event.port_no or 0))
                if collapsed:
                    previous = (
                        collapsed[-1].reader_id.id, int(collapsed[-1].port_no or 0)
                    )
                    if previous == key:
                        continue
                collapsed.append(event)
            if len(collapsed) < length:
                continue
            for offset in range(0, len(collapsed) - length + 1):
                window = collapsed[offset:offset + length]
                actual_keys = [
                    (event.reader_id.id, int(event.port_no or 0)) for event in window
                ]
                if actual_keys != expected_keys:
                    continue
                total_allowed = 0.0
                valid = True
                for index in range(1, length):
                    allowed = allowed_durations[index]
                    gap = (window[index].detected_at - window[index - 1].detected_at).total_seconds()
                    if gap < 0 or gap > allowed:
                        valid = False
                        break
                    total_allowed += allowed
                if not valid:
                    continue
                matches.append({
                    "tid": tid,
                    "duration_seconds": max(0.001, total_allowed),
                    "start_at": window[0].detected_at,
                    "end_at": window[-1].detected_at,
                    "events": self.browse([event.id for event in window]),
                })
        matches.sort(key=lambda item: (item["end_at"], item["start_at"], item["tid"]))
        return matches

    @api.model
    def _expire_stale_vehicle_events(self, lane, now):
        cutoff = now - timedelta(seconds=lane.max_sequence_window())
        stale = self.search([
            ("layout_lane_id", "=", lane.id),
            ("state", "=", "pending"),
            ("parking_log_id", "=", False),
            ("layout_revision", "=", int(lane.parking_area_id.published_revision or 0)),
            ("vehicle_id", "!=", False),
            ("detected_at", "<", cutoff),
        ])
        if stale:
            stale.write({
                "state": "error",
                "error_code": "sequence_timeout",
            })

    def _create_log_for_vehicle(
        self,
        vehicle_events,
        movement_state,
        user_events=False,
        authorized_borrow_map=False,
    ):
        event_type = movement_state.get("event_type")
        supporting_users = user_events or self.browse()
        group = vehicle_events | supporting_users if event_type == "check_out" else vehicle_events
        parking_log = self.env["nsp.parking.log"].sudo().create_from_detection_group(
            group,
            movement_state=movement_state,
            authorized_borrow_map=authorized_borrow_map,
        )
        # A complete physical sequence is consumed exactly once. An ignored movement
        # is acquisition noise and intentionally has no business Parking Log.
        group.write({
            "state": "processed",
            "parking_log_id": parking_log.id if parking_log else False,
        })

        # One physical detection is fanned out to every contextual Lane candidate.
        # Once one Lane wins, consume sibling copies so they cannot form a false
        # later movement on another logical Lane.
        source_uids = [uid for uid in group.mapped("event_uid") if uid]
        if source_uids:
            siblings = self.search([
                ("event_uid", "in", source_uids),
                ("id", "not in", group.ids),
                ("state", "=", "pending"),
                ("parking_log_id", "=", False),
            ])
            if siblings:
                siblings.write({
                    "state": "processed",
                    "error_code": "consumed_by_other_lane",
                })
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
            source_events = match["events"]
            if tid in blocked_tids:
                continue

            # Resolve Vehicle identity from the matched sequence, then serialize all
            # decisions for that Vehicle. A different logical Lane may be processing
            # sibling fan-out copies of the same raw Controller events concurrently.
            vehicle_event = source_events.filtered(
                lambda rec: bool(rec.vehicle_id)
            ).sorted(key=lambda rec: (rec.detected_at, rec.id))[-1:]
            vehicle = vehicle_event.vehicle_id if vehicle_event else self.env["nsp.vehicle"].browse()
            if not vehicle:
                source_events.filtered(lambda rec: rec.state == "pending").write({
                    "state": "error",
                    "error_code": "vehicle_not_found",
                })
                continue

            duration = max(0.001, float(match["duration_seconds"] or 0.001))
            ParkingLog._acquire_vehicle_continuity_lock(vehicle)

            # The recordset may have been read before waiting on the Vehicle lock.
            # Refresh state after the lock so another Lane cannot leave us with a
            # stale `pending` cache and cause a second business movement.
            source_events.invalidate_recordset(["state", "parking_log_id"])
            movement_events = source_events.filtered(
                lambda rec: rec.state == "pending" and not rec.parking_log_id
            )
            if not movement_events or len(movement_events) != len(source_events):
                continue

            # One physical Controller event is fanned out to every contextual Lane
            # candidate. If another Lane has already consumed any sibling copy, that
            # Lane won the physical crossing; consume this group without re-running
            # Vehicle state resolution. The (event_uid, layout_lane_id) unique index
            # also serves this prefix lookup efficiently.
            source_uids = [uid for uid in movement_events.mapped("event_uid") if uid]
            sibling_winner = self.search([
                ("event_uid", "in", source_uids),
                ("id", "not in", movement_events.ids),
                ("state", "=", "processed"),
            ], order="parking_log_id desc, id asc", limit=1) if source_uids else self.browse()
            if sibling_winner:
                movement_events.write({
                    "state": "processed",
                    "parking_log_id": sibling_winner.parking_log_id.id
                    if sibling_winner.parking_log_id else False,
                    "error_code": "consumed_by_other_lane",
                })
                continue

            # Suppress a repeated physical crossing before resolving Check-in/Check-out.
            # This is intentionally event-type agnostic: otherwise a duplicate of a
            # Check-in can be misclassified as a Check-out after the first log exists.
            window_start = match["end_at"] - timedelta(seconds=duration)
            recent = ParkingLog.search([
                ("layout_lane_id", "=", lane.id),
                ("layout_revision", "=", layout_revision),
                ("vehicle_id", "=", vehicle.id),
                ("event_time", ">=", window_start),
                ("event_time", "<=", match["end_at"]),
            ], order="event_time desc, id desc", limit=1)
            if recent:
                movement_events.write({"state": "processed", "parking_log_id": recent.id})
                continue

            movement_state = ParkingLog._resolve_vehicle_movement(
                vehicle, match["end_at"], lane.parking_area_id
            )
            if movement_state.get("action") == "ignore":
                movement_events.write({
                    "state": "processed",
                    "parking_log_id": False,
                    "error_code": "stale_movement",
                })
                continue

            event_type = movement_state.get("event_type")
            matched_user_events = self.browse()
            authorized_borrow_map = False
            if event_type == "check_out" and movement_state.get("action") != "deny":
                deadline = match["end_at"] + timedelta(seconds=duration)
                deadline_reached = bool(finalize_expired and now >= deadline)
                candidates = self._user_candidates_from_pool(
                    user_events, match["end_at"], duration, consumed_user_ids
                )
                candidate_users = candidates.mapped("user_id")
                authorized_borrow_map = ParkingLog._authorized_user_borrow_map(
                    vehicle, match["end_at"]
                )
                has_one_authorized_identity = (
                    len(candidate_users) == 1 and candidate_users.id in authorized_borrow_map
                )
                has_ambiguous_identities = len(candidate_users) > 1
                if not (
                    has_one_authorized_identity
                    or has_ambiguous_identities
                    or deadline_reached
                ):
                    blocked_tids.add(tid)
                    continue
                matched_user_events = candidates

            try:
                with self.env.cr.savepoint():
                    parking_log = self._create_log_for_vehicle(
                        movement_events,
                        movement_state,
                        user_events=matched_user_events,
                        authorized_borrow_map=authorized_borrow_map,
                    )
                    logs |= parking_log
                    consumed_user_ids.update(matched_user_events.ids)
            except Exception:
                _logger.exception(
                    "Parking Antenna Sequence processing failed: lane=%s event_type=%s ids=%s",
                    lane.id, event_type, movement_events.ids,
                )
                movement_events.write({
                    "state": "error", "error_code": "processing_error",
                })
                if matched_user_events:
                    matched_user_events.write({
                        "state": "error", "error_code": "processing_error",
                    })
                    consumed_user_ids.update(matched_user_events.ids)

        if finalize_expired:
            self._expire_stale_vehicle_events(lane, now)
            self._expire_orphan_user_events(lane, now)
        return logs

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
            return self.env["nsp.parking.log"].browse()
        # Per-Lane processing only needs to invalidate that Lane. Full Parking
        # Layout reconciliation remains in invalidate_pending_for_runtime_change().
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
        raw_days = self.env["ir.config_parameter"].sudo().get_param(
            "nsp.parking_detection_retention_days", "7"
        )
        try:
            retention_days = max(1, int(raw_days))
        except (TypeError, ValueError):
            retention_days = 7

        terminal_cutoff = fields.Datetime.now() - timedelta(days=retention_days)
        # Detection rows are leaf technical evidence: nothing owns them and Parking
        # Log only exposes the reverse One2many. Delete by indexed keyset batches
        # instead of materializing tens of thousands of ORM records every hour.
        batch_size = 50000
        max_batches = 10
        deleted = 0
        for _batch in range(max_batches):
            self.env.cr.execute(
                """
                WITH doomed AS (
                    SELECT id
                      FROM nsp_parking_detection_event
                     WHERE state IN ('processed', 'error')
                       AND detected_at < %s
                     ORDER BY detected_at ASC, id ASC
                     LIMIT %s
                )
                DELETE FROM nsp_parking_detection_event AS event
                 USING doomed
                 WHERE event.id = doomed.id
                """,
                (terminal_cutoff, batch_size),
            )
            count = self.env.cr.rowcount
            deleted += count
            if count < batch_size:
                break
        if deleted:
            self.invalidate_model()
            self.env["nsp.parking.log"].invalidate_model(["source_detection_ids"])
            _logger.info(
                "Parking Detection cleanup deleted %s terminal rows older than %s",
                deleted, terminal_cutoff,
            )
        return True
