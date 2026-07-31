# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError
from odoo.addons.nsp_core.utils import new_management_code


class Vehicle(models.Model):
    """Vehicle master owned by one NSP User with one active RFID Tag."""

    _name = "nsp.vehicle"
    _description = "Vehicle Management"
    _inherit = ["mail.thread", "mail.activity.mixin", "image.mixin"]
    _rec_name = "license_plate"
    _order = "license_plate, id"

    vehicle_code = fields.Char(
        string="Technical Code",
        required=True,
        readonly=True,
        copy=False,
        index=True,
        default=lambda self: new_management_code("VEH"),
        help="Stable system-generated identifier used for Cloud/Edge synchronization.",
    )
    license_plate = fields.Char(
        string="License Plate",
        required=True,
        tracking=True,
        index=True,
    )
    owner_id = fields.Many2one(
        "nsp.user",
        string="Owner",
        required=True,
        tracking=True,
        ondelete="restrict",
        index=True,
    )
    vehicle_type_id = fields.Many2one(
        "nsp.vehicle.type",
        string="Vehicle Type",
        ondelete="set null",
        tracking=True,
    )
    brand_id = fields.Many2one(
        "nsp.reference.brand",
        string="Brand",
        ondelete="set null",
        tracking=True,
        index=True,
    )
    model_id = fields.Many2one(
        "nsp.reference.model",
        string="Model",
        ondelete="set null",
        tracking=True,
        index=True,
    )
    color_id = fields.Many2one(
        "nsp.vehicle.color",
        string="Color",
        ondelete="set null",
        tracking=True,
    )
    active = fields.Boolean(default=True, tracking=True, index=True)

    tag_assignment_ids = fields.One2many(
        "nsp.rfid.tag.assignment",
        "vehicle_id",
        string="RFID Tag History",
        readonly=True,
    )
    active_tag_assignment_id = fields.Many2one(
        "nsp.rfid.tag.assignment",
        compute="_compute_active_tag",
        string="RFID Tag",
    )
    tid = fields.Char(
        compute="_compute_active_tag",
        string="RFID TID",
    )
    rfid_tid_input = fields.Char(
        string="Assign RFID TID",
        compute="_compute_rfid_tid_input",
        inverse="_inverse_rfid_tid_input",
        store=False,
        help="Scan or enter a TID. A missing whitelist record is created automatically.",
    )
    borrow_ids = fields.One2many(
        "nsp.vehicle.borrow",
        "vehicle_id",
        string="Authorized Users",
        help="Temporary vehicle-use permissions granted by the owner to accepted friends.",
    )

    _sql_constraints = [
        ("vehicle_code_uniq", "unique(vehicle_code)", "Vehicle Technical Code must be unique."),
        ("license_plate_uniq", "unique(license_plate)", "This license plate already exists in the system!"),
    ]

    @api.depends("tag_assignment_ids.state", "tag_assignment_ids.tid")
    def _compute_active_tag(self):
        assignments = self.env["nsp.rfid.tag.assignment"].sudo().search([
            ("vehicle_id", "in", self.ids),
            ("state", "=", "active"),
        ], order="assigned_at desc, id desc") if self.ids else self.env["nsp.rfid.tag.assignment"].browse()
        assignment_by_vehicle = {}
        for assignment in assignments:
            assignment_by_vehicle.setdefault(assignment.vehicle_id.id, assignment)
        empty = self.env["nsp.rfid.tag.assignment"].browse()
        for vehicle in self:
            assignment = assignment_by_vehicle.get(vehicle.id, empty)
            vehicle.active_tag_assignment_id = assignment
            vehicle.tid = assignment.tid if assignment else False

    @api.depends("tid")
    def _compute_rfid_tid_input(self):
        for vehicle in self:
            vehicle.rfid_tid_input = vehicle.tid or False

    def _assign_rfid_tid(self, raw_tid):
        Assignment = self.env["nsp.rfid.tag.assignment"]
        Tag = self.env["nsp.rfid.tag"]
        for vehicle in self:
            tid = Tag._normalize_tid(raw_tid)
            if not tid:
                continue
            if not vehicle.active:
                raise ValidationError(_("An archived Vehicle cannot receive an RFID Tag."))
            current = Assignment.sudo().active_for_vehicle(vehicle)
            if current:
                if current.tid == tid:
                    continue
                raise ValidationError(_(
                    "Vehicle %s already has an active RFID Tag. Revoke it before assigning another TID."
                ) % vehicle.display_name)
            tag = Tag.sudo().get_or_create_by_tid(tid)
            Assignment.sudo().with_context(
                rfid_audit_user_id=self.env.user.id,
            ).create({
                "tag_id": tag.id,
                "vehicle_id": vehicle.id,
            })

    def _inverse_rfid_tid_input(self):
        for vehicle in self:
            vehicle._assign_rfid_tid(vehicle.rfid_tid_input)

    @api.model
    def _normalize_license_plate(self, value):
        return " ".join(str(value or "").strip().upper().split()) or False

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        pending_tids = []
        for source in vals_list:
            vals = dict(source)
            pending_tids.append(vals.pop("rfid_tid_input", False))
            vals["vehicle_code"] = str(
                vals.get("vehicle_code") or new_management_code("VEH")
            ).strip().upper()
            vals["license_plate"] = self._normalize_license_plate(vals.get("license_plate"))
            prepared.append(vals)
        records = super().create(prepared)
        for vehicle, tid in zip(records, pending_tids):
            if tid:
                vehicle._assign_rfid_tid(tid)
        return records

    def write(self, vals):
        values = dict(vals)
        pending_tid = values.pop("rfid_tid_input", None)
        if values.get("vehicle_code"):
            values["vehicle_code"] = str(values["vehicle_code"]).strip().upper()
        if "license_plate" in values:
            values["license_plate"] = self._normalize_license_plate(values.get("license_plate"))
        if values.get("active") is False:
            self._revoke_rfid_tag(actor_user_id=self.env.user.id)
        result = super().write(values)
        if pending_tid is not None:
            self._assign_rfid_tid(pending_tid)
        return result

    @api.onchange("brand_id")
    def _onchange_brand_id(self):
        for record in self:
            if record.model_id and record.model_id.brand_id != record.brand_id:
                record.model_id = False

    @api.constrains("brand_id", "model_id")
    def _check_model_brand(self):
        for record in self:
            if record.model_id and record.model_id.brand_id != record.brand_id:
                raise ValidationError(_("Vehicle Model must belong to the selected Brand."))

    def _revoke_rfid_tag(self, actor_user_id=False):
        Assignment = self.env["nsp.rfid.tag.assignment"].sudo()
        for vehicle in self:
            Assignment.active_for_vehicle(vehicle).with_context(
                rfid_audit_user_id=actor_user_id or self.env.user.id,
            ).action_revoke()

    def action_revoke_rfid_tag(self):
        self._revoke_rfid_tag(actor_user_id=self.env.user.id)
        return True

    def action_archive(self):
        self.write({"active": False})
        return True

    def action_unarchive(self):
        self.write({"active": True})
        return True
