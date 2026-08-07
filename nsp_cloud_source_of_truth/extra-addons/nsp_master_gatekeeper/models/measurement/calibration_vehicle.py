# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class NspMeasurementSessionVehicleScope(models.Model):
    _inherit = "nsp.measurement.session"

    def _sanitize_target_commands(self, commands):
        """Normalize Vehicle scan rows and remove only a completely blank virtual row."""
        if not commands:
            return commands

        Target = self.env["nsp.measurement.target.line"]
        clear_all = any(
            isinstance(command, (list, tuple)) and command and command[0] == 5
            for command in commands
        )
        removed_ids = {
            int(command[1])
            for command in commands
            if isinstance(command, (list, tuple))
            and len(command) > 1
            and command[0] in (2, 3)
            and command[1]
        }
        existing = self.mapped("target_line_ids") if self and not clear_all else Target
        existing = existing.filtered(lambda line: line.id not in removed_ids)
        seen_tags = set(existing.mapped("tag_id").ids)
        seen_vehicles = set(existing.mapped("vehicle_id").ids)

        cleaned = []
        for command in commands:
            if not isinstance(command, (list, tuple)) or not command:
                cleaned.append(command)
                continue
            operation = command[0]
            if operation == 0 and len(command) >= 3:
                values = Target._prepare_vehicle_values(dict(command[2] or {}))
                tag_id = Target._many2one_id(values.get("tag_id"))
                vehicle_id = Target._many2one_id(values.get("vehicle_id"))
                has_input = bool(
                    tag_id
                    or vehicle_id
                    or Target._normalize_scan_tid(values.get("vehicle_scan_tid"))
                )
                if not has_input:
                    continue
                if not tag_id or not vehicle_id:
                    raise ValidationError(_(
                        "Each Vehicle line requires one RFID Tag and one License Plate."
                    ))
                if tag_id in seen_tags:
                    raise ValidationError(_(
                        "The same RFID Tag can be used only once in a Lane Calibration."
                    ))
                if vehicle_id in seen_vehicles:
                    raise ValidationError(_(
                        "The same Vehicle can be used only once in a Lane Calibration."
                    ))
                cleaned.append((0, 0, values))
                seen_tags.add(tag_id)
                seen_vehicles.add(vehicle_id)
                continue

            if operation == 1 and len(command) >= 3:
                values = dict(command[2] or {})
                if {"vehicle_scan_tid", "tag_id", "vehicle_id"}.intersection(values):
                    values = Target._prepare_vehicle_values(values)
                cleaned.append((1, command[1], values))
                continue

            cleaned.append(command)
        return cleaned

    def _allowed_target_tids(self):
        self.ensure_one()
        Tag = self.env["nsp.rfid.tag"]
        return {
            Tag._normalize_tid(tid)
            for tid in self.target_line_ids.mapped("vehicle_tid")
            if tid
        }

    def _validate_vehicle_scope(self):
        Assignment = self.env["nsp.rfid.tag.assignment"].sudo()
        all_targets = self.mapped("target_line_ids")
        all_tag_ids = all_targets.mapped("tag_id").ids
        assignments = Assignment.search([
            ("tag_id", "in", all_tag_ids),
            ("state", "=", "active"),
        ]) if all_tag_ids else Assignment.browse()
        assignment_by_tag = {row.tag_id.id: row for row in assignments}

        for session in self:
            targets = session.target_line_ids
            incomplete = targets.filtered(
                lambda line: not line.tag_id or not line.vehicle_id or not line.license_plate
            )
            if incomplete:
                raise ValidationError(
                    _("Every Vehicle line must contain one RFID Tag and one License Plate.")
                )
            tag_ids = targets.mapped("tag_id").ids
            vehicle_ids = targets.mapped("vehicle_id").ids
            if len(tag_ids) != len(set(tag_ids)):
                raise ValidationError(
                    _("An RFID Tag can be selected only once in a Lane Calibration.")
                )
            if len(vehicle_ids) != len(set(vehicle_ids)):
                raise ValidationError(
                    _("A Vehicle can be selected only once in a Lane Calibration.")
                )
            for target in targets:
                assignment = assignment_by_tag.get(target.tag_id.id)
                if not assignment or assignment.vehicle_id != target.vehicle_id:
                    raise ValidationError(
                        _("Vehicle RFID assignment changed after this calibration Vehicle was created.")
                    )
        return True


class NspMeasurementTargetLine(models.Model):
    """One Vehicle RFID target in a Lane Calibration.

    A scanned TID may already be assigned to a Vehicle. In that case the License
    Plate and existing owner are resolved immediately. When the TID is available,
    the operator can quick-create/select a Vehicle by License Plate; saving the
    line creates the active Vehicle RFID assignment. Vehicle ownership is optional
    for test vehicles and can be assigned by quick-creating/selecting an NSP User.
    """

    _name = "nsp.measurement.target.line"
    _description = "NSP Lane Calibration Vehicle"
    _order = "session_id, license_plate, id"

    session_id = fields.Many2one(
        "nsp.measurement.session",
        required=False,
        ondelete="cascade",
        index=True,
        help=(
            "Assigned automatically when the Reader Assembly is attached to "
            "a Lane Calibration. It may be temporarily empty while editing "
            "a new, unsaved calibration form."
        ),
    )
    vehicle_scan_tid = fields.Char(
        string="RFID Tag", store=False, copy=False,
        help=(
            "Scan or enter a whitelisted TID. If it is already assigned to a Vehicle, "
            "the License Plate and owner are resolved automatically."
        ),
    )
    tag_id = fields.Many2one(
        "nsp.rfid.tag", string="RFID Tag", required=True,
        ondelete="restrict", index=True,
    )
    vehicle_tid = fields.Char(
        related="tag_id.tid", string="RFID TID", readonly=True,
        store=True, index=True,
    )
    vehicle_id = fields.Many2one(
        "nsp.vehicle", string="License Plate", required=True,
        ondelete="restrict", index=True,
        domain=[("active", "=", True)],
    )
    license_plate = fields.Char(
        related="vehicle_id.license_plate", string="License Plate",
        readonly=True, store=True, index=True,
    )
    owner_id = fields.Many2one(
        related="vehicle_id.owner_id", string="Owner",
        readonly=False, store=True,
    )
    owner_locked = fields.Boolean(
        compute="_compute_owner_locked", string="Existing Owner",
    )

    vehicle_detection_state = fields.Selection(
        [("pending", "Not Detected"), ("detected", "Detected")],
        compute="_compute_detection_state", string="Vehicle Status",
    )
    vehicle_detection_count = fields.Integer(
        compute="_compute_detection_state", string="Reads",
    )

    _sql_constraints = [
        (
            "measurement_target_tag_unique",
            "unique(session_id, tag_id)",
            "This RFID Tag is already selected in the Lane Calibration.",
        ),
        (
            "measurement_target_vehicle_unique",
            "unique(session_id, vehicle_id)",
            "This Vehicle is already selected in the Lane Calibration.",
        ),
    ]

    @api.depends("vehicle_id.owner_id")
    def _compute_owner_locked(self):
        for line in self:
            line.owner_locked = bool(line.vehicle_id.owner_id)

    @api.model
    def _normalize_scan_tid(self, value):
        return self.env["nsp.rfid.tag"]._normalize_scan_tid(value)

    @api.model
    def _many2one_id(self, value):
        if isinstance(value, (list, tuple)):
            value = value[0] if value else False
        if isinstance(value, dict):
            value = value.get("id") or value.get("resId")
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    @api.model
    def _resolve_vehicle_from_tid(self, tid, tag_id=False):
        canonical_tid = self._normalize_scan_tid(tid)
        if not canonical_tid:
            return {}
        result = self.env["nsp.rfid.tag"].sudo().nsp_validate_scan(
            canonical_tid,
            require_active_assignment=False,
            expected_target="vehicle_or_available",
        )
        if not result.get("valid") or not result.get("tag_id"):
            raise ValidationError(
                result.get("message") or _("Invalid Vehicle RFID Tag.")
            )
        selected_tag_id = self._many2one_id(tag_id)
        if selected_tag_id and int(result["tag_id"]) != selected_tag_id:
            raise ValidationError(
                _("The scanned RFID Tag does not match the selected whitelist tag.")
            )
        return result

    @api.model
    def _resolve_active_vehicle_tag(self, vehicle):
        vehicle = vehicle.exists()
        if not vehicle:
            raise ValidationError(_("The selected Vehicle no longer exists."))
        vehicle.ensure_one()
        vehicle.check_access("read")
        return self.env["nsp.rfid.tag.assignment"].sudo().active_for_target(vehicle)

    @api.model
    def _apply_vehicle_scan_result(self, values, result):
        prepared = dict(values)
        prepared["tag_id"] = int(result["tag_id"])
        prepared["vehicle_scan_tid"] = result.get("tid") or False
        resolved_vehicle_id = int(result.get("vehicle_id") or 0)
        selected_vehicle_id = self._many2one_id(prepared.get("vehicle_id"))
        if (
            resolved_vehicle_id
            and selected_vehicle_id
            and resolved_vehicle_id != selected_vehicle_id
        ):
            raise ValidationError(
                _("RFID Tag %(tid)s is already assigned to another Vehicle.")
                % {"tid": result.get("tid") or "-"}
            )
        if resolved_vehicle_id and not selected_vehicle_id:
            prepared["vehicle_id"] = resolved_vehicle_id
        return prepared

    @api.model
    def _prepare_vehicle_values(self, vals):
        values = dict(vals)
        selected_tag_id = self._many2one_id(values.get("tag_id"))
        scan_tid = self._normalize_scan_tid(values.get("vehicle_scan_tid"))
        if "vehicle_scan_tid" in values:
            values["vehicle_scan_tid"] = scan_tid or False

        if selected_tag_id or scan_tid:
            tag = self.env["nsp.rfid.tag"].browse(selected_tag_id).exists()
            if selected_tag_id and not tag:
                raise ValidationError(_("The selected RFID Tag no longer exists."))
            if tag:
                tag.check_access("read")
            result = self._resolve_vehicle_from_tid(
                scan_tid or (tag.tid if tag else False),
                tag_id=selected_tag_id,
            )
            values = self._apply_vehicle_scan_result(values, result)

        vehicle_id = self._many2one_id(values.get("vehicle_id"))
        if vehicle_id and not self._many2one_id(values.get("tag_id")):
            vehicle = self.env["nsp.vehicle"].browse(vehicle_id)
            assignment = self._resolve_active_vehicle_tag(vehicle)
            if assignment:
                values["tag_id"] = assignment.tag_id.id
                values["vehicle_scan_tid"] = assignment.tid
        return values

    def _ensure_vehicle_assignment(self):
        lines = self.filtered(lambda row: row.tag_id and row.vehicle_id)
        if not lines:
            return True
        if lines.filtered(lambda row: not row.vehicle_id.active):
            raise ValidationError(_("An archived Vehicle cannot be used in Lane Calibration."))

        Assignment = self.env["nsp.rfid.tag.assignment"].sudo()
        assignments = Assignment.search([
            ("tag_id", "in", lines.mapped("tag_id").ids),
            ("state", "=", "active"),
        ])
        active_by_tag = {assignment.tag_id.id: assignment for assignment in assignments}
        create_values = []
        missing_tags = set()
        for line in lines:
            active = active_by_tag.get(line.tag_id.id)
            if active:
                if active.user_id:
                    raise ValidationError(_(
                        "RFID Tag %(tid)s is assigned to User %(user)s and cannot be used as a Vehicle Tag."
                    ) % {
                        "tid": line.vehicle_tid or line.tag_id.tid,
                        "user": active.user_id.display_name,
                    })
                if active.vehicle_id != line.vehicle_id:
                    raise ValidationError(_(
                        "RFID Tag %(tid)s is already assigned to Vehicle %(vehicle)s."
                    ) % {
                        "tid": line.vehicle_tid or line.tag_id.tid,
                        "vehicle": active.vehicle_id.display_name,
                    })
                continue
            if line.tag_id.id in missing_tags:
                continue
            missing_tags.add(line.tag_id.id)
            create_values.append({
                "tag_id": line.tag_id.id,
                "vehicle_id": line.vehicle_id.id,
            })
        if create_values:
            Assignment.with_context(rfid_audit_user_id=self.env.user.id).create(create_values)
        return True

    @api.depends("session_id.revision", "session_id.event_ids")
    def _compute_detection_state(self):
        session_ids = self.mapped("session_id").ids
        counts = {}
        if session_ids:
            rows = self.env["nsp.measurement.event"].sudo()._read_group(
                [("session_id", "in", session_ids)],
                ["session_id", "revision", "tid"], ["__count"],
            )
            counts = {
                (session.id, int(revision or 1), tid): int(count or 0)
                for session, revision, tid, count in rows
            }
        for line in self:
            count = counts.get((
                line.session_id.id,
                int(line.session_id.revision or 1),
                line.vehicle_tid,
            ), 0)
            line.vehicle_detection_count = count
            line.vehicle_detection_state = "detected" if count else "pending"

    @api.model_create_multi
    def create(self, vals_list):
        prepared = [self._prepare_vehicle_values(vals) for vals in vals_list]
        tag_ids = [self._many2one_id(vals.get("tag_id")) for vals in prepared]
        vehicle_ids = [self._many2one_id(vals.get("vehicle_id")) for vals in prepared]
        if any(not value for value in tag_ids):
            raise ValidationError(_("RFID Tag is required for every calibration Vehicle."))
        if any(not value for value in vehicle_ids):
            raise ValidationError(_("License Plate is required for every calibration Vehicle."))
        if len(tag_ids) != len(set(tag_ids)):
            raise ValidationError(_("The same RFID Tag is entered more than once."))
        if len(vehicle_ids) != len(set(vehicle_ids)):
            raise ValidationError(_("The same Vehicle is entered more than once."))
        records = super().create(prepared)
        records._ensure_vehicle_assignment()
        return records

    def write(self, vals):
        result = super().write(self._prepare_vehicle_values(vals))
        self._ensure_vehicle_assignment()
        return result

    @api.constrains("tag_id", "vehicle_id", "session_id")
    def _check_vehicle_target(self):
        if self.filtered(lambda line: not line.tag_id or not line.vehicle_id):
            raise ValidationError(_("Each Lane Calibration Vehicle requires an RFID Tag and License Plate."))
        assignments = self.env["nsp.rfid.tag.assignment"].sudo().search([
            ("tag_id", "in", self.mapped("tag_id").ids),
            ("state", "=", "active"),
        ])
        active_by_tag = {assignment.tag_id.id: assignment for assignment in assignments}
        for line in self:
            active = active_by_tag.get(line.tag_id.id)
            if active and active.vehicle_id != line.vehicle_id:
                if active.user_id:
                    raise ValidationError(_("The selected RFID Tag is assigned to a User."))
                raise ValidationError(_("The selected RFID Tag is assigned to another Vehicle."))

    @api.onchange("tag_id")
    def _onchange_tag_id(self):
        """Resolve a persisted RFID Tag selection inside the Odoo 19 One2many editor.

        The Vehicles popup stores ``tag_id`` directly.  Keeping this onchange on
        the persistent field makes the row complete before the parent form Save
        is executed and avoids relying on the non-stored scanner helper field.
        """
        for line in self:
            if not line.tag_id:
                line.vehicle_scan_tid = False
                continue
            result = line._resolve_vehicle_from_tid(line.tag_id.tid, tag_id=line.tag_id.id)
            line.vehicle_scan_tid = result.get("tid")
            resolved_vehicle_id = int(result.get("vehicle_id") or 0)
            if resolved_vehicle_id:
                line.vehicle_id = self.env["nsp.vehicle"].browse(resolved_vehicle_id)

    @api.onchange("vehicle_scan_tid")
    def _onchange_vehicle_scan_tid(self):
        for line in self:
            canonical_tid = line._normalize_scan_tid(line.vehicle_scan_tid)
            line.vehicle_scan_tid = canonical_tid or False
            if not canonical_tid:
                line.tag_id = False
                continue
            result = line._resolve_vehicle_from_tid(canonical_tid)
            line.tag_id = self.env["nsp.rfid.tag"].browse(int(result["tag_id"]))
            line.vehicle_scan_tid = result.get("tid")
            if result.get("vehicle_id"):
                line.vehicle_id = self.env["nsp.vehicle"].browse(int(result["vehicle_id"]))

    @api.onchange("vehicle_id")
    def _onchange_vehicle_id(self):
        for line in self:
            if not line.vehicle_id:
                continue
            assignment = line._resolve_active_vehicle_tag(line.vehicle_id)
            if assignment and not line.tag_id:
                line.tag_id = assignment.tag_id
                line.vehicle_scan_tid = assignment.tid
