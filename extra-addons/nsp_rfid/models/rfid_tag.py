# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class NspRfidTag(models.Model):
    """Authoritative whitelist of RFID TIDs.

    The whitelist intentionally owns only the physical TID. Whether a tag is
    currently used by an employee or a vehicle is derived from the active
    ``nsp.rfid.tag.assignment`` record and is never stored as a tag type.
    """

    _name = "nsp.rfid.tag"
    _description = "NSP RFID Tag Whitelist"
    _rec_name = "tid"
    _order = "tid, id"

    tid = fields.Char(
        string="TID",
        required=True,
        index=True,
        help="Normalized RFID Tag Identifier allowed by NSP.",
    )

    _sql_constraints = [
        ("tid_unique", "unique(tid)", "TID must be unique in RFID Tag Whitelist."),
    ]

    @api.model
    def _normalize_tid(self, value):
        return "".join(str(value or "").strip().upper().split())

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for source in vals_list:
            vals = dict(source)
            vals["tid"] = self._normalize_tid(vals.get("tid"))
            prepared.append(vals)
        return super().create(prepared)

    def write(self, vals):
        values = dict(vals)
        if "tid" in values:
            normalized = self._normalize_tid(values.get("tid"))
            changed = self.filtered(lambda tag: tag.tid != normalized)
            if changed and "nsp.rfid.tag.assignment" in self.env.registry.models:
                assigned = self.env["nsp.rfid.tag.assignment"].sudo().search_count([
                    ("tag_id", "in", changed.ids),
                ])
                if assigned:
                    raise ValidationError(_(
                        "A TID with assignment history cannot be changed. Create a new whitelist TID instead."
                    ))
            values["tid"] = normalized
        return super().write(values)

    def unlink(self):
        if not self.env.context.get("module_uninstall"):
            raise ValidationError(_(
                "RFID Tag Whitelist history cannot be deleted. Leave the TID unassigned instead."
            ))
        return super().unlink()

    @api.constrains("tid")
    def _check_tid(self):
        for tag in self:
            normalized = tag._normalize_tid(tag.tid)
            if not normalized:
                raise ValidationError(_("TID is required."))
            if tag.tid != normalized:
                raise ValidationError(_("TID must be uppercase without whitespace."))

    @api.model
    def get_or_create_by_tid(self, tid):
        normalized = self._normalize_tid(tid)
        if not normalized:
            raise ValidationError(_("TID is required."))
        tag = self.sudo().search([("tid", "=", normalized)], limit=1)
        return tag or self.sudo().create({"tid": normalized})

    @api.model
    def nsp_validate_scan(
        self,
        tid,
        require_available=False,
        allow_tag_id=False,
        require_active_assignment=False,
        expected_target=False,
        create_missing=False,
    ):
        """Validate a keyboard-scanned TID without persisting scan state.

        ``expected_target`` may be ``user``, ``vehicle`` or
        ``vehicle_or_available``. The latter accepts an unassigned whitelist TID
        or a TID already assigned to a Vehicle, but rejects User assignments.
        """
        normalized = self._normalize_tid(tid)
        if not normalized:
            return {"valid": False, "message": _("Scan or enter an RFID TID first.")}

        tag = self.sudo().search([("tid", "=", normalized)], limit=1)
        if not tag and create_missing:
            tag = self.sudo().create({"tid": normalized})
        if not tag:
            return {
                "valid": False,
                "tid": normalized,
                "message": _("RFID Tag %s is not in RFID Tag Whitelist.") % normalized,
            }

        assignment = False
        if "nsp.rfid.tag.assignment" in self.env.registry.models:
            assignment = self.env["nsp.rfid.tag.assignment"].sudo().search([
                ("tag_id", "=", tag.id),
                ("state", "=", "active"),
            ], limit=1)

        allowed_tag_id = int(allow_tag_id or 0)
        if require_available and assignment and tag.id != allowed_tag_id:
            return {
                "valid": False,
                "tid": normalized,
                "tag_id": tag.id,
                "message": _("RFID Tag %s is already assigned.") % normalized,
            }

        expected = str(expected_target or "").strip().lower()
        if require_active_assignment and not assignment:
            return {
                "valid": False,
                "tid": normalized,
                "tag_id": tag.id,
                "message": _("RFID Tag %s has no active assignment.") % normalized,
            }
        if expected == "user" and (not assignment or not assignment.user_id):
            return {
                "valid": False,
                "tid": normalized,
                "tag_id": tag.id,
                "message": _("RFID Tag %s is not assigned to an active User.") % normalized,
            }
        if expected == "vehicle" and (not assignment or not assignment.vehicle_id):
            return {
                "valid": False,
                "tid": normalized,
                "tag_id": tag.id,
                "message": _("RFID Tag %s is not assigned to an active Vehicle.") % normalized,
            }
        if expected == "vehicle_or_available" and assignment and not assignment.vehicle_id:
            return {
                "valid": False,
                "tid": normalized,
                "tag_id": tag.id,
                "message": _("RFID Tag %s is assigned to a User and cannot be used for a Vehicle.") % normalized,
            }

        result = {
            "valid": True,
            "tid": tag.tid,
            "tag_id": tag.id,
            "assignment_id": assignment.id if assignment else False,
            "user_id": assignment.user_id.id if assignment and assignment.user_id else False,
            "vehicle_id": assignment.vehicle_id.id if assignment and assignment.vehicle_id else False,
            "license_plate": assignment.vehicle_id.license_plate if assignment and assignment.vehicle_id else False,
            "owner_id": (
                assignment.vehicle_id.owner_id.id
                if assignment and assignment.vehicle_id and assignment.vehicle_id.owner_id
                else False
            ),
            "owner_name": (
                assignment.vehicle_id.owner_id.display_name
                if assignment and assignment.vehicle_id and assignment.vehicle_id.owner_id
                else False
            ),
            "message": _("RFID Tag is valid."),
        }
        return result
