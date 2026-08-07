# -*- coding: utf-8 -*-
"""State policies for calibration Runs, Results and Validation Runs."""

from odoo import _, models

from .state_policy import validate_state_transition


_PASS_STATE_TRANSITIONS = {
    "running": frozenset({"completed"}),
    "completed": frozenset({"accepted", "rejected"}),
    "accepted": frozenset({"rejected"}),
    "rejected": frozenset(),
}

_RESULT_STATE_TRANSITIONS = {
    "draft": frozenset({"validation"}),
    "validation": frozenset({"accepted"}),
    "accepted": frozenset({"superseded"}),
    "superseded": frozenset(),
}

_VALIDATION_RUN_STATE_TRANSITIONS = {
    "draft": frozenset({"running"}),
    "running": frozenset({"completed", "passed", "failed"}),
    "completed": frozenset({"passed", "failed"}),
    "passed": frozenset(),
    "failed": frozenset(),
}


class NspMeasurementPassStatePolicy(models.Model):
    _inherit = "nsp.measurement.pass"

    def _apply_pass_state(self, target_state, extra_values=None, *, allow_same=True):
        for record in self:
            validate_state_transition(
                record.state, target_state, _PASS_STATE_TRANSITIONS,
                label=_("Calibration Run"), allow_same=allow_same,
            )
            values = dict(extra_values or {})
            values["state"] = target_state
            record.write(values)
        return True


class NspMeasurementResultStatePolicy(models.Model):
    _inherit = "nsp.measurement.result"

    def _apply_result_state(self, target_state, extra_values=None, *, allow_same=True):
        for record in self:
            validate_state_transition(
                record.state, target_state, _RESULT_STATE_TRANSITIONS,
                label=_("Calibration Result"), allow_same=allow_same,
            )
            values = dict(extra_values or {})
            values["state"] = target_state
            record.write(values)
        return True


class NspMeasurementValidationRunStatePolicy(models.Model):
    _inherit = "nsp.measurement.validation.run"

    def _apply_validation_run_state(self, target_state, extra_values=None, *, allow_same=True):
        for record in self:
            validate_state_transition(
                record.state, target_state, _VALIDATION_RUN_STATE_TRANSITIONS,
                label=_("Validation Run"), allow_same=allow_same,
            )
            values = dict(extra_values or {})
            values["state"] = target_state
            record.write(values)
        return True
