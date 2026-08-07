# -*- coding: utf-8 -*-
"""Public actions and the single state machine for Lane Calibration."""

from odoo import _, fields, models
from odoo.exceptions import UserError, ValidationError



class CalibrationStatusPolicy:
    """Authoritative state, runtime-status, and revision policy."""

    TRANSITIONS = {
        "draft": frozenset({"ready", "cancelled"}),
        "ready": frozenset({"running", "cancelled"}),
        "running": frozenset({"completed", "failed", "cancelled"}),
        "completed": frozenset({"applied"}),
        "applied": frozenset(),
        "failed": frozenset({"ready", "cancelled"}),
        "cancelled": frozenset(),
    }

    REVISION_SOURCES = {
        "ready": frozenset({"running", "completed", "failed"}),
        "draft": frozenset({"completed", "failed", "applied"}),
    }

    CLOUD_STATUSES = frozenset({"draft", "ready", "applied"})
    RUNTIME_STATUSES = frozenset({"running", "completed", "failed", "cancelled"})
    ALL_STATUSES = CLOUD_STATUSES | RUNTIME_STATUSES

    STALE_RUNTIME_TARGETS = {
        "completed": frozenset({"running"}),
        "failed": frozenset({"running"}),
        "cancelled": frozenset({"running"}),
    }

    @classmethod
    def validate_transition(cls, current, target, *, allow_same=True):
        current_state = str(current or "").strip()
        target_state = str(target or "").strip()
        if not target_state:
            raise ValidationError(_("Target state is required."))
        if allow_same and current_state == target_state:
            return target_state
        if target_state not in cls.TRANSITIONS.get(current_state, frozenset()):
            raise ValidationError(_(
                "Lane Calibration cannot move from %(current)s to %(target)s."
            ) % {
                "current": current_state or "-",
                "target": target_state,
            })
        return target_state

    @classmethod
    def validate_revision_source(cls, current, target_status):
        allowed_sources = cls.REVISION_SOURCES.get(target_status, frozenset())
        if current not in allowed_sources:
            raise ValidationError(_(
                "Lane Calibration cannot create a new %(target)s revision from %(current)s."
            ) % {"target": target_status, "current": current})
        return True

    @classmethod
    def classify_revision(cls, incoming, current):
        try:
            incoming_revision = int(incoming)
            current_revision = int(current)
        except (TypeError, ValueError) as exc:
            raise ValidationError(_("Revision must be an integer.")) from exc
        if incoming_revision <= 0 or current_revision <= 0:
            raise ValidationError(_("Revision must be greater than zero."))
        if incoming_revision < current_revision:
            return "stale"
        if incoming_revision > current_revision:
            return "future"
        return "current"

    @classmethod
    def classify_runtime_status(cls, current, target, incoming_revision, current_revision):
        target_status = str(target or "").strip().lower()
        current_status = str(current or "draft").strip().lower()
        if target_status not in cls.ALL_STATUSES:
            raise ValueError("invalid_lane_calibration_status")

        relation = cls.classify_revision(incoming_revision, current_revision)
        result = {
            "outcome": "duplicate",
            "incoming_status": target_status,
            "current_status": current_status,
            "incoming_revision": int(incoming_revision),
            "current_revision": int(current_revision),
            "status_owner": "cloud" if target_status in cls.CLOUD_STATUSES else "runtime",
        }
        if relation == "stale":
            result["outcome"] = "ignored_stale_revision"
            return result
        if relation == "future":
            raise ValueError("lane_calibration_revision_ahead")
        if target_status in cls.CLOUD_STATUSES and target_status != current_status:
            result["outcome"] = "ignored_cloud_owned_status"
            return result
        if current_status == "applied" and target_status in cls.RUNTIME_STATUSES:
            result["outcome"] = "ignored_after_configured"
            return result
        if target_status != current_status:
            if target_status in cls.STALE_RUNTIME_TARGETS.get(current_status, frozenset()):
                result["outcome"] = "ignored_stale_status"
                return result
            try:
                cls.validate_transition(current_status, target_status, allow_same=False)
            except ValidationError as exc:
                raise ValueError("invalid_status_transition") from exc
        return result


CALIBRATION_TRANSITIONS = CalibrationStatusPolicy.TRANSITIONS


class NspMeasurementSessionStatus(models.Model):
    _inherit = "nsp.measurement.session"

    def _check_public_action_access(self, operation="write"):
        records = self.exists()
        if len(records) != len(self):
            raise ValidationError(_("The requested Lane Calibration no longer exists."))
        records.check_access(operation)
        return records

    def _validate_status_transition(self, target_status, *, allow_same=True):
        self.ensure_one()
        return CalibrationStatusPolicy.validate_transition(
            self.status, target_status, allow_same=allow_same
        )

    def _apply_status_transition(self, target_status, extra_values=None, *, allow_same=True):
        for session in self:
            session._validate_status_transition(target_status, allow_same=allow_same)
            values = dict(extra_values or {})
            values["status"] = target_status
            session.with_context(measurement_sync=True).write(values)
        return True

    def _require_ready_configuration(self):
        self.ensure_one()
        missing = []
        if len(self.target_line_ids) != 1:
            missing.append(_("exactly one raw Calibration Tag"))
        if not self.reader_line_ids:
            missing.append(_("Readers"))
        if missing:
            raise ValidationError(
                _("Missing Lane Calibration configuration: %s") % ", ".join(missing)
            )
        missing_ports = self.reader_line_ids.filtered(lambda line: not line.reader_port_ids)
        if missing_ports:
            names = ", ".join(missing_ports.mapped("reader_id.display_name"))
            raise ValidationError(
                _("Select at least one Reader Port for each RFID Reader. Missing: %s") % names
            )
        self._validate_measurement_scope()
        return True

    def _release_new_revision(self, target_status="ready"):
        self.ensure_one()
        CalibrationStatusPolicy.validate_revision_source(self.status, target_status)
        self.with_context(measurement_sync=True).write({
            "revision": int(self.revision or 1) + 1,
            "status": target_status,
            "started_at": False,
            "ended_at": False,
            "applied_at": False,
        })
        return self.get_live_snapshot(self.id)

    def _set_ready(self):
        sessions = self._check_public_action_access("write")
        for session in sessions:
            if session.status == "ready":
                continue
            session._require_ready_configuration()
            session._apply_status_transition("ready", allow_same=False)
        return True

    def _complete_calibration(self):
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

    def _cancel_calibration(self):
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
        if self.status not in CalibrationStatusPolicy.REVISION_SOURCES["ready"]:
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
        if self.status not in CalibrationStatusPolicy.REVISION_SOURCES["ready"]:
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


_PASS_STATE_TRANSITIONS = {
    "running": frozenset({"completed"}),
    "completed": frozenset({"accepted", "rejected"}),
    "accepted": frozenset({"rejected"}),
    "rejected": frozenset(),
}

_RESULT_STATE_TRANSITIONS = {
    "draft": frozenset({"accepted"}),
    "accepted": frozenset({"superseded"}),
    "superseded": frozenset(),
}


def _validate_domain_transition(current, target, transitions, label, allow_same=True):
    if allow_same and current == target:
        return True
    if target not in transitions.get(current, frozenset()):
        raise ValidationError(
            _("%(label)s cannot move from %(current)s to %(target)s.")
            % {"label": label, "current": current or "-", "target": target or "-"}
        )
    return True


class NspMeasurementPassStatePolicy(models.Model):
    _inherit = "nsp.measurement.pass"

    def _apply_pass_state(self, target_state, extra_values=None, *, allow_same=True):
        for record in self:
            _validate_domain_transition(
                record.state, target_state, _PASS_STATE_TRANSITIONS, _("Calibration Run"), allow_same
            )
            values = dict(extra_values or {})
            values["state"] = target_state
            record.write(values)
        return True


class NspMeasurementResultStatePolicy(models.Model):
    _inherit = "nsp.measurement.result"

    def _apply_result_state(self, target_state, extra_values=None, *, allow_same=True):
        for record in self:
            _validate_domain_transition(
                record.state, target_state, _RESULT_STATE_TRANSITIONS, _("Calibration Result"), allow_same
            )
            values = dict(extra_values or {})
            values["state"] = target_state
            record.write(values)
        return True
