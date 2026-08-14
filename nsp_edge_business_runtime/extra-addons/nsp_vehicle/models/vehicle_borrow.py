# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError, ValidationError


class NspVehicleBorrow(models.Model):
    _name = "nsp.vehicle.borrow"
    _description = "NSP Vehicle Borrow"
    _order = "valid_from desc, id desc"
    _rec_name = "name"

    name = fields.Char(compute="_compute_name", store=True)
    borrow_code = fields.Char(required=True, copy=False, readonly=True, default="New", index=True)
    vehicle_id = fields.Many2one(
        "nsp.vehicle", string="Vehicle", required=True, index=True,
        ondelete="restrict", domain=[("active", "=", True)],
    )
    license_plate = fields.Char(related="vehicle_id.license_plate", readonly=True)
    owner_id = fields.Many2one("nsp.user", related="vehicle_id.owner_id", readonly=True)
    borrower_id = fields.Many2one(
        "nsp.user", string="Borrower", required=True, index=True, ondelete="restrict",
    )
    borrower_code = fields.Char(related="borrower_id.user_code", readonly=True)
    allowed_borrower_ids = fields.Many2many(
        "nsp.user", compute="_compute_allowed_borrower_ids", string="Accepted Friends",
    )
    valid_from = fields.Datetime(required=True, default=fields.Datetime.now, index=True)
    valid_to = fields.Datetime(
        required=True, default=lambda self: fields.Datetime.now() + timedelta(days=1), index=True,
    )
    state = fields.Selection([
        ("active", "Active"),
        ("returned", "Returned"),
        ("cancelled", "Cancelled"),
    ], default="active", required=True, index=True)
    returned_at = fields.Datetime(readonly=True)
    active_now = fields.Boolean(compute="_compute_active_now", string="Active Now")

    _sql_constraints = [
        ("borrow_code_unique", "unique(borrow_code)", "Borrow Code must be unique."),
    ]

    @api.model
    def _is_borrow_admin(self):
        return bool(
            self.env.is_superuser()
            or self.env.user.has_group("base.group_system")
            or self.env.user.has_group("nsp_core.group_nsp_it_parking")
            or self.env.user.has_group("nsp_core.group_nsp_hr_parking")
        )

    @api.model
    def _current_identity(self, required=True):
        return self.env["nsp.user"]._current_nsp_identity(required=required)

    def _check_owner_self_service(self):
        if self._is_borrow_admin():
            return True
        identity = self._current_identity(required=True)
        if any(record.vehicle_id.owner_id != identity for record in self):
            raise AccessError(_("Only the Vehicle Owner can manage this lending authorization."))
        return True

    def init(self):
        self.env.cr.execute(
            """
            CREATE INDEX IF NOT EXISTS nsp_vehicle_borrow_active_lookup_idx
                ON nsp_vehicle_borrow (vehicle_id, borrower_id, valid_from, valid_to)
             WHERE state = 'active' AND returned_at IS NULL
            """
        )
        self.env.cr.execute(
            """
            CREATE INDEX IF NOT EXISTS nsp_vehicle_borrow_overlap_idx
                ON nsp_vehicle_borrow (vehicle_id, valid_from, valid_to)
             WHERE state = 'active'
            """
        )

    @api.depends("vehicle_id.license_plate", "borrower_id.name")
    def _compute_name(self):
        for rec in self:
            rec.name = "%s → %s" % (
                rec.vehicle_id.license_plate or _("Vehicle"),
                rec.borrower_id.name or _("User"),
            )

    @api.depends(
        "vehicle_id.owner_id",
        "vehicle_id.owner_id.friendship_sent_ids.state",
        "vehicle_id.owner_id.friendship_sent_ids.addressee_id",
        "vehicle_id.owner_id.friendship_received_ids.state",
        "vehicle_id.owner_id.friendship_received_ids.requester_id",
    )
    def _compute_allowed_borrower_ids(self):
        owners = self.mapped("vehicle_id.owner_id")
        friend_map = self.env["nsp.user.friendship"].sudo().accepted_friends_map(owners)
        for rec in self:
            owner_id = rec.vehicle_id.owner_id.id if rec.vehicle_id.owner_id else 0
            rec.allowed_borrower_ids = self.env["nsp.user"].browse(friend_map.get(owner_id, []))

    @api.onchange("vehicle_id")
    def _onchange_vehicle_id_refresh_allowed_borrowers(self):
        self._compute_allowed_borrower_ids()

    @api.depends("state", "valid_from", "valid_to", "returned_at")
    def _compute_active_now(self):
        now = fields.Datetime.now()
        for rec in self:
            rec.active_now = bool(
                rec.state == "active"
                and not rec.returned_at
                and rec.valid_from and rec.valid_from <= now
                and rec.valid_to and rec.valid_to >= now
            )

    @api.constrains("valid_from", "valid_to")
    def _check_valid_range(self):
        for rec in self:
            if rec.valid_from and rec.valid_to and rec.valid_from >= rec.valid_to:
                raise ValidationError(_("Valid To must be later than Valid From."))

    def _validate_borrower(self):
        if self.env.context.get("vehicle_borrow_sync"):
            return
        owners = self.mapped("vehicle_id.owner_id")
        friend_map = self.env["nsp.user.friendship"].sudo().accepted_friends_map(owners)
        for rec in self:
            if not rec.vehicle_id or not rec.borrower_id:
                continue
            if not rec.vehicle_id.active:
                raise ValidationError(_("Archived vehicles cannot be borrowed."))
            owner = rec.vehicle_id.owner_id
            if not owner:
                raise ValidationError(_("Vehicle owner is required before lending the vehicle."))
            if owner == rec.borrower_id:
                raise ValidationError(_("The borrower is already the vehicle owner."))
            if rec.borrower_id.id not in set(friend_map.get(owner.id, [])):
                raise ValidationError(_("The borrower must be an accepted friend of the vehicle owner."))

    def _check_overlap(self):
        active = self.filtered(
            lambda rec: rec.state == "active" and rec.vehicle_id and rec.valid_from and rec.valid_to
        )
        if not active:
            return
        candidates = self.sudo().search([
            ("vehicle_id", "in", active.mapped("vehicle_id").ids),
            ("state", "=", "active"),
        ], order="vehicle_id, valid_from, valid_to, id")
        latest_end_by_vehicle = {}
        for borrow in candidates:
            vehicle_id = borrow.vehicle_id.id
            latest_end = latest_end_by_vehicle.get(vehicle_id)
            if latest_end and borrow.valid_from < latest_end:
                raise ValidationError(
                    _("This vehicle already has an active lending period that overlaps this time window.")
                )
            if not latest_end or borrow.valid_to > latest_end:
                latest_end_by_vehicle[vehicle_id] = borrow.valid_to

    @api.model_create_multi
    def create(self, vals_list):
        seq = self.env["ir.sequence"].sudo()
        prepared = []
        sync_create = bool(self.env.context.get("vehicle_borrow_sync"))
        is_admin = self._is_borrow_admin()
        identity = (
            self._current_identity(required=True)
            if not is_admin and not sync_create
            else self.env["nsp.user"].browse([])
        )
        Vehicle = self.env["nsp.vehicle"].sudo()
        for source in vals_list:
            vals = dict(source)

            # A user-created Cloud authorization always starts as Active.  Do not
            # trust a readonly/state value round-tripped by the web client, even for
            # HR/IT administrators.  Only Cloud -> Edge snapshot application may
            # create a historical Returned/Cancelled record directly.
            if not sync_create:
                vals["state"] = "active"
                vals["returned_at"] = False

            if not is_admin and not sync_create:
                vehicle = Vehicle.browse(int(vals.get("vehicle_id") or 0)).exists()
                if not vehicle or vehicle.owner_id != identity:
                    raise AccessError(_("You can lend only a Vehicle that you own."))

            if vals.get("borrow_code", "New") == "New":
                vals["borrow_code"] = seq.next_by_code("nsp.vehicle.borrow") or "BORROW"
            vals.setdefault("state", "active")
            if vals.get("state") in ("active", "cancelled"):
                vals["returned_at"] = False
            prepared.append(vals)
        records = super().create(prepared)
        records._validate_borrower()
        records._check_overlap()
        return records

    def write(self, vals):
        values = dict(vals)
        if not self._is_borrow_admin() and not self.env.context.get("nsp_borrow_action"):
            self._check_owner_self_service()
            forbidden = {"vehicle_id", "borrow_code", "state", "returned_at"}.intersection(values)
            if forbidden:
                raise AccessError(_("Use the End or Cancel actions to change lending state."))

        result = super().write(values)
        if not self.env.context.get("vehicle_borrow_sync") and (
            "vehicle_id" in values or "borrower_id" in values or values.get("state") == "active"
        ):
            self._validate_borrower()
        if any(key in values for key in ("vehicle_id", "valid_from", "valid_to", "state")):
            self._check_overlap()
        return result

    def action_return_vehicle(self):
        self._check_owner_self_service()
        if self.filtered(lambda rec: rec.state != "active"):
            raise UserError(_("Only an active vehicle borrow can be ended."))
        if self:
            self.with_context(nsp_borrow_action=True).write(
                {"state": "returned", "returned_at": fields.Datetime.now()}
            )
        return True

    def action_cancel(self):
        self._check_owner_self_service()
        if self.filtered(lambda rec: rec.state == "returned"):
            raise UserError(_("Returned vehicle borrows cannot be cancelled."))
        if self:
            self.with_context(nsp_borrow_action=True).write(
                {"state": "cancelled", "returned_at": False}
            )
        return True

    @api.model
    def find_valid_borrow(self, vehicle, borrower=False, borrow_time=False):
        if not vehicle:
            return self.browse()
        borrow_time = borrow_time or fields.Datetime.now()
        domain = [
            ("vehicle_id", "=", vehicle.id),
            ("state", "=", "active"),
            ("returned_at", "=", False),
            ("valid_from", "<=", borrow_time),
            ("valid_to", ">=", borrow_time),
        ]
        if borrower:
            domain.append(("borrower_id", "=", borrower.id))
        return self.sudo().search(domain, order="valid_to asc, id desc", limit=1)
