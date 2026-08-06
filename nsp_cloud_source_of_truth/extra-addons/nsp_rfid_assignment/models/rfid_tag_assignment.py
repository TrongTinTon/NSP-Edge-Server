from psycopg2 import IntegrityError, errorcodes

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


_TARGET_FIELDS = {
    "nsp.user": "user_id",
    "nsp.vehicle": "vehicle_id",
}
_REVOKE_FIELDS = {"state", "revoked_at", "revoked_by_id"}
_COMPUTED_FIELDS = {"tid", "target_type", "target_code", "target_name"}
_ACTIVE_INDEX_DDL = (
    """
    CREATE UNIQUE INDEX IF NOT EXISTS nsp_rfid_assignment_active_tag_uniq
        ON nsp_rfid_tag_assignment (tag_id)
     WHERE state = 'active'
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS nsp_rfid_assignment_active_user_uniq
        ON nsp_rfid_tag_assignment (user_id)
     WHERE state = 'active' AND user_id IS NOT NULL
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS nsp_rfid_assignment_active_vehicle_uniq
        ON nsp_rfid_tag_assignment (vehicle_id)
     WHERE state = 'active' AND vehicle_id IS NOT NULL
    """,
)


class NspRfidTagAssignment(models.Model):
    _name = "nsp.rfid.tag.assignment"
    _description = "NSP RFID Tag Assignment"
    _order = "assigned_at desc, id desc"

    tag_id = fields.Many2one(
        "nsp.rfid.tag",
        required=True,
        ondelete="restrict",
        index=True,
    )
    tid = fields.Char(related="tag_id.tid", store=True, readonly=True, index=True)
    user_id = fields.Many2one("nsp.user", ondelete="restrict", index=True)
    vehicle_id = fields.Many2one("nsp.vehicle", ondelete="restrict", index=True)
    target_type = fields.Selection(
        [("user", "User"), ("vehicle", "Vehicle")],
        compute="_compute_target",
        store=True,
        index=True,
    )
    target_code = fields.Char(compute="_compute_target", store=True, index=True)
    target_name = fields.Char(compute="_compute_target", store=True)
    state = fields.Selection(
        [("active", "Active"), ("revoked", "Revoked")],
        required=True,
        readonly=True,
        default="active",
        index=True,
    )
    assigned_at = fields.Datetime(
        required=True,
        readonly=True,
        default=fields.Datetime.now,
        index=True,
    )
    assigned_by_id = fields.Many2one(
        "res.users",
        required=True,
        readonly=True,
        default=lambda self: self.env.user,
        ondelete="restrict",
    )
    revoked_at = fields.Datetime(readonly=True, index=True)
    revoked_by_id = fields.Many2one(
        "res.users",
        readonly=True,
        ondelete="restrict",
    )

    _exactly_one_target = models.Constraint(
        "CHECK ((user_id IS NOT NULL) <> (vehicle_id IS NOT NULL))",
        "An RFID Tag must be assigned to exactly one User or one Vehicle.",
    )

    def init(self):
        for statement in _ACTIVE_INDEX_DDL:
            self.env.cr.execute(statement)

    @api.depends(
        "user_id.user_code",
        "user_id.name",
        "vehicle_id.vehicle_code",
        "vehicle_id.license_plate",
    )
    def _compute_target(self):
        for assignment in self:
            if assignment.user_id:
                assignment.target_type = "user"
                assignment.target_code = assignment.user_id.user_code
                assignment.target_name = assignment.user_id.display_name
            elif assignment.vehicle_id:
                assignment.target_type = "vehicle"
                assignment.target_code = assignment.vehicle_id.vehicle_code
                assignment.target_name = assignment.vehicle_id.display_name
            else:
                assignment.target_type = False
                assignment.target_code = False
                assignment.target_name = False

    @api.depends("tid", "target_name")
    def _compute_display_name(self):
        for assignment in self:
            assignment.display_name = "%s → %s" % (
                assignment.tid or _("RFID Tag"),
                assignment.target_name or _("Unknown Target"),
            )

    @api.model
    def _actor_id(self):
        return int(self.env.context.get("rfid_audit_user_id") or self.env.user.id)

    @api.model
    def _resolve_create_values(self, values):
        tag = self.env["nsp.rfid.tag"].sudo().browse(
            int(values.get("tag_id") or 0)
        ).exists()
        user = self.env["nsp.user"].sudo().browse(
            int(values.get("user_id") or 0)
        ).exists()
        vehicle = self.env["nsp.vehicle"].sudo().browse(
            int(values.get("vehicle_id") or 0)
        ).exists()

        if not tag:
            raise ValidationError(_("RFID Tag is required."))
        if bool(user) == bool(vehicle):
            raise ValidationError(
                _("An RFID Tag must be assigned to exactly one User or one Vehicle.")
            )

        target = user or vehicle
        if not target.active:
            raise ValidationError(_("An archived target cannot receive an RFID Tag."))
        return tag, target, "user_id" if user else "vehicle_id"

    @api.model
    def _validate_create_batch(self, resolved_rows):
        reserved_tags = set()
        reserved_targets = set()
        tag_ids = set()
        user_ids = set()
        vehicle_ids = set()

        for tag, target, target_field in resolved_rows:
            target_key = (target_field, target.id)
            if tag.id in reserved_tags:
                raise ValidationError(_("RFID Tag %s is already assigned.") % tag.tid)
            if target_key in reserved_targets:
                raise ValidationError(
                    _("The selected target already has an active RFID Tag.")
                )
            reserved_tags.add(tag.id)
            reserved_targets.add(target_key)
            tag_ids.add(tag.id)
            if target_field == "user_id":
                user_ids.add(target.id)
            else:
                vehicle_ids.add(target.id)

        conflicts = self.sudo().search(
            [
                ("state", "=", "active"),
                "|",
                "|",
                ("tag_id", "in", list(tag_ids)),
                ("user_id", "in", list(user_ids) or [0]),
                ("vehicle_id", "in", list(vehicle_ids) or [0]),
            ]
        )
        active_tag_ids = set(conflicts.mapped("tag_id").ids)
        active_user_ids = set(conflicts.mapped("user_id").ids)
        active_vehicle_ids = set(conflicts.mapped("vehicle_id").ids)

        for tag, target, target_field in resolved_rows:
            if tag.id in active_tag_ids:
                raise ValidationError(_("RFID Tag %s is already assigned.") % tag.tid)
            target_conflicts = (
                active_user_ids if target_field == "user_id" else active_vehicle_ids
            )
            if target.id in target_conflicts:
                raise ValidationError(
                    _("The selected target already has an active RFID Tag.")
                )

    @staticmethod
    def _is_unique_violation(error):
        return getattr(error, "pgcode", None) == errorcodes.UNIQUE_VIOLATION

    @api.model_create_multi
    def create(self, vals_list):
        assigned_at = fields.Datetime.now()
        actor_id = self._actor_id()
        prepared = []
        resolved_rows = []

        for source in vals_list:
            values = dict(source)
            values.update(
                {
                    "state": "active",
                    "assigned_at": assigned_at,
                    "assigned_by_id": actor_id,
                    "revoked_at": False,
                    "revoked_by_id": False,
                }
            )
            resolved_rows.append(self._resolve_create_values(values))
            prepared.append(values)

        self._validate_create_batch(resolved_rows)
        try:
            with self.env.cr.savepoint():
                assignments = super().create(prepared)
        except IntegrityError as error:
            if not self._is_unique_violation(error):
                raise
            raise ValidationError(
                _("The RFID Tag or target already has an active assignment.")
            ) from error

        assignments._post_audit_message(_("RFID Tag assigned"), actor_id)
        return assignments

    def write(self, vals):
        values = dict(vals)
        if not values:
            return True
        if not self.env.context.get("rfid_assignment_revoke"):
            if set(values).issubset(_COMPUTED_FIELDS):
                return super().write(values)
            raise UserError(
                _(
                    "RFID assignments are immutable. Revoke the assignment and "
                    "assign the Tag again."
                )
            )

        unexpected_fields = set(values) - _REVOKE_FIELDS
        valid_transition = (
            not unexpected_fields
            and values.get("state") == "revoked"
            and bool(values.get("revoked_at"))
            and bool(values.get("revoked_by_id"))
            and all(assignment.state == "active" for assignment in self)
        )
        if not valid_transition:
            raise UserError(
                _("RFID assignments can only transition from Active to Revoked.")
            )
        return super().write(values)

    def unlink(self):
        if self.env.context.get("module_uninstall"):
            return super().unlink()
        raise ValidationError(
            _(
                "RFID assignment history cannot be deleted. "
                "Revoke the active assignment instead."
            )
        )

    @api.constrains("user_id", "vehicle_id", "state")
    def _check_active_target(self):
        for assignment in self:
            target = assignment.user_id or assignment.vehicle_id
            if assignment.state == "active" and target and not target.active:
                raise ValidationError(
                    _("An archived target cannot have an active RFID Tag.")
                )

    def _post_audit_message(self, title, actor_id):
        actor = self.env["res.users"].sudo().browse(actor_id).exists()
        author_id = actor.partner_id.id if actor and actor.partner_id else False
        for assignment in self:
            target = assignment.user_id or assignment.vehicle_id
            if target:
                target.sudo().message_post(
                    body=_("%(title)s: %(tid)s")
                    % {"title": title, "tid": assignment.tid},
                    subtype_xmlid="mail.mt_note",
                    author_id=author_id,
                )

    def action_revoke(self):
        active_assignments = self.filtered(lambda record: record.state == "active")
        if not active_assignments:
            return True

        actor_id = self._actor_id()
        active_assignments.with_context(rfid_assignment_revoke=True).write(
            {
                "state": "revoked",
                "revoked_at": fields.Datetime.now(),
                "revoked_by_id": actor_id,
            }
        )
        active_assignments._post_audit_message(_("RFID Tag revoked"), actor_id)
        return True

    @api.model
    def active_for_tid(self, tid):
        normalized = self.env["nsp.rfid.tag"]._normalize_tid(tid)
        if not normalized:
            return self.browse()
        return self.sudo().search(
            [("tid", "=", normalized), ("state", "=", "active")],
            limit=1,
        )

    @api.model
    def active_for_target(self, target):
        target_field = _TARGET_FIELDS.get(getattr(target, "_name", None))
        if not target_field or not target:
            return self.browse()
        target.ensure_one()
        if not isinstance(target.id, int):
            return self.browse()
        return self.sudo().search(
            [(target_field, "=", target.id), ("state", "=", "active")],
            limit=1,
        )

    @api.model
    def _raise_assignment_conflict(self, target, tid):
        current = self.active_for_target(target)
        if current:
            if current.tid == tid:
                return current
            raise ValidationError(
                _(
                    "The selected target already has an active RFID Tag. "
                    "Revoke it first."
                )
            )

        if self.active_for_tid(tid):
            raise ValidationError(_("RFID Tag %s is already assigned.") % tid)
        return self.browse()

    @api.model
    def assign_tid(self, target, raw_tid):
        target_field = _TARGET_FIELDS.get(getattr(target, "_name", None))
        if not target_field or not target:
            raise ValidationError(_("RFID assignment target is invalid."))
        target.ensure_one()
        if not isinstance(target.id, int):
            raise ValidationError(_("Save the target before assigning an RFID Tag."))
        if not target.active:
            raise ValidationError(_("An archived target cannot receive an RFID Tag."))

        Tag = self.env["nsp.rfid.tag"].sudo()
        tid = Tag._prepare_tid(raw_tid)
        current = self._raise_assignment_conflict(target, tid)
        if current:
            return current

        tag = Tag.get_or_create_by_tid(tid)
        try:
            with self.env.cr.savepoint():
                return self.with_context(rfid_audit_user_id=self.env.user.id).create(
                    {"tag_id": tag.id, target_field: target.id}
                )
        except IntegrityError:
            current = self._raise_assignment_conflict(target, tid)
            if current:
                return current
            raise

    @api.model
    def prepare_runtime_projection(self):
        assignments = self.sudo().search(
            [("state", "=", "active")],
            order="tid, id",
        )
        items = []
        user_count = 0
        vehicle_count = 0

        for assignment in assignments:
            target = assignment.user_id or assignment.vehicle_id
            if not target or not target.active or not assignment.target_code:
                continue
            target_type = "user" if assignment.user_id else "vehicle"
            user_count += int(target_type == "user")
            vehicle_count += int(target_type == "vehicle")
            items.append(
                {
                    "tid": assignment.tid,
                    "assignment": {
                        "target": target_type,
                        "code": assignment.target_code,
                        "assigned_at": fields.Datetime.to_string(
                            assignment.assigned_at
                        ),
                    },
                }
            )

        return {
            "items": items,
            "summary": {
                "active_assignments": len(items),
                "user_assignments": user_count,
                "vehicle_assignments": vehicle_count,
            },
            "snapshot_scope": "rfid_runtime_assignments",
            "snapshot_mode": "replace",
        }
