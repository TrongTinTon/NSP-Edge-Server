# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError
from odoo.addons.nsp_core.utils import new_management_code


def _new_measurement_code():
    return new_management_code("MSR")


class NspMeasurementSession(models.Model):
    """Measurement plan mirrored between Cloud and Edge.

    Cloud owns one Target RFID Tag, one Controller and one-or-more Reader lines.
    Every Reader has its own temporary Measurement power and antenna scope.
    Edge executes the plan; Controller only performs the physical Reader changes.
    """

    _name = "nsp.measurement.session"
    _description = "NSP Measurement Session"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _rec_name = "measurement_code"
    _order = "create_date desc, id desc"

    measurement_code = fields.Char(
        required=True,
        readonly=True,
        copy=False,
        index=True,
        default=lambda self: _new_measurement_code(),
    )
    controller_id = fields.Many2one(
        "nsp.controller",
        required=True,
        ondelete="restrict",
        index=True,
        tracking=True,
        help="Controller that manages all Readers participating in this Measurement Session.",
    )
    target_card_id = fields.Many2one(
        "nsp.rfid.card",
        string="Target RFID Tag",
        ondelete="restrict",
        index=True,
        tracking=True,
        help="Exactly one RFID Tag already registered in NSP. Other Tags are ignored during Measurement.",
    )
    target_tid = fields.Char(
        string="Target TID",
        related="target_card_id.tid",
        readonly=True,
        store=True,
        index=True,
    )
    reader_line_ids = fields.One2many(
        "nsp.measurement.reader.line",
        "session_id",
        string="Measurement Readers",
        copy=True,
    )
    reader_count = fields.Integer(compute="_compute_reader_count")
    revision = fields.Integer(
        string="Revision",
        required=True,
        default=1,
        readonly=True,
        copy=False,
        index=True,
    )
    planned_start_at = fields.Datetime(tracking=True)
    planned_end_at = fields.Datetime(tracking=True)
    started_at = fields.Datetime(readonly=True, copy=False)
    ended_at = fields.Datetime(readonly=True, copy=False)
    applied_at = fields.Datetime(readonly=True, copy=False)
    note = fields.Text()
    status = fields.Selection(
        [
            ("draft", "Draft"),
            ("ready", "Ready"),
            ("running", "Running"),
            ("completed", "Completed"),
            ("applied", "Applied to Operation"),
            ("failed", "Failed"),
            ("cancelled", "Cancelled"),
        ],
        required=True,
        default="draft",
        index=True,
        tracking=True,
    )
    event_ids = fields.One2many(
        "nsp.measurement.event",
        "session_id",
        string="Measurement Observations",
        readonly=True,
    )
    event_count = fields.Integer(compute="_compute_event_count")

    _sql_constraints = [
        (
            "measurement_code_unique",
            "unique(measurement_code)",
            "Measurement Code must be unique.",
        ),
        (
            "measurement_revision_positive",
            "CHECK(revision > 0)",
            "Measurement Revision must be greater than zero.",
        ),
    ]

    def _deployment_role(self):
        role = str(
            self.env["ir.config_parameter"].sudo().get_param("nsp.deployment_role")
            or "edge_server"
        ).strip().lower()
        return role if role in ("cloud", "edge_server") else "edge_server"

    @api.depends("reader_line_ids")
    def _compute_reader_count(self):
        for session in self:
            session.reader_count = len(session.reader_line_ids)

    @api.depends("event_ids", "revision")
    def _compute_event_count(self):
        ids = [record.id for record in self if record.id]
        counts = {}
        if ids:
            rows = self.env["nsp.measurement.event"].sudo()._read_group(
                [("session_id", "in", ids)],
                ["session_id", "revision"],
                ["__count"],
            )
            counts = {(session.id, revision): count for session, revision, count in rows}
        for session in self:
            session.event_count = counts.get((session.id, session.revision), 0)

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for source in vals_list:
            vals = dict(source)
            vals["measurement_code"] = str(
                vals.get("measurement_code") or _new_measurement_code()
            ).strip().upper()
            vals["revision"] = max(int(vals.get("revision") or 1), 1)
            if not self.env.context.get("measurement_sync"):
                vals["status"] = "draft"
            prepared.append(vals)
        records = super().create(prepared)
        for record in records:
            record._validate_measurement_scope()
        return records

    def write(self, vals):
        values = dict(vals)
        configuration_fields = {
            "measurement_code",
            "controller_id",
            "target_card_id",
            "reader_line_ids",
            "planned_start_at",
            "planned_end_at",
            "note",
        }
        if configuration_fields.intersection(values) and not self.env.context.get("measurement_sync"):
            protected = self.filtered(lambda session: session.status not in ("draft", "completed"))
            if protected:
                raise ValidationError(
                    _("Measurement configuration can be edited only while Draft or after completion before Measure Again.")
                )
        if "measurement_code" in values:
            values["measurement_code"] = str(values.get("measurement_code") or "").strip().upper()
        result = super().write(values)
        if {"controller_id", "reader_line_ids"}.intersection(values):
            self._validate_measurement_scope()
        return result

    @api.constrains("planned_start_at", "planned_end_at")
    def _check_planned_time(self):
        for session in self:
            if (
                session.planned_start_at
                and session.planned_end_at
                and session.planned_end_at <= session.planned_start_at
            ):
                raise ValidationError(_("Planned end time must be later than planned start time."))

    @api.constrains("controller_id", "reader_line_ids")
    def _check_scope_constraint(self):
        self._validate_measurement_scope()

    def _validate_measurement_scope(self):
        for session in self:
            seen = set()
            for line in session.reader_line_ids:
                if line.reader_id.controller_id != session.controller_id:
                    raise ValidationError(_("Every selected Reader must belong to the selected Controller."))
                if line.reader_id.id in seen:
                    raise ValidationError(_("A Reader can be selected only once in a Measurement Session."))
                seen.add(line.reader_id.id)
                invalid = line.antenna_ids.filtered(lambda antenna: antenna.device_id != line.reader_id)
                if invalid:
                    raise ValidationError(_("Every Measurement Antenna must belong to its Reader."))
        return True

    def _require_ready_configuration(self):
        self.ensure_one()
        missing = []
        if not self.target_card_id:
            missing.append(_("Target RFID Tag"))
        if not self.reader_line_ids:
            missing.append(_("Measurement Readers"))
        if missing:
            raise ValidationError(_("Missing Measurement configuration: %s") % ", ".join(missing))
        readers_without_ports = self.reader_line_ids.filtered(
            lambda line: line.reader_id and not line.reader_id.antennas_ids
        )
        if readers_without_ports:
            names = ", ".join(readers_without_ports.mapped("reader_id.display_name"))
            raise ValidationError(
                _("The following Reader(s) have no configured Antennas: %s. Configure antenna ports on the Reader first.")
                % names
            )
        missing_antennas = self.reader_line_ids.filtered(lambda line: not line.antenna_ids)
        if missing_antennas:
            names = ", ".join(missing_antennas.mapped("reader_id.display_name"))
            raise ValidationError(
                _("Select at least one Measurement Antenna for each Reader. Missing: %s") % names
            )
        self._validate_measurement_scope()

    def _allowed_antenna_pairs(self):
        self.ensure_one()
        return {
            (line.reader_id.serial_number, int(antenna.antenna_no or 0))
            for line in self.reader_line_ids
            for antenna in line.antenna_ids
        }

    def _measurement_line_for_serial(self, serial_number):
        self.ensure_one()
        serial = str(serial_number or "").strip().upper()
        return self.reader_line_ids.filtered(
            lambda line: (line.reader_id.serial_number or "").strip().upper() == serial
        )[:1]

    def _measurement_power_for_serial(self, serial_number):
        self.ensure_one()
        line = self._measurement_line_for_serial(serial_number)
        return int(line.measurement_power_dbm or 0) if line else 0

    def action_ready(self):
        for session in self:
            if session.status == "ready":
                continue
            if session.status != "draft":
                raise ValidationError(_("Only draft Measurement Sessions can be released."))
            session._require_ready_configuration()
            session.with_context(measurement_sync=True).write({"status": "ready"})
        return True

    def action_complete(self):
        for session in self:
            if session.status == "completed":
                continue
            if session.status != "running":
                raise ValidationError(_("Only running Measurement Sessions can be completed."))
            session.with_context(measurement_sync=True).write(
                {"status": "completed", "ended_at": fields.Datetime.now()}
            )
        return True

    def action_cancel(self):
        for session in self:
            if session.status in ("completed", "applied", "failed", "cancelled"):
                raise ValidationError(_("This Measurement Session can no longer be cancelled."))
            session.with_context(measurement_sync=True).write(
                {"status": "cancelled", "ended_at": fields.Datetime.now()}
            )
        return True

    def action_measure_again(self, reader_powers=None):
        """Release a new revision using one temporary power value per Reader."""
        self.ensure_one()
        if self.status not in ("running", "completed", "failed"):
            raise ValidationError(_("Measure Again is available for running, completed, or failed sessions."))
        self._require_ready_configuration()

        if reader_powers not in (None, False, ""):
            if not isinstance(reader_powers, list):
                raise ValidationError(_("Reader power configuration must be a list."))
            line_by_id = {line.id: line for line in self.reader_line_ids}
            seen = set()
            for item in reader_powers:
                if not isinstance(item, dict):
                    raise ValidationError(_("Invalid Reader power configuration."))
                try:
                    line_id = int(item.get("reader_line_id") or 0)
                    power = int(item.get("power_dbm"))
                except Exception as exc:
                    raise ValidationError(_("Invalid Reader power configuration.")) from exc
                line = line_by_id.get(line_id)
                if not line or line_id in seen:
                    raise ValidationError(_("Reader power configuration does not match this Measurement Session."))
                if power < 0 or power > 40:
                    raise ValidationError(_("Measurement Power must be between 0 and 40 dBm."))
                seen.add(line_id)
                line.with_context(measurement_sync=True).write({"measurement_power_dbm": power})

        self.with_context(measurement_sync=True).write({
            "revision": self.revision + 1,
            "status": "ready",
            "started_at": False,
            "ended_at": False,
            "applied_at": False,
        })
        return self.get_live_snapshot(self.id)

    def action_apply_to_operation(self):
        """Promote validated temporary powers to each Reader operation profile."""
        self.ensure_one()
        if self._deployment_role() != "cloud":
            raise UserError(_("Apply to Operation is owned by the Cloud Master."))
        if self.status != "completed":
            raise ValidationError(_("Complete the Measurement before applying it to operation."))
        self._require_ready_configuration()
        # The Measurement action is the Cloud-Master approval boundary.  A user
        # who may operate Measurement does not necessarily have direct write ACL
        # on Reader master records, therefore promote the already validated
        # values with sudo instead of leaking Reader ACL details into the UI.
        # Business authorization is still enforced above (Cloud role + completed
        # session + valid Measurement scope).
        for line in self.reader_line_ids:
            line.reader_id.sudo().write({"power_dbm": int(line.measurement_power_dbm or 0)})

        self.with_context(measurement_sync=True).sudo().write({
            "status": "applied",
            "applied_at": fields.Datetime.now(),
        })
        return self.get_live_snapshot(self.id)

    def action_view_events(self):
        self.ensure_one()
        action = self.env.ref("nsp_master_gatekeeper.action_nsp_measurement_event").read()[0]
        action["domain"] = [("session_id", "=", self.id)]
        action["context"] = {
            "search_default_session_id": self.id,
            "search_default_group_revision": 1,
        }
        return action

    def action_open_live(self):
        self.ensure_one()
        return {
            "type": "ir.actions.client",
            "name": _("Cloud Live Measurement") if self._deployment_role() == "cloud" else _("Live Measurement"),
            "tag": "nsp_measurement_live",
            "params": {"session_id": self.id},
            "context": {
                **dict(self.env.context),
                "active_id": self.id,
                "active_ids": [self.id],
                "active_model": self._name,
                "default_session_id": self.id,
            },
        }

    @api.model
    def get_live_snapshot(self, session_id, last_event_id=0, limit=500):
        """Return Live Measurement state for all Readers in the current revision."""
        session = self.sudo().browse(int(session_id or 0)).exists()
        if not session:
            return {"found": False}
        try:
            limit = min(max(int(limit or 500), 20), 1000)
        except Exception:
            limit = 500

        target_tid = str(session.target_tid or "").strip().upper()
        domain = [
            ("session_id", "=", session.id),
            ("revision", "=", session.revision),
        ]
        if target_tid:
            domain.append(("tid", "=", target_tid))
        events = self.env["nsp.measurement.event"].sudo().search(
            domain,
            order="read_at asc, read_at_ms asc, id asc",
            limit=limit,
        )
        steps = session._build_detection_steps(events)
        first_step = steps[0] if steps else False
        last_step = steps[-1] if steps else False
        edge = session.controller_id.edge_server_id
        card = session.target_card_id
        role = session._deployment_role()
        readers = []
        for line in session.reader_line_ids.sorted(
            key=lambda item: ((item.reader_id.name or ""), (item.reader_id.serial_number or ""), item.id)
        ):
            reader = line.reader_id
            readers.append({
                "reader_line_id": line.id,
                "id": reader.id,
                "name": reader.name or reader.serial_number or "",
                "serial_number": reader.serial_number or "",
                "status": reader.status or "",
                "operation_power_dbm": int(reader.power_dbm or 0),
                "measurement_power_dbm": int(line.measurement_power_dbm or 0),
                "firmware_version": reader.firmware_version or "",
                "antennas": sorted(line.antenna_ids.mapped("antenna_no")),
            })
        return {
            "found": True,
            "session_id": session.id,
            "deployment_role": role,
            "measurement_code": session.measurement_code,
            "revision": int(session.revision or 1),
            "status": session.status,
            "controller_code": session.controller_id.controller_id,
            "controller_name": session.controller_id.controller_name or "",
            "edge_server_code": edge.edge_server_code if edge else "",
            "edge_status": edge.status if edge else "",
            "edge_last_seen": fields.Datetime.to_string(edge.timestamp) if edge and edge.timestamp else None,
            "target_tag": {
                "id": card.id if card else False,
                "tid": card.tid if card else "",
                "card_type": card.card_type if card else "",
                "assigned_to": card.assigned_to if card else "",
            },
            "readers": readers,
            "reader_count": len(readers),
            "started_at": fields.Datetime.to_string(session.started_at) if session.started_at else None,
            "ended_at": fields.Datetime.to_string(session.ended_at) if session.ended_at else None,
            "applied_at": fields.Datetime.to_string(session.applied_at) if session.applied_at else None,
            "planned_start_at": fields.Datetime.to_string(session.planned_start_at) if session.planned_start_at else None,
            "planned_end_at": fields.Datetime.to_string(session.planned_end_at) if session.planned_end_at else None,
            "note": session.note or "",
            "raw_event_count": int(session.event_count or 0),
            "detection_count": len(steps),
            "unique_antennas": len({(step["serial_number"], step["antenna_no"]) for step in steps}),
            "unique_readers": len({step["serial_number"] for step in steps}),
            "first_detection": first_step,
            "last_detection": last_step,
            "steps": steps,
            "last_event_id": max(events.ids or [int(last_event_id or 0)]),
            "server_time": fields.Datetime.to_string(fields.Datetime.now()),
        }

    def _event_timestamp(self, event):
        self.ensure_one()
        if not event.read_at:
            return None
        base = fields.Datetime.to_string(event.read_at).replace(" ", "T")
        return "%s.%03dZ" % (base, int(event.read_at_ms or 0))

    def _build_detection_steps(self, events):
        """Collapse consecutive reads on the same Reader/Antenna into timeline steps."""
        self.ensure_one()
        lines = {
            (line.reader_id.serial_number or "").strip().upper(): line
            for line in self.reader_line_ids
        }
        steps = []
        current = None
        for event in events:
            key = (event.serial_number, int(event.antenna_no or 0))
            rssi = None if event.rssi_dbm in (False, None) else float(event.rssi_dbm)
            if current and current["_key"] == key:
                current["last_seen_at"] = self._event_timestamp(event)
                current["read_count"] += 1
                current["last_rssi_dbm"] = rssi
                if rssi is not None:
                    current["peak_rssi_dbm"] = rssi if current["peak_rssi_dbm"] is None else max(current["peak_rssi_dbm"], rssi)
                continue
            if current:
                current.pop("_key", None)
                steps.append(current)
            line = lines.get((event.serial_number or "").strip().upper())
            current = {
                "_key": key,
                "sequence_no": len(steps) + 1,
                "first_seen_at": self._event_timestamp(event),
                "last_seen_at": self._event_timestamp(event),
                "controller_code": self.controller_id.controller_id,
                "serial_number": event.serial_number,
                "reader_name": line.reader_id.name if line else event.serial_number,
                "antenna_no": int(event.antenna_no or 0),
                "power_dbm": int(
                    event.power_dbm
                    if event.power_dbm is not None
                    else (line.measurement_power_dbm if line else 0)
                ),
                "read_count": 1,
                "first_rssi_dbm": rssi,
                "last_rssi_dbm": rssi,
                "peak_rssi_dbm": rssi,
            }
        if current:
            current.pop("_key", None)
            steps.append(current)
        for index, step in enumerate(steps, start=1):
            step["sequence_no"] = index
        return steps

    def _antenna_summary(self):
        """Build current-revision antenna statistics without storing derived rows."""
        self.ensure_one()
        domain = [("session_id", "=", self.id), ("revision", "=", self.revision)]
        if self.target_tid:
            domain.append(("tid", "=", self.target_tid))
        rows = self.env["nsp.measurement.event"].sudo()._read_group(
            domain,
            ["serial_number", "antenna_no"],
            [
                "__count",
                "rssi_dbm:count",
                "rssi_dbm:min",
                "rssi_dbm:avg",
                "rssi_dbm:max",
                "read_at:min",
                "read_at:max",
            ],
            order="serial_number, antenna_no",
        )
        return [
            {
                "serial_number": serial_number,
                "antenna_no": int(antenna_no or 0),
                "read_count": int(count or 0),
                "rssi_sample_count": int(rssi_count or 0),
                "min_rssi_dbm": min_rssi,
                "average_rssi_dbm": avg_rssi,
                "max_rssi_dbm": max_rssi,
                "first_read_at": first_read,
                "last_read_at": last_read,
            }
            for (
                serial_number,
                antenna_no,
                count,
                rssi_count,
                min_rssi,
                avg_rssi,
                max_rssi,
                first_read,
                last_read,
            ) in rows
        ]

    @api.model
    def cron_cleanup_expired_measurements(self):
        value = self.env["ir.config_parameter"].sudo().get_param(
            "nsp_master_gatekeeper.measurement_retention_days", "7"
        )
        try:
            retention_days = max(int(value), 1)
        except Exception:
            retention_days = 7
        cutoff = fields.Datetime.now() - timedelta(days=retention_days)
        events = self.env["nsp.measurement.event"].sudo().search(
            [
                ("session_id.status", "in", ["completed", "applied", "failed", "cancelled"]),
                ("read_at", "<", cutoff),
            ],
            limit=5000,
        )
        count = len(events)
        if events and "nsp.sync.record" in self.env.registry.models:
            self.env["nsp.sync.record"].sudo().search([
                ("record_model", "=", "nsp.measurement.event"),
                ("record_key", "in", events.mapped("event_uid")),
            ]).unlink()
        events.unlink()
        return count


class NspMeasurementReaderLine(models.Model):
    """One Reader participating in a Measurement Session."""

    _name = "nsp.measurement.reader.line"
    _description = "NSP Measurement Reader"
    _order = "session_id, id"

    session_id = fields.Many2one(
        "nsp.measurement.session",
        required=True,
        ondelete="cascade",
        index=True,
    )
    reader_id = fields.Many2one(
        "nsp.device",
        string="Reader",
        required=True,
        ondelete="restrict",
        index=True,
    )
    serial_number = fields.Char(related="reader_id.serial_number", readonly=True)
    reader_status = fields.Selection(related="reader_id.status", readonly=True)
    operation_power_dbm = fields.Integer(related="reader_id.power_dbm", readonly=True)
    measurement_power_dbm = fields.Integer(
        string="Measurement Power (dBm)",
        default=30,
        required=True,
    )
    available_antenna_ids = fields.Many2many(
        "nsp.device.antenna",
        string="Available Antennas",
        compute="_compute_available_antennas",
        readonly=True,
    )
    available_antenna_count = fields.Integer(
        string="Available Antennas",
        compute="_compute_available_antennas",
        readonly=True,
    )
    antenna_ids = fields.Many2many(
        "nsp.device.antenna",
        "nsp_measurement_reader_antenna_rel",
        "reader_line_id",
        "antenna_id",
        string="Measurement Antennas",
        help="Antenna ports used by this Reader during Measurement. Selecting a Reader defaults to all antennas configured on that Reader; remove any ports that should not participate.",
    )

    _sql_constraints = [
        (
            "measurement_reader_unique",
            "unique(session_id, reader_id)",
            "A Reader can be selected only once in a Measurement Session.",
        ),
        (
            "measurement_reader_power_range",
            "CHECK(measurement_power_dbm >= 0 AND measurement_power_dbm <= 40)",
            "Measurement Power must be between 0 and 40 dBm.",
        ),
    ]

    @api.depends("reader_id", "reader_id.antennas_ids")
    def _compute_available_antennas(self):
        for line in self:
            antennas = line.reader_id.antennas_ids if line.reader_id else self.env["nsp.device.antenna"]
            line.available_antenna_ids = antennas
            line.available_antenna_count = len(antennas)

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for source in vals_list:
            vals = dict(source)
            reader = self.env["nsp.device"].browse(vals.get("reader_id")).exists()
            if reader and vals.get("measurement_power_dbm") in (None, False, ""):
                vals["measurement_power_dbm"] = int(reader.power_dbm or 30)
            # New Reader lines default to every antenna already configured on that
            # physical Reader. Users may remove ports they do not want to measure.
            if reader and "antenna_ids" not in vals:
                vals["antenna_ids"] = [(6, 0, reader.antennas_ids.ids)]
            prepared.append(vals)
        records = super().create(prepared)
        records._validate_line_scope()
        return records

    def write(self, vals):
        if not self.env.context.get("measurement_sync"):
            protected = self.filtered(lambda line: line.session_id.status not in ("draft", "completed"))
            if protected:
                raise ValidationError(
                    _("Measurement Readers can be edited only while Draft or after completion before Measure Again.")
                )
        values = dict(vals)
        if "reader_id" in values and "antenna_ids" not in values:
            reader = self.env["nsp.device"].browse(values.get("reader_id")).exists()
            values["antenna_ids"] = [(6, 0, reader.antennas_ids.ids if reader else [])]
            if reader and values.get("measurement_power_dbm") in (None, False, ""):
                values["measurement_power_dbm"] = int(reader.power_dbm or 30)
        result = super().write(values)
        self._validate_line_scope()
        return result

    def unlink(self):
        if not self.env.context.get("measurement_sync"):
            protected = self.filtered(lambda line: line.session_id.status not in ("draft", "completed"))
            if protected:
                raise ValidationError(
                    _("Measurement Readers can be removed only while Draft or after completion before Measure Again.")
                )
        return super().unlink()

    @api.onchange("reader_id")
    def _onchange_reader_id(self):
        for line in self:
            if not line.reader_id:
                line.antenna_ids = [(5, 0, 0)]
                continue
            line.measurement_power_dbm = int(line.reader_id.power_dbm or 30)
            # Make antenna selection immediately usable in the Measurement form.
            # All configured ports are selected by default and can be removed.
            line.antenna_ids = [(6, 0, line.reader_id.antennas_ids.ids)]

    @api.constrains("reader_id", "antenna_ids", "measurement_power_dbm", "session_id")
    def _check_line_scope(self):
        self._validate_line_scope()

    def _validate_line_scope(self):
        for line in self:
            if line.measurement_power_dbm < 0 or line.measurement_power_dbm > 40:
                raise ValidationError(_("Measurement Power must be between 0 and 40 dBm."))
            if line.session_id.controller_id and line.reader_id.controller_id != line.session_id.controller_id:
                raise ValidationError(_("The selected Reader must belong to the Measurement Controller."))
            invalid = line.antenna_ids.filtered(lambda antenna: antenna.device_id != line.reader_id)
            if invalid:
                raise ValidationError(_("Every Measurement Antenna must belong to its Reader."))
        return True


class NspMeasurementEvent(models.Model):
    """One target-Tag RFID observation captured during a Measurement revision."""

    _name = "nsp.measurement.event"
    _description = "NSP Measurement Observation"
    _rec_name = "event_uid"
    _order = "read_at desc, id desc"

    event_uid = fields.Char(required=True, copy=False, index=True)
    session_id = fields.Many2one(
        "nsp.measurement.session",
        required=True,
        ondelete="cascade",
        index=True,
    )
    revision = fields.Integer(required=True, default=1, index=True)
    serial_number = fields.Char(required=True, index=True)
    antenna_no = fields.Integer(required=True, index=True)
    tid = fields.Char(required=True, index=True)
    read_at = fields.Datetime(required=True, index=True)
    read_at_ms = fields.Integer(string="Millisecond", required=True, default=0)
    rssi_dbm = fields.Float()
    power_dbm = fields.Integer(string="Measurement Power (dBm)")

    _sql_constraints = [
        (
            "measurement_event_uid_unique",
            "unique(event_uid)",
            "Measurement Event UID must be unique.",
        ),
        (
            "measurement_event_antenna_positive",
            "CHECK(antenna_no > 0)",
            "Antenna number must be greater than zero.",
        ),
        (
            "measurement_event_revision_positive",
            "CHECK(revision > 0)",
            "Measurement Revision must be greater than zero.",
        ),
        (
            "measurement_event_ms_range",
            "CHECK(read_at_ms >= 0 AND read_at_ms <= 999)",
            "Measurement millisecond must be between 0 and 999.",
        ),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for source in vals_list:
            vals = dict(source)
            vals["event_uid"] = str(vals.get("event_uid") or "").strip()
            vals["serial_number"] = str(vals.get("serial_number") or "").strip().upper()
            vals["tid"] = str(vals.get("tid") or "").strip().upper().replace(" ", "")
            vals["revision"] = max(int(vals.get("revision") or 1), 1)
            vals["read_at_ms"] = max(0, min(int(vals.get("read_at_ms") or 0), 999))
            prepared.append(vals)
        return super().create(prepared)

    def init(self):
        self.env.cr.execute(
            """
            CREATE INDEX IF NOT EXISTS nsp_measurement_event_session_revision_read_idx
                ON nsp_measurement_event (session_id, revision, read_at, read_at_ms, id)
            """
        )

    @api.constrains("session_id", "serial_number", "antenna_no", "tid")
    def _check_event_scope(self):
        for event in self:
            session = event.session_id
            key = (event.serial_number, int(event.antenna_no or 0))
            if key not in session._allowed_antenna_pairs():
                raise ValidationError(_("Measurement observation antenna is not part of the Measurement Session."))
            if session.target_tid and event.tid != session.target_tid:
                raise ValidationError(_("Only the selected Target RFID Tag may be stored in this Measurement Session."))
