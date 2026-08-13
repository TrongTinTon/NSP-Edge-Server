# -*- coding: utf-8 -*-
"""Lane Calibration state-transition policy used by Edge API and sync paths."""

from odoo import _, models

from .state_policy import validate_state_transition


_MEASUREMENT_STATUS_TRANSITIONS = {
    "draft": frozenset({"ready", "cancelled"}),
    "ready": frozenset({"running", "completed", "failed", "cancelled"}),
    "running": frozenset({"completed", "failed", "cancelled"}),
    "completed": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}

class NspMeasurementSessionStatePolicy(models.Model):
    _inherit = "nsp.measurement.session"

    def _validate_status_transition(self, target_status, *, allow_same=True):
        self.ensure_one()
        return validate_state_transition(
            self.status,
            target_status,
            _MEASUREMENT_STATUS_TRANSITIONS,
            label=_("Lane Calibration"),
            allow_same=allow_same,
        )

    def _apply_status_transition(self, target_status, extra_values=None, *, allow_same=True):
        for session in self:
            session._validate_status_transition(target_status, allow_same=allow_same)
            values = dict(extra_values or {})
            values["status"] = target_status
            session.with_context(measurement_sync=True).write(values)
        return True
