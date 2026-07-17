# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError, UserError


class NspVehicleBorrowRequest(models.Model):
    _name = "nsp.vehicle.borrow.request"
    _description = "NSP Vehicle Borrow Request"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "request_date desc, id desc"
    _rec_name = "borrow_code"

    borrow_code = fields.Char(string="Borrow Code", required=True, copy=False, readonly=True, default="New", index=True)
    vehicle_id = fields.Many2one("nsp.vehicle", string="Vehicle", required=True, tracking=True, index=True, domain=[("state", "=", "approved")])
    license_plate = fields.Char(string="License Plate", related="vehicle_id.license_plate", store=True, readonly=True, index=True)
    owner_id = fields.Many2one("nsp.user", string="Vehicle Owner", related="vehicle_id.owner_id", store=True, readonly=True, index=True)
    borrower_id = fields.Many2one("nsp.user", string="Borrower", required=True, tracking=True, index=True)
    borrower_code = fields.Char(string="Borrower Code", compute="_compute_borrower_code", store=True, readonly=True, index=True)
    borrower_name = fields.Char(string="Borrower Name", related="borrower_id.name", store=True, readonly=True)

    request_date = fields.Datetime(string="Request Date", default=fields.Datetime.now, required=True, tracking=True)
    valid_from = fields.Datetime(string="Valid From", required=True, tracking=True)
    valid_to = fields.Datetime(string="Valid To", required=True, tracking=True)
    approved_at = fields.Datetime(string="Approved At", readonly=True, tracking=True)
    approved_by_id = fields.Many2one("res.users", string="Approved By", readonly=True)
    returned_at = fields.Datetime(string="Returned At", readonly=True, tracking=True)
    returned_by_id = fields.Many2one("res.users", string="Closed By", readonly=True)

    reason = fields.Text(string="Borrow Reason")
    approval_note = fields.Text(string="Approval Note")
    return_note = fields.Text(string="Return Note")
    reject_reason = fields.Text(string="Reject Reason")

    state = fields.Selection([
        ("draft", "Draft"),
        ("waiting", "Waiting Approval"),
        ("approved", "Approved"),
        ("returned", "Returned / Closed"),
        ("rejected", "Rejected"),
        ("cancelled", "Cancelled"),
    ], string="Status", default="draft", required=True, tracking=True, index=True)
    active_for_controller = fields.Boolean(string="Active For Controller", compute="_compute_active_for_controller", store=True, index=True)
    sync_record_key = fields.Char(string="Sync Record Key", compute="_compute_sync_record_key", store=True, index=True)

    _sql_constraints = [
        ("borrow_code_unique", "unique(borrow_code)", "Borrow Code must be unique."),
    ]

    @api.depends("borrower_id", "borrower_id.user_code")
    def _compute_borrower_code(self):
        for rec in self:
            rec.borrower_code = rec.borrower_id.user_code if rec.borrower_id and "user_code" in rec.borrower_id._fields else (str(rec.borrower_id.id) if rec.borrower_id else False)

    @api.depends("borrow_code")
    def _compute_sync_record_key(self):
        for rec in self:
            rec.sync_record_key = rec.borrow_code or ("BORROW-%s" % rec.id if rec.id else False)

    @api.depends("state", "valid_from", "valid_to", "returned_at")
    def _compute_active_for_controller(self):
        now = fields.Datetime.now()
        for rec in self:
            rec.active_for_controller = bool(
                rec.state == "approved"
                and not rec.returned_at
                and rec.valid_to
                and rec.valid_to >= now
            )

    @api.constrains("valid_from", "valid_to")
    def _check_valid_range(self):
        for rec in self:
            if rec.valid_from and rec.valid_to and rec.valid_from >= rec.valid_to:
                raise ValidationError(_("Valid To must be later than Valid From."))

    @api.constrains("vehicle_id", "borrower_id")
    def _check_vehicle_and_borrower(self):
        for rec in self:
            if rec.vehicle_id and rec.vehicle_id.state != "approved":
                raise ValidationError(_("Only approved vehicles can be borrowed."))
            if rec.vehicle_id and rec.borrower_id and rec.vehicle_id.owner_id == rec.borrower_id:
                raise ValidationError(_("The borrower is already the vehicle owner. A borrow request is not required."))

    @api.model_create_multi
    def create(self, vals_list):
        seq = self.env["ir.sequence"].sudo()
        for vals in vals_list:
            if vals.get("borrow_code", "New") == "New":
                vals["borrow_code"] = seq.next_by_code("nsp.vehicle.borrow.request") or "BORROW"
        return super().create(vals_list)

    def write(self, vals):
        res = super().write(vals)
        if any(key in vals for key in ("state", "valid_from", "valid_to", "returned_at", "vehicle_id", "borrower_id")):
            self._mark_borrow_sync_pending("Borrow request updated.")
        return res

    def _open_overlap_domain(self):
        self.ensure_one()
        return [
            ("id", "!=", self.id),
            ("vehicle_id", "=", self.vehicle_id.id),
            ("state", "in", ["waiting", "approved"]),
            ("valid_from", "<", self.valid_to),
            ("valid_to", ">", self.valid_from),
        ]

    def _check_overlapping_open_borrow(self):
        for rec in self:
            if not rec.vehicle_id or not rec.valid_from or not rec.valid_to:
                continue
            overlap = self.search(rec._open_overlap_domain(), limit=1)
            if overlap:
                raise ValidationError(_("Vehicle %s already has an open/approved borrow request in the same time window: %s.") % (rec.license_plate, overlap.borrow_code))

    def action_submit(self):
        for rec in self:
            if rec.state != "draft":
                continue
            rec._check_overlapping_open_borrow()
            rec.write({"state": "waiting"})

    def action_approve(self):
        for rec in self:
            if rec.state not in ("draft", "waiting"):
                raise UserError(_("Only Draft or Waiting Approval borrow requests can be approved."))
            rec._check_overlapping_open_borrow()
            rec.write({
                "state": "approved",
                "approved_at": fields.Datetime.now(),
                "approved_by_id": self.env.user.id,
            })

    def action_reject(self):
        for rec in self:
            if rec.state in ("returned", "cancelled"):
                continue
            rec.write({"state": "rejected"})

    def action_cancel(self):
        for rec in self:
            if rec.state == "returned":
                raise UserError(_("Returned borrow requests cannot be cancelled."))
            rec.write({"state": "cancelled"})

    def action_return_vehicle(self):
        for rec in self:
            if rec.state != "approved":
                raise UserError(_("Only approved borrow requests can be closed as returned."))
            rec.write({
                "state": "returned",
                "returned_at": fields.Datetime.now(),
                "returned_by_id": self.env.user.id,
            })

    @api.model
    def _vehicle_tids(self, vehicle):
        if not vehicle:
            return []
        tids = []
        for line in getattr(vehicle, "vehicle_card_ids", []):
            if line.state == "active" and line.tid:
                tids.append(line.tid)
        if not tids and vehicle.tid:
            tids.append(vehicle.tid)
        return list(dict.fromkeys([str(t).strip() for t in tids if str(t or "").strip()]))

    def _controller_payload(self):
        self.ensure_one()
        vehicle = self.vehicle_id
        borrower = self.borrower_id
        return {
            "record_key": self.sync_record_key,
            "borrow_id": self.id,
            "borrow_code": self.borrow_code,
            "vehicle_id": vehicle.id if vehicle else False,
            "license_plate": vehicle.license_plate if vehicle else "",
            "vehicle_tids": self._vehicle_tids(vehicle),
            "borrower_user_id": borrower.id if borrower else False,
            "borrower_user_code": self.borrower_code or "",
            "borrower_employee_id": self.borrower_code or "",
            "borrower_name": borrower.name if borrower else "",
            "owner_user_id": self.owner_id.id if self.owner_id else False,
            "owner_user_code": self.owner_id.user_code if self.owner_id and "user_code" in self.owner_id._fields else "",
            "owner_name": self.owner_id.name if self.owner_id else "",
            "valid_from": fields.Datetime.to_string(self.valid_from) if self.valid_from else False,
            "valid_to": fields.Datetime.to_string(self.valid_to) if self.valid_to else False,
            "returned_at": fields.Datetime.to_string(self.returned_at) if self.returned_at else False,
            "state": self.state,
            "active": bool(self.active_for_controller),
            "write_date": fields.Datetime.to_string(self.write_date) if self.write_date else False,
        }

    @api.model
    def find_valid_borrow(self, vehicle, borrower=False, borrow_time=False):
        if not vehicle:
            return self.browse()
        borrow_time = borrow_time or fields.Datetime.now()
        domain = [
            ("vehicle_id", "=", vehicle.id),
            ("state", "=", "approved"),
            ("returned_at", "=", False),
            ("valid_from", "<=", borrow_time),
            ("valid_to", ">=", borrow_time),
        ]
        if borrower:
            domain.append(("borrower_id", "=", borrower.id))
        return self.sudo().search(domain, order="valid_to asc, id desc", limit=1)

    def _mark_borrow_sync_pending(self, message=False):
        if "nsp.sync.record" not in self.env.registry.models:
            return False
        Controller = self.env["nsp.controller"].sudo() if "nsp.controller" in self.env.registry.models else False
        controllers = Controller.search([("core_api_application_id", "!=", False)]) if Controller else []
        for rec in self:
            for controller in controllers:
                try:
                    self.env["nsp.sync.record"].sudo().mark_pending(
                        controller=controller,
                        action_code="nsp_gatekeeper_vehicle_borrow_sync",
                        action_name="NSP Gatekeeper Vehicle Borrow Sync",
                        record=rec,
                        record_key=rec.sync_record_key,
                        message=message or "Borrow request changed; waiting for Cloud synchronization.",
                        operation="pull",
                    )
                except Exception:
                    continue
        return True
