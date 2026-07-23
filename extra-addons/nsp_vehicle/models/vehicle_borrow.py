# -*- coding: utf-8 -*-
from datetime import timedelta
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError, UserError


class NspVehicleBorrow(models.Model):
    _name = "nsp.vehicle.borrow"
    _description = "NSP Vehicle Borrow"
    _order = "valid_from desc, id desc"
    _rec_name = "name"

    name = fields.Char(compute="_compute_name", store=True)
    borrow_code = fields.Char(required=True, copy=False, readonly=True, default="New", index=True)
    vehicle_id = fields.Many2one("nsp.vehicle", string="Vehicle", required=True, index=True, ondelete="restrict", domain=[("state", "=", "approved")])
    license_plate = fields.Char(related="vehicle_id.license_plate", readonly=True)
    owner_id = fields.Many2one("nsp.user", related="vehicle_id.owner_id", readonly=True)
    borrower_id = fields.Many2one("nsp.user", string="Borrower", required=True, index=True, ondelete="restrict")
    borrower_code = fields.Char(related="borrower_id.user_code", readonly=True)
    allowed_borrower_ids = fields.Many2many("nsp.user", compute="_compute_allowed_borrower_ids", string="Accepted Friends")
    valid_from = fields.Datetime(required=True, default=fields.Datetime.now, index=True)
    valid_to = fields.Datetime(required=True, default=lambda self: fields.Datetime.now() + timedelta(days=1), index=True)
    state = fields.Selection([
        ("active", "Active"),
        ("returned", "Returned"),
        ("cancelled", "Cancelled"),
    ], default="active", required=True, index=True)
    returned_at = fields.Datetime(readonly=True)
    active_now = fields.Boolean(compute="_compute_active_now", string="Active Now")
    sync_record_key = fields.Char(compute="_compute_sync_record_key", store=True, index=True)

    _sql_constraints = [
        ("borrow_code_unique", "unique(borrow_code)", "Borrow Code must be unique."),
    ]


    @api.depends("vehicle_id.license_plate", "borrower_id.name")
    def _compute_name(self):
        for rec in self:
            rec.name = "%s → %s" % (
                rec.vehicle_id.license_plate or _("Vehicle"),
                rec.borrower_id.name or _("User"),
            )

    @api.depends("vehicle_id", "vehicle_id.owner_id")
    def _compute_allowed_borrower_ids(self):
        Friendship = self.env["nsp.user.friendship"].sudo()
        for rec in self:
            rec.allowed_borrower_ids = Friendship.accepted_friends(rec.vehicle_id.owner_id)

    @api.depends("state", "valid_from", "valid_to", "returned_at")
    def _compute_active_now(self):
        now = fields.Datetime.now()
        for rec in self:
            rec.active_now = bool(
                rec.state == "active" and not rec.returned_at
                and rec.valid_from and rec.valid_from <= now
                and rec.valid_to and rec.valid_to >= now
            )

    @api.depends("borrow_code")
    def _compute_sync_record_key(self):
        for rec in self:
            rec.sync_record_key = rec.borrow_code or False

    @api.constrains("valid_from", "valid_to")
    def _check_valid_range(self):
        for rec in self:
            if rec.valid_from and rec.valid_to and rec.valid_from >= rec.valid_to:
                raise ValidationError(_("Valid To must be later than Valid From."))

    def _validate_borrower(self):
        if self.env.context.get("vehicle_borrow_sync"):
            return
        Friendship = self.env["nsp.user.friendship"].sudo()
        for rec in self:
            if not rec.vehicle_id or not rec.borrower_id:
                continue
            if rec.vehicle_id.state != "approved":
                raise ValidationError(_("Only approved vehicles can be borrowed."))
            owner = rec.vehicle_id.owner_id
            if not owner:
                raise ValidationError(_("Vehicle owner is required before lending the vehicle."))
            if owner == rec.borrower_id:
                raise ValidationError(_("The borrower is already the vehicle owner."))
            if not Friendship.are_friends(owner, rec.borrower_id):
                raise ValidationError(_("The borrower must be an accepted friend of the vehicle owner."))

    def _check_overlap(self):
        for rec in self:
            if rec.state != "active" or not rec.vehicle_id or not rec.valid_from or not rec.valid_to:
                continue
            overlap = self.search([
                ("id", "!=", rec.id),
                ("vehicle_id", "=", rec.vehicle_id.id),
                ("state", "=", "active"),
                ("valid_from", "<", rec.valid_to),
                ("valid_to", ">", rec.valid_from),
            ], limit=1)
            if overlap:
                raise ValidationError(_("This vehicle already has an active lending period that overlaps this time window."))

    @api.model_create_multi
    def create(self, vals_list):
        seq = self.env["ir.sequence"].sudo()
        prepared = []
        for source in vals_list:
            vals = dict(source)
            if vals.get("borrow_code", "New") == "New":
                vals["borrow_code"] = seq.next_by_code("nsp.vehicle.borrow") or "BORROW"
            vals.setdefault("state", "active")
            prepared.append(vals)
        records = super().create(prepared)
        records._validate_borrower()
        records._check_overlap()
        return records

    def write(self, vals):
        res = super().write(vals)
        if not self.env.context.get("vehicle_borrow_sync") and (
            "vehicle_id" in vals or "borrower_id" in vals or vals.get("state") == "active"
        ):
            self._validate_borrower()
        if any(key in vals for key in ("vehicle_id", "valid_from", "valid_to", "state")):
            self._check_overlap()
        return res

    def action_return_vehicle(self):
        for rec in self:
            if rec.state != "active":
                raise UserError(_("Only an active vehicle borrow can be ended."))
            rec.write({"state": "returned", "returned_at": fields.Datetime.now()})
        return True

    def action_cancel(self):
        for rec in self:
            if rec.state == "returned":
                raise UserError(_("Returned vehicle borrows cannot be cancelled."))
            rec.write({"state": "cancelled", "returned_at": False})
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
