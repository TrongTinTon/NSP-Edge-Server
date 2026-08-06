from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from .rfid_target_helpers import reload_action


class NspRfidTagAssignmentExtension(models.Model):
    _inherit = "nsp.rfid.tag"

    assignment_ids = fields.One2many(
        "nsp.rfid.tag.assignment",
        "tag_id",
        readonly=True,
    )
    active_assignment_id = fields.Many2one(
        "nsp.rfid.tag.assignment",
        compute="_compute_active_assignment",
        compute_sudo=True,
        store=True,
    )
    is_assigned = fields.Boolean(
        compute="_compute_active_assignment",
        compute_sudo=True,
        store=True,
    )
    active_target_type = fields.Selection(
        related="active_assignment_id.target_type",
        readonly=True,
    )
    active_target_name = fields.Char(
        related="active_assignment_id.target_name",
        readonly=True,
    )

    @api.depends("assignment_ids.state", "assignment_ids.assigned_at")
    def _compute_active_assignment(self):
        Assignment = self.env["nsp.rfid.tag.assignment"].sudo()
        tag_ids = [tag.id for tag in self if isinstance(tag.id, int)]
        assignments = Assignment.search(
            [("tag_id", "in", tag_ids), ("state", "=", "active")],
            order="assigned_at desc, id desc",
        ) if tag_ids else Assignment.browse()

        assignment_by_tag = {}
        for assignment in assignments:
            assignment_by_tag.setdefault(assignment.tag_id.id, assignment)

        empty = Assignment.browse()
        for tag in self:
            assignment = assignment_by_tag.get(tag.id, empty)
            tag.active_assignment_id = assignment
            tag.is_assigned = bool(assignment)

    def write(self, vals):
        values = dict(vals)
        if "tid" in values:
            normalized = self._prepare_tid(values.get("tid"))
            changed_tags = self.filtered(lambda tag: tag.tid != normalized)
            if changed_tags and self.env["nsp.rfid.tag.assignment"].sudo().search_count(
                [("tag_id", "in", changed_tags.ids)]
            ):
                raise ValidationError(
                    _(
                        "A TID with assignment history cannot be changed. "
                        "Create a new whitelist TID instead."
                    )
                )
            values["tid"] = normalized
        return super().write(values)

    def action_revoke_active_assignment(self):
        assignments = self.mapped("active_assignment_id").filtered(
            lambda assignment: assignment.state == "active"
        )
        if assignments:
            assignments.sudo().with_context(
                rfid_audit_user_id=self.env.user.id
            ).action_revoke()
        return reload_action()

    @staticmethod
    def _invalid_scan_response(tag, message):
        return {
            "valid": False,
            "tid": tag.tid,
            "tag_id": tag.id,
            "message": message,
        }

    @api.model
    def nsp_validate_scan(
        self,
        tid,
        require_available=False,
        allow_tag_id=False,
        require_active_assignment=False,
        expected_target=False,
        create_missing=False,
        **kwargs,
    ):
        result = super().nsp_validate_scan(
            tid,
            create_missing=create_missing,
            **kwargs,
        )
        if not result.get("valid"):
            return result

        tag = self.sudo().browse(result["tag_id"]).exists()
        assignment = self.env["nsp.rfid.tag.assignment"].sudo().search(
            [("tag_id", "=", tag.id), ("state", "=", "active")],
            limit=1,
        )
        allowed_tag_id = int(allow_tag_id or 0)

        if require_available and assignment and tag.id != allowed_tag_id:
            return self._invalid_scan_response(
                tag,
                _("RFID Tag %s is already assigned.") % tag.tid,
            )
        if require_active_assignment and not assignment:
            return self._invalid_scan_response(
                tag,
                _("RFID Tag %s has no active assignment.") % tag.tid,
            )

        expected = str(expected_target or "").strip().lower()
        if expected == "user" and (not assignment or not assignment.user_id):
            return self._invalid_scan_response(
                tag,
                _("RFID Tag %s is not assigned to an active User.") % tag.tid,
            )
        if expected == "vehicle" and (
            not assignment or not assignment.vehicle_id
        ):
            return self._invalid_scan_response(
                tag,
                _("RFID Tag %s is not assigned to an active Vehicle.") % tag.tid,
            )
        if (
            expected == "vehicle_or_available"
            and assignment
            and not assignment.vehicle_id
        ):
            return self._invalid_scan_response(
                tag,
                _("RFID Tag %s is assigned to a User.") % tag.tid,
            )

        result.update(
            {
                "assignment_id": assignment.id if assignment else False,
                "user_id": assignment.user_id.id
                if assignment and assignment.user_id
                else False,
                "vehicle_id": assignment.vehicle_id.id
                if assignment and assignment.vehicle_id
                else False,
                "license_plate": assignment.vehicle_id.license_plate
                if assignment and assignment.vehicle_id
                else False,
                "owner_id": assignment.vehicle_id.owner_id.id
                if assignment
                and assignment.vehicle_id
                and assignment.vehicle_id.owner_id
                else False,
                "owner_name": assignment.vehicle_id.owner_id.display_name
                if assignment
                and assignment.vehicle_id
                and assignment.vehicle_id.owner_id
                else False,
            }
        )
        return result
