# -*- coding: utf-8 -*-
"""Single state policy for Parking Area runtime lifecycle."""

from odoo import _, models
from odoo.exceptions import UserError, ValidationError

from .state_policy import validate_state_transition


_PARKING_AREA_STATE_TRANSITIONS = {
    "draft": frozenset({"operational"}),
    "operational": frozenset({"maintenance", "blocked", "draft"}),
    "maintenance": frozenset({"operational", "blocked", "draft"}),
    "blocked": frozenset({"operational", "maintenance", "draft"}),
}


class NspParkingAreaStatePolicy(models.Model):
    _inherit = "nsp.parking.area"

    def _check_parking_action_access(self, operation="write"):
        records = self.exists()
        if len(records) != len(self):
            raise ValidationError(_("The requested Parking Area no longer exists."))
        records.check_access(operation)
        return records

    def _validate_parking_state_transition(self, target_state, *, allow_same=True):
        self.ensure_one()
        return validate_state_transition(
            self.state,
            target_state,
            _PARKING_AREA_STATE_TRANSITIONS,
            label=_("Parking Area"),
            allow_same=allow_same,
        )

    def _apply_parking_state_transition(
        self, target_state, extra_values=None, *, allow_same=True, force=False,
    ):
        for area in self:
            if not force:
                area._validate_parking_state_transition(target_state, allow_same=allow_same)
            values = dict(extra_values or {})
            values["state"] = target_state
            area.write(values)
        return True


class NspParkingAreaRuntimeActions(models.Model):
    _inherit = "nsp.parking.area"

    def action_set_operational(self):
        areas = self._check_parking_action_access("write")
        for area in areas:
            issues = area._operational_issues()
            if issues:
                raise UserError("\n".join(issues))
        return areas._apply_parking_state_transition("operational")

    def action_reset_to_draft(self):
        areas = self._check_parking_action_access("write")
        return areas._apply_parking_state_transition("draft")

    def action_set_maintenance(self):
        areas = self._check_parking_action_access("write")
        return areas._apply_parking_state_transition("maintenance")

    def action_set_blocked(self):
        areas = self._check_parking_action_access("write")
        return areas._apply_parking_state_transition("blocked")
