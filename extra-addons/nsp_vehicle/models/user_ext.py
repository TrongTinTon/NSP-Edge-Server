# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class NspUserVehicleExtension(models.Model):
    _inherit = "nsp.user"

    employee_tag_assignment_ids = fields.One2many(
        "nsp.rfid.tag.assignment",
        "user_id",
        string="Employee RFID Tag Assignments",
        readonly=True,
    )
    employee_tag_assignment_id = fields.Many2one(
        "nsp.rfid.tag.assignment",
        compute="_compute_employee_tag",
        string="Employee RFID Tag",
    )
    employee_tid = fields.Char(
        compute="_compute_employee_tag",
        string="Employee TID",
    )
    employee_tid_input = fields.Char(
        string="Assign Employee TID",
        compute="_compute_employee_tid_input",
        inverse="_inverse_employee_tid_input",
        store=False,
        help="Scan or enter a TID. A missing whitelist record is created automatically.",
    )
    vehicle_ids = fields.One2many("nsp.vehicle", "owner_id", string="Vehicles")
    vehicle_count = fields.Integer(compute="_compute_vehicle_count")

    @api.depends("employee_tag_assignment_ids.state", "employee_tag_assignment_ids.tid")
    def _compute_employee_tag(self):
        assignments = self.env["nsp.rfid.tag.assignment"].sudo().search([
            ("user_id", "in", self.ids),
            ("state", "=", "active"),
        ], order="assigned_at desc, id desc") if self.ids else self.env["nsp.rfid.tag.assignment"].browse()
        assignment_by_user = {}
        for assignment in assignments:
            assignment_by_user.setdefault(assignment.user_id.id, assignment)
        empty = self.env["nsp.rfid.tag.assignment"].browse()
        for user in self:
            assignment = assignment_by_user.get(user.id, empty)
            user.employee_tag_assignment_id = assignment
            user.employee_tid = assignment.tid if assignment else False

    @api.depends("employee_tid")
    def _compute_employee_tid_input(self):
        for user in self:
            user.employee_tid_input = user.employee_tid or False

    def _assign_employee_tid(self, raw_tid):
        Assignment = self.env["nsp.rfid.tag.assignment"]
        Tag = self.env["nsp.rfid.tag"]
        for user in self:
            tid = Tag._normalize_tid(raw_tid)
            if not tid:
                continue
            if not user.active:
                raise ValidationError(_("An archived User cannot receive an Employee RFID Tag."))
            current = Assignment.sudo().active_for_user(user)
            if current:
                if current.tid == tid:
                    continue
                raise ValidationError(_(
                    "User %s already has an active Employee RFID Tag. Revoke it before assigning another TID."
                ) % user.display_name)
            tag = Tag.sudo().get_or_create_by_tid(tid)
            Assignment.sudo().with_context(
                rfid_audit_user_id=self.env.user.id,
            ).create({
                "tag_id": tag.id,
                "user_id": user.id,
            })

    def _inverse_employee_tid_input(self):
        for user in self:
            user._assign_employee_tid(user.employee_tid_input)

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        pending_tids = []
        for source in vals_list:
            vals = dict(source)
            pending_tids.append(vals.pop("employee_tid_input", False))
            prepared.append(vals)
        records = super().create(prepared)
        for user, tid in zip(records, pending_tids):
            if tid:
                user._assign_employee_tid(tid)
        return records

    def write(self, vals):
        values = dict(vals)
        pending_tid = values.pop("employee_tid_input", None)
        archive = values.get("active") is False
        if archive:
            self._revoke_employee_tag(actor_user_id=self.env.user.id)
        result = super().write(values)
        if pending_tid is not None:
            self._assign_employee_tid(pending_tid)
        return result

    @api.depends("vehicle_ids")
    def _compute_vehicle_count(self):
        counts = self.env["nsp.vehicle"].sudo()._read_group(
            [("owner_id", "in", self.ids)],
            ["owner_id"],
            ["__count"],
        ) if self.ids else []
        count_by_owner = {owner.id: count for owner, count in counts}
        for user in self:
            user.vehicle_count = count_by_owner.get(user.id, 0)

    def _revoke_employee_tag(self, actor_user_id=False):
        Assignment = self.env["nsp.rfid.tag.assignment"].sudo()
        for user in self:
            assignment = Assignment.active_for_user(user)
            if not assignment:
                continue
            assignment.with_context(
                rfid_audit_user_id=actor_user_id or self.env.user.id,
            ).action_revoke()

    def action_revoke_employee_tag(self):
        self._revoke_employee_tag(actor_user_id=self.env.user.id)
        return True
