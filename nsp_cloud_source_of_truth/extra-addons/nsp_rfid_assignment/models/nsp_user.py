from odoo import api, fields, models


class NspUserRfidAssignment(models.Model):
    _inherit = "nsp.user"

    rfid_assignment_ids = fields.One2many(
        "nsp.rfid.tag.assignment",
        "user_id",
        readonly=True,
    )
    active_rfid_assignment_id = fields.Many2one(
        "nsp.rfid.tag.assignment",
        compute="_compute_rfid_assignment",
    )
    rfid_tag_id = fields.Many2one(
        "nsp.rfid.tag",
        compute="_compute_rfid_assignment",
    )
    rfid_tid = fields.Char(
        string="RFID TID",
        compute="_compute_rfid_assignment",
    )
    rfid_tid_input = fields.Char(
        string="Scan RFID Tag",
        compute="_compute_rfid_tid_input",
        inverse="_inverse_rfid_tid_input",
        store=False,
    )

    @api.depends("rfid_assignment_ids.state", "rfid_assignment_ids.tag_id")
    def _compute_rfid_assignment(self):
        Assignment = self.env["nsp.rfid.tag.assignment"].sudo()
        assignments = Assignment.search([
            ("user_id", "in", self.ids),
            ("state", "=", "active"),
        ], order="assigned_at desc, id desc") if self.ids else Assignment.browse()
        by_user = {}
        for assignment in assignments:
            by_user.setdefault(assignment.user_id.id, assignment)
        empty = Assignment.browse()
        for user in self:
            assignment = by_user.get(user.id, empty)
            user.active_rfid_assignment_id = assignment
            user.rfid_tag_id = assignment.tag_id if assignment else False
            user.rfid_tid = assignment.tid if assignment else False

    @api.depends("rfid_tid")
    def _compute_rfid_tid_input(self):
        for user in self:
            user.rfid_tid_input = user.rfid_tid or False

    def _inverse_rfid_tid_input(self):
        Assignment = self.env["nsp.rfid.tag.assignment"]
        for user in self:
            if user.rfid_tid_input:
                Assignment.assign_tid(user, user.rfid_tid_input)

    def action_revoke_rfid_tag(self):
        for user in self:
            self.env["nsp.rfid.tag.assignment"].revoke_target(user)
        return True

    def write(self, vals):
        if vals.get("active") is False:
            assignments = self.env["nsp.rfid.tag.assignment"].sudo().search([
                ("user_id", "in", self.ids),
                ("state", "=", "active"),
            ])
            assignments.action_revoke()
        return super().write(vals)
