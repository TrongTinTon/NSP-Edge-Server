# -*- coding: utf-8 -*-
"""State policy and public actions for Lane Calibration.

The policy is intentionally isolated from the persistence-heavy measurement
model so every UI/RPC entry point uses the same transition rules.
"""

from odoo import _, fields, models
from odoo.exceptions import UserError, ValidationError

from .state_policy import validate_state_transition


_MEASUREMENT_STATUS_TRANSITIONS = {
    "draft": frozenset({"ready", "cancelled"}),
    "ready": frozenset({"running", "completed", "failed", "cancelled"}),
    "running": frozenset({"completed", "failed", "cancelled"}),
    "completed": frozenset({"applied"}),
    "failed": frozenset(),
    "applied": frozenset(),
    "cancelled": frozenset(),
}

_REVISION_TARGET_SOURCES = {
    "ready": frozenset({"running", "completed", "failed"}),
    "draft": frozenset({"completed", "failed", "applied"}),
}


class NspMeasurementSessionStatePolicy(models.Model):
    _inherit = "nsp.measurement.session"

    def _check_public_action_access(self, operation="write"):
        records = self.exists()
        if len(records) != len(self):
            raise ValidationError(_("The requested Lane Calibration no longer exists."))
        records.check_access(operation)
        return records

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


    def _release_new_revision(self, target_status="ready"):
        self.ensure_one()
        allowed_sources = _REVISION_TARGET_SOURCES.get(target_status, frozenset())
        if self.status not in allowed_sources:
            raise ValidationError(_(
                "Lane Calibration cannot create a new %(target)s revision from %(current)s."
            ) % {"target": target_status, "current": self.status})
        self.with_context(measurement_sync=True).write({
            "revision": int(self.revision or 1) + 1,
            "status": target_status,
            "started_at": False,
            "ended_at": False,
            "applied_at": False,
        })
        return self.get_live_snapshot(self.id)

    def action_ready(self):
        sessions = self._check_public_action_access("write")
        for session in sessions:
            if session.status == "ready":
                continue
            session._require_ready_configuration()
            session._apply_status_transition("ready", allow_same=False)
        return True

    def action_complete(self):
        sessions = self._check_public_action_access("write")
        for session in sessions:
            if session.status == "completed":
                continue
            if session.status != "running":
                raise ValidationError(_("Only a running Lane Calibration can be completed."))
            session._apply_status_transition(
                "completed",
                {"ended_at": fields.Datetime.now()},
                allow_same=False,
            )
        return True

    def action_cancel(self):
        sessions = self._check_public_action_access("write")
        for session in sessions:
            session._apply_status_transition(
                "cancelled",
                {"ended_at": fields.Datetime.now()},
                allow_same=True,
            )
        return True

    def action_prepare_device_reconfiguration(self):
        """Create a new editable revision without changing historical results."""
        self.ensure_one()
        self._check_public_action_access("write")
        if self._deployment_role() != "cloud":
            raise UserError(_("Device reconfiguration is owned by the Cloud Master."))
        running_passes = getattr(self, "pass_ids", self.browse()).filtered(
            lambda item: item.state == "running"
        )
        if running_passes:
            raise ValidationError(_("Stop the running Run before changing devices."))
        self._release_new_revision("draft")
        action = self.action_open_session_form()
        action["name"] = _("Revise · R%(revision)s") % {"revision": self.revision}
        action["context"] = {
            **dict(action.get("context") or {}),
            "form_view_initial_mode": "edit",
            "nsp_device_reconfiguration": True,
        }
        return action

    def action_measure_again(self, reader_settings=None):
        self.ensure_one()
        self._check_public_action_access("write")
        if self.status not in _REVISION_TARGET_SOURCES["ready"]:
            raise ValidationError(_(
                "Measure Again is available for running, completed, or failed sessions."
            ))
        self._require_ready_configuration()
        if reader_settings not in (None, False, ""):
            if not isinstance(reader_settings, list):
                raise ValidationError(_("Reader settings must be a list."))
            line_by_id = {line.id: line for line in self.reader_line_ids}
            seen = set()
            updates_by_values = {}
            for item in reader_settings:
                if not isinstance(item, dict):
                    raise ValidationError(_("Invalid Reader settings."))
                try:
                    line_id = int(item.get("reader_line_id") or 0)
                    power = int(item.get("power_dbm"))
                    interval = int(item.get("read_interval_ms"))
                except (TypeError, ValueError) as exc:
                    raise ValidationError(_("Invalid Reader settings.")) from exc
                line = line_by_id.get(line_id)
                if not line or line_id in seen:
                    raise ValidationError(_(
                        "Reader settings do not match this Lane Calibration."
                    ))
                seen.add(line_id)
                updates_by_values.setdefault((power, interval), self.env[line._name].browse())
                updates_by_values[(power, interval)] |= line
            for (power, interval), lines in updates_by_values.items():
                lines.with_context(measurement_sync=True).write({
                    "reader_power_dbm": power,
                    "read_interval_ms": interval,
                })
        return self._release_new_revision("ready")

    def action_apply_reader_settings(self, reader_line_id, power_dbm, read_interval_ms):
        """Apply one Reader configuration and release a new shared revision."""
        self.ensure_one()
        self._check_public_action_access("write")
        if self.status not in _REVISION_TARGET_SOURCES["ready"]:
            raise ValidationError(_(
                "Reader settings can be applied only while running, completed, or failed."
            ))
        try:
            line_id = int(reader_line_id or 0)
            power = int(power_dbm)
            interval = int(read_interval_ms)
        except (TypeError, ValueError) as exc:
            raise ValidationError(_("Invalid Reader settings.")) from exc
        line = self.reader_line_ids.filtered(lambda item: item.id == line_id)[:1]
        if not line:
            raise ValidationError(_("Reader does not belong to this Lane Calibration."))
        line.with_context(measurement_sync=True).write({
            "reader_power_dbm": power,
            "read_interval_ms": interval,
        })
        self._require_ready_configuration()
        return self._release_new_revision("ready")

    def action_apply_to_operation(self):
        self.ensure_one()
        self._check_public_action_access("write")
        if self._deployment_role() != "cloud":
            raise UserError(_("Applying calibration results is owned by the Cloud Master."))
        if self.status != "completed":
            raise ValidationError(_(
                "Complete the Lane Calibration before applying its result to a Lane configuration."
            ))
        self._require_ready_configuration()

        readers_by_settings = {}
        for line in self.reader_line_ids:
            values = (
                int(line.reader_power_dbm or 0),
                int(line.read_interval_ms or 200),
            )
            readers_by_settings.setdefault(values, self.env[line.reader_id._name].browse())
            readers_by_settings[values] |= line.reader_id
        for (power, interval), readers in readers_by_settings.items():
            # Scope and ownership were validated on the selected Reader lines above.
            readers.sudo().write({
                "power_dbm": power,
                "read_interval_ms": interval,
            })

        self._apply_status_transition(
            "applied",
            {"applied_at": fields.Datetime.now()},
            allow_same=False,
        )
        return self.get_live_snapshot(self.id)
