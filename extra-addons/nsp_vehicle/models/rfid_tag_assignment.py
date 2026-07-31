# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError


class NspRfidTagAssignment(models.Model):
    """Immutable audit record assigning one whitelisted TID to one target."""

    _name = "nsp.rfid.tag.assignment"
    _description = "NSP RFID Tag Assignment"
    _rec_name = "display_name"
    _order = "assigned_at desc, id desc"

    display_name = fields.Char(compute="_compute_display_name", store=True)
    tag_id = fields.Many2one(
        "nsp.rfid.tag",
        string="RFID Tag",
        required=True,
        ondelete="restrict",
        index=True,
    )
    tid = fields.Char(
        related="tag_id.tid",
        string="TID",
        store=True,
        readonly=True,
        index=True,
    )
    user_id = fields.Many2one(
        "nsp.user",
        string="User",
        ondelete="restrict",
        index=True,
    )
    vehicle_id = fields.Many2one(
        "nsp.vehicle",
        string="Vehicle",
        ondelete="restrict",
        index=True,
    )
    state = fields.Selection(
        [("active", "Active"), ("revoked", "Revoked")],
        required=True,
        default="active",
        readonly=True,
        index=True,
    )
    assigned_at = fields.Datetime(
        string="Assigned At",
        required=True,
        default=fields.Datetime.now,
        readonly=True,
        index=True,
    )
    assigned_by_id = fields.Many2one(
        "res.users",
        string="Assigned By",
        required=True,
        default=lambda self: self.env.user,
        readonly=True,
        ondelete="restrict",
    )
    revoked_at = fields.Datetime(string="Revoked At", readonly=True, index=True)
    revoked_by_id = fields.Many2one(
        "res.users",
        string="Revoked By",
        readonly=True,
        ondelete="restrict",
    )

    def init(self):
        """Race-safe database guarantees for the active assignment snapshot."""
        self.env.cr.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS nsp_rfid_assignment_one_active_tag_idx
                ON nsp_rfid_tag_assignment (tag_id)
             WHERE state = 'active'
            """
        )
        self.env.cr.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS nsp_rfid_assignment_one_active_user_idx
                ON nsp_rfid_tag_assignment (user_id)
             WHERE state = 'active' AND user_id IS NOT NULL
            """
        )
        self.env.cr.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS nsp_rfid_assignment_one_active_vehicle_idx
                ON nsp_rfid_tag_assignment (vehicle_id)
             WHERE state = 'active' AND vehicle_id IS NOT NULL
            """
        )
        self.env.cr.execute(
            """
            ALTER TABLE nsp_rfid_tag_assignment
            DROP CONSTRAINT IF EXISTS nsp_rfid_assignment_exactly_one_target
            """
        )
        self.env.cr.execute(
            """
            ALTER TABLE nsp_rfid_tag_assignment
            ADD CONSTRAINT nsp_rfid_assignment_exactly_one_target
            CHECK ((user_id IS NOT NULL)::integer + (vehicle_id IS NOT NULL)::integer = 1)
            """
        )

    @api.depends("tid", "user_id.name", "vehicle_id.license_plate", "state")
    def _compute_display_name(self):
        for assignment in self:
            target = (
                assignment.user_id.display_name
                if assignment.user_id
                else assignment.vehicle_id.license_plate
            ) or _("Unassigned")
            assignment.display_name = "%s → %s" % (
                assignment.tid or _("RFID Tag"),
                target,
            )

    @api.model
    def _validate_create_values(self, values, reserved_tags, reserved_users, reserved_vehicles):
        tag = self.env["nsp.rfid.tag"].sudo().browse(int(values.get("tag_id") or 0)).exists()
        user = self.env["nsp.user"].sudo().browse(int(values.get("user_id") or 0)).exists()
        vehicle = self.env["nsp.vehicle"].sudo().browse(int(values.get("vehicle_id") or 0)).exists()

        if not tag:
            raise ValidationError(_("RFID Tag is required."))
        if bool(user) == bool(vehicle):
            raise ValidationError(_("An RFID Tag must be assigned to exactly one User or one Vehicle."))
        if user and not user.active:
            raise ValidationError(_("An archived User cannot receive an active RFID Tag."))
        if vehicle and not vehicle.active:
            raise ValidationError(_("An archived Vehicle cannot receive an active RFID Tag."))

        if tag.id in reserved_tags:
            raise ValidationError(_("RFID Tag %s is already assigned.") % tag.tid)
        if user and user.id in reserved_users:
            raise ValidationError(_("User %s already has an active Employee RFID Tag.") % user.display_name)
        if vehicle and vehicle.id in reserved_vehicles:
            raise ValidationError(_("Vehicle %s already has an active RFID Tag.") % vehicle.display_name)

        conflict_domain = [("state", "=", "active"), ("tag_id", "=", tag.id)]
        if user:
            conflict_domain = [
                ("state", "=", "active"),
                "|",
                ("tag_id", "=", tag.id),
                ("user_id", "=", user.id),
            ]
        elif vehicle:
            conflict_domain = [
                ("state", "=", "active"),
                "|",
                ("tag_id", "=", tag.id),
                ("vehicle_id", "=", vehicle.id),
            ]
        conflicts = self.sudo().search(conflict_domain)
        tag_conflict = conflicts.filtered(lambda rec: rec.tag_id == tag)[:1]
        if tag_conflict:
            raise ValidationError(_("RFID Tag %s is already assigned.") % tag.tid)
        user_conflict = conflicts.filtered(lambda rec: user and rec.user_id == user)[:1]
        if user_conflict:
            raise ValidationError(_("User %s already has an active Employee RFID Tag.") % user.display_name)
        vehicle_conflict = conflicts.filtered(lambda rec: vehicle and rec.vehicle_id == vehicle)[:1]
        if vehicle_conflict:
            raise ValidationError(_("Vehicle %s already has an active RFID Tag.") % vehicle.display_name)

        reserved_tags.add(tag.id)
        if user:
            reserved_users.add(user.id)
        if vehicle:
            reserved_vehicles.add(vehicle.id)

    @api.model_create_multi
    def create(self, vals_list):
        sync_mode = bool(self.env.context.get("rfid_assignment_sync"))
        actor_user_id = int(
            self.env.context.get("rfid_audit_user_id")
            or self.env.user.id
        )
        now = fields.Datetime.now()
        prepared = []
        reserved_tags = set()
        reserved_users = set()
        reserved_vehicles = set()
        for source in vals_list:
            vals = dict(source)
            vals["state"] = "active"
            if sync_mode:
                vals.setdefault("assigned_at", now)
                vals.setdefault("assigned_by_id", actor_user_id)
            else:
                # Audit values are server-owned for normal UI/API operations.
                vals["assigned_at"] = now
                vals["assigned_by_id"] = actor_user_id
                vals.pop("revoked_at", None)
                vals.pop("revoked_by_id", None)
            self._validate_create_values(
                vals,
                reserved_tags,
                reserved_users,
                reserved_vehicles,
            )
            prepared.append(vals)
        return super().create(prepared)

    def write(self, vals):
        values = dict(vals)
        controlled = bool(
            self.env.context.get("rfid_assignment_sync")
            or self.env.context.get("rfid_assignment_revoke")
        )
        immutable_fields = {
            "tag_id",
            "user_id",
            "vehicle_id",
            "state",
            "assigned_at",
            "assigned_by_id",
            "revoked_at",
            "revoked_by_id",
        }
        if immutable_fields.intersection(values) and not controlled:
            raise UserError(_(
                "RFID Tag assignment history is immutable. Revoke the active assignment and create a new one."
            ))
        if values.get("state") == "active" and self.filtered(lambda rec: rec.state == "revoked"):
            raise UserError(_("A revoked RFID Tag assignment cannot be reactivated."))
        result = super().write(values)
        self._check_assignment_targets()
        return result

    def unlink(self):
        if not (
            self.env.context.get("module_uninstall")
            or self.env.context.get("rfid_assignment_sync_cleanup")
        ):
            raise UserError(_("RFID Tag assignment history cannot be deleted. Revoke it instead."))
        return super().unlink()

    @api.constrains("tag_id", "user_id", "vehicle_id", "state")
    def _check_assignment_targets(self):
        for assignment in self:
            if bool(assignment.user_id) == bool(assignment.vehicle_id):
                raise ValidationError(_("An RFID Tag must be assigned to exactly one User or one Vehicle."))
            if assignment.state == "active":
                if assignment.user_id and not assignment.user_id.active:
                    raise ValidationError(_("An archived User cannot have an active RFID Tag."))
                if assignment.vehicle_id and not assignment.vehicle_id.active:
                    raise ValidationError(_("An archived Vehicle cannot have an active RFID Tag."))

    def action_revoke(self):
        active = self.filtered(lambda rec: rec.state == "active")
        if not active:
            return True
        actor_user_id = int(
            self.env.context.get("rfid_audit_user_id")
            or self.env.user.id
        )
        active.with_context(rfid_assignment_revoke=True).write({
            "state": "revoked",
            "revoked_at": fields.Datetime.now(),
            "revoked_by_id": actor_user_id,
        })
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
        if not user:
            return self.browse()
        return self.sudo().search([
            ("user_id", "=", user.id),
            ("state", "=", "active"),
        ], limit=1)

    @api.model
    def active_for_vehicle(self, vehicle):
        if not vehicle:
            return self.browse()
        return self.sudo().search([
            ("vehicle_id", "=", vehicle.id),
            ("state", "=", "active"),
        ], limit=1)
