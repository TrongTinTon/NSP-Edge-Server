from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError


class NspRfidTagAssignment(models.Model):
    _name = "nsp.rfid.tag.assignment"
    _description = "NSP RFID Tag Assignment"
    _rec_name = "display_name"
    _order = "assigned_at desc, id desc"

    display_name = fields.Char(compute="_compute_display_name", store=True)
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
        default="active",
        readonly=True,
        index=True,
    )
    assigned_at = fields.Datetime(
        required=True,
        default=fields.Datetime.now,
        readonly=True,
        index=True,
    )
    assigned_by_id = fields.Many2one(
        "res.users",
        required=True,
        default=lambda self: self.env.user,
        readonly=True,
        ondelete="restrict",
    )
    revoked_at = fields.Datetime(readonly=True, index=True)
    revoked_by_id = fields.Many2one(
        "res.users",
        readonly=True,
        ondelete="restrict",
    )

    _sql_constraints = [
        (
            "exactly_one_target",
            "CHECK ((user_id IS NOT NULL) <> (vehicle_id IS NOT NULL))",
            "An RFID Tag must be assigned to exactly one User or one Vehicle.",
        ),
    ]

    def init(self):
        self.env.cr.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS nsp_rfid_assignment_active_tag_uniq
                ON nsp_rfid_tag_assignment (tag_id)
             WHERE state = 'active'
            """
        )
        self.env.cr.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS nsp_rfid_assignment_active_user_uniq
                ON nsp_rfid_tag_assignment (user_id)
             WHERE state = 'active' AND user_id IS NOT NULL
            """
        )
        self.env.cr.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS nsp_rfid_assignment_active_vehicle_uniq
                ON nsp_rfid_tag_assignment (vehicle_id)
             WHERE state = 'active' AND vehicle_id IS NOT NULL
            """
        )

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
    def _resolve_values(self, values):
        tag = self.env["nsp.rfid.tag"].sudo().browse(
            int(values.get("tag_id") or 0)
        ).exists()
        user = self.env["nsp.user"].sudo().browse(
            int(values.get("user_id") or 0)
        ).exists()
        vehicle = self.env["nsp.vehicle"].sudo().browse(
            int(values.get("vehicle_id") or 0)
        ).exists()
        return tag, user, vehicle

    @api.model
    def _validate_assignment(self, values, reserved, exclude_ids=None):
        tag, user, vehicle = self._resolve_values(values)
        if not tag:
            raise ValidationError(_("RFID Tag is required."))
        if bool(user) == bool(vehicle):
            raise ValidationError(_(
                "An RFID Tag must be assigned to exactly one User or one Vehicle."
            ))
        target = user or vehicle
        if not target.active:
            raise ValidationError(_("An archived target cannot receive an RFID Tag."))

        target_key = ("user", user.id) if user else ("vehicle", vehicle.id)
        if tag.id in reserved["tags"]:
            raise ValidationError(_("RFID Tag %s is already assigned.") % tag.tid)
        if target_key in reserved["targets"]:
            raise ValidationError(_("The selected target already has an active RFID Tag."))

        domain = [
            ("state", "=", "active"),
            "|",
            ("tag_id", "=", tag.id),
            ("user_id" if user else "vehicle_id", "=", target.id),
        ]
        if exclude_ids:
            domain.append(("id", "not in", list(exclude_ids)))
        conflict = self.sudo().search(domain, limit=1)
        if conflict:
            if conflict.tag_id == tag:
                raise ValidationError(_("RFID Tag %s is already assigned.") % tag.tid)
            raise ValidationError(_("The selected target already has an active RFID Tag."))

        reserved["tags"].add(tag.id)
        reserved["targets"].add(target_key)

    @api.model_create_multi
    def create(self, vals_list):
        now = fields.Datetime.now()
        actor_id = int(self.env.context.get("rfid_audit_user_id") or self.env.user.id)
        reserved = {"tags": set(), "targets": set()}
        prepared = []
        for source in vals_list:
            vals = dict(source)
            vals.update({
                "state": "active",
                "assigned_at": now,
                "assigned_by_id": actor_id,
                "revoked_at": False,
                "revoked_by_id": False,
            })
            self._validate_assignment(vals, reserved)
            prepared.append(vals)
        assignments = super().create(prepared)
        assignments._post_audit_message(_("RFID Tag assigned"), actor_id)
        return assignments

    def write(self, vals):
        values = dict(vals)
        controlled = bool(self.env.context.get("rfid_assignment_revoke"))
        protected = {
            "user_id",
            "vehicle_id",
            "state",
            "assigned_at",
            "assigned_by_id",
            "revoked_at",
            "revoked_by_id",
        }
        if protected.intersection(values) and not controlled:
            raise UserError(_("RFID assignment target and audit fields cannot be edited."))
        if values.get("state") == "active" and self.filtered(
            lambda record: record.state == "revoked"
        ):
            raise UserError(_("A revoked RFID assignment cannot be reactivated."))

        if "tag_id" not in values or controlled:
            return super().write(values)

        if self.filtered(lambda assignment: assignment.state != "active"):
            raise UserError(_("Only active RFID assignments can be edited."))

        tag = self.env["nsp.rfid.tag"].sudo().browse(
            int(values.get("tag_id") or 0)
        ).exists()
        if not tag:
            raise ValidationError(_("RFID Tag is required."))

        reserved = {"tags": set(), "targets": set()}
        for assignment in self:
            candidate = {
                "tag_id": tag.id,
                "user_id": assignment.user_id.id,
                "vehicle_id": assignment.vehicle_id.id,
            }
            self._validate_assignment(
                candidate,
                reserved,
                exclude_ids=self.ids,
            )

        previous = {assignment.id: assignment.tid for assignment in self}
        actor_id = int(self.env.context.get("rfid_audit_user_id") or self.env.user.id)
        values.update({
            "assigned_at": fields.Datetime.now(),
            "assigned_by_id": actor_id,
        })
        result = super().write(values)
        actor = self.env["res.users"].sudo().browse(actor_id).exists()
        author_id = actor.partner_id.id if actor and actor.partner_id else False
        for assignment in self:
            target = assignment.user_id or assignment.vehicle_id
            if target and previous.get(assignment.id) != assignment.tid:
                target.sudo().message_post(
                    body=_("RFID Tag changed: %(old)s → %(new)s") % {
                        "old": previous.get(assignment.id) or "-",
                        "new": assignment.tid or "-",
                    },
                    subtype_xmlid="mail.mt_note",
                    author_id=author_id,
                )
        return result

    def unlink(self):
        if self.env.context.get("module_uninstall"):
            return super().unlink()
        actor_id = int(self.env.context.get("rfid_audit_user_id") or self.env.user.id)
        self._post_audit_message(_("RFID Tag unassigned"), actor_id)
        return super().unlink()

    @api.constrains("user_id", "vehicle_id", "state")
    def _check_target(self):
        for assignment in self:
            if bool(assignment.user_id) == bool(assignment.vehicle_id):
                raise ValidationError(_(
                    "An RFID Tag must be assigned to exactly one User or one Vehicle."
                ))
            target = assignment.user_id or assignment.vehicle_id
            if assignment.state == "active" and target and not target.active:
                raise ValidationError(_("An archived target cannot have an active RFID Tag."))

    def _post_audit_message(self, title, actor_id):
        actor = self.env["res.users"].sudo().browse(actor_id).exists()
        author_id = actor.partner_id.id if actor and actor.partner_id else False
        for assignment in self:
            target = assignment.user_id or assignment.vehicle_id
            if target:
                target.sudo().message_post(
                    body=_("%(title)s: %(tid)s") % {
                        "title": title,
                        "tid": assignment.tid,
                    },
                    subtype_xmlid="mail.mt_note",
                    author_id=author_id,
                )

    def action_revoke(self):
        active = self.filtered(lambda record: record.state == "active")
        if not active:
            return True
        actor_id = int(self.env.context.get("rfid_audit_user_id") or self.env.user.id)
        active.with_context(rfid_assignment_revoke=True).write({
            "state": "revoked",
            "revoked_at": fields.Datetime.now(),
            "revoked_by_id": actor_id,
        })
        active._post_audit_message(_("RFID Tag revoked"), actor_id)
        return True

    @api.model
    def active_for_tid(self, tid):
        normalized = self.env["nsp.rfid.tag"]._normalize_tid(tid)
        if not normalized:
            return self.browse()
        return self.sudo().search([
            ("tid", "=", normalized),
            ("state", "=", "active"),
        ], limit=1)

    @api.model
    def active_for_user(self, user):
        return self.sudo().search([
            ("user_id", "=", user.id),
            ("state", "=", "active"),
        ], limit=1) if user else self.browse()

    @api.model
    def active_for_vehicle(self, vehicle):
        return self.sudo().search([
            ("vehicle_id", "=", vehicle.id),
            ("state", "=", "active"),
        ], limit=1) if vehicle else self.browse()

    @api.model
    def active_for_target(self, target):
        if not target or target._name not in {"nsp.user", "nsp.vehicle"}:
            return self.browse()
        field_name = "user_id" if target._name == "nsp.user" else "vehicle_id"
        return self.sudo().search([
            (field_name, "=", target.id),
            ("state", "=", "active"),
        ], limit=1)

    @api.model
    def assign_tid(self, target, raw_tid):
        if not target or target._name not in {"nsp.user", "nsp.vehicle"}:
            raise ValidationError(_("RFID assignment target is invalid."))
        if not target.active:
            raise ValidationError(_("An archived target cannot receive an RFID Tag."))
        Tag = self.env["nsp.rfid.tag"].sudo()
        tid = Tag._normalize_tid(raw_tid)
        if not tid:
            return self.browse()
        current = self.active_for_target(target)
        if current:
            if current.tid == tid:
                return current
            raise ValidationError(_(
                "The selected target already has an active RFID Tag. Revoke it first."
            ))
        tag = Tag.get_or_create_by_tid(tid)
        values = {
            "tag_id": tag.id,
            "user_id" if target._name == "nsp.user" else "vehicle_id": target.id,
        }
        return self.with_context(rfid_audit_user_id=self.env.user.id).create(values)

    @api.model
    def revoke_target(self, target):
        assignment = self.active_for_target(target)
        if assignment:
            assignment.with_context(rfid_audit_user_id=self.env.user.id).action_revoke()
        return True

    @api.model
    def prepare_runtime_projection(self):
        assignments = self.sudo().search([
            ("state", "=", "active"),
        ], order="tid, id")
        items = []
        user_count = 0
        vehicle_count = 0
        for assignment in assignments:
            target = assignment.user_id or assignment.vehicle_id
            if not target or not target.active or not assignment.target_code:
                continue
            target_type = "user" if assignment.user_id else "vehicle"
            if target_type == "user":
                user_count += 1
            else:
                vehicle_count += 1
            items.append({
                "tid": assignment.tid,
                "assignment": {
                    "target": target_type,
                    "code": assignment.target_code,
                    "assigned_at": fields.Datetime.to_string(assignment.assigned_at),
                },
            })
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
