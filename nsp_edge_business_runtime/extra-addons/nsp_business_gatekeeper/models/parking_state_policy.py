# -*- coding: utf-8 -*-
"""Single state policy for Parking Area runtime lifecycle."""

from odoo import _, models

from .state_policy import validate_state_transition


_PARKING_AREA_STATE_TRANSITIONS = {
    "draft": frozenset({"operational"}),
    "operational": frozenset({"maintenance", "blocked", "draft"}),
    "maintenance": frozenset({"operational", "blocked", "draft"}),
    "blocked": frozenset({"operational", "maintenance", "draft"}),
}


class NspParkingAreaStatePolicy(models.Model):
    _inherit = "nsp.parking.area"

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
