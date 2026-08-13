# -*- coding: utf-8 -*-
"""Public actions and the single state machine for Lane Calibration."""

from odoo import _, fields, models
from odoo.exceptions import ValidationError



class CalibrationStatusPolicy:
    """Authoritative state, runtime-status, and revision policy."""

    TRANSITIONS = {
        "draft": frozenset({"ready", "cancelled"}),
        "ready": frozenset({"running", "completed", "failed", "cancelled"}),
        "running": frozenset({"completed", "failed", "cancelled"}),
        "completed": frozenset(),
        "failed": frozenset(),
        "cancelled": frozenset(),
    }

    # Revise is a Cloud-authoring action, not a runtime state transition.
    # It creates the next Draft revision while preserving historical events/results
    # under their original revision numbers.
    REVISION_SOURCES = frozenset({"ready", "completed"})

    CLOUD_STATUSES = frozenset({"draft", "ready"})
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
        if target_status != current_status:
            if target_status in cls.STALE_RUNTIME_TARGETS.get(current_status, frozenset()):
                result["outcome"] = "ignored_stale_status"
                return result
            try:
                cls.validate_transition(current_status, target_status, allow_same=False)
            except ValidationError as exc:
                raise ValueError("invalid_status_transition") from exc
        return result



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
        """Release gate for a complete Server -> Controller -> Reader Tree.

        Draft persistence deliberately does not call this validator.  Multiple
        Servers, Controllers and Readers are supported; every selected node must
        participate in one valid topology before Release.
        """
        self.ensure_one()
        if len(self.target_line_ids) != 1:
            raise ValidationError(_("Release requires exactly one raw Calibration Tag."))

        server_nodes = self._server_nodes()
        controller_nodes = self._controller_nodes()
        reader_nodes = self._reader_nodes()
        missing = []
        if not server_nodes:
            missing.append(_("Server"))
        if not controller_nodes:
            missing.append(_("Controller"))
        if not reader_nodes:
            missing.append(_("Reader"))
        if missing:
            raise ValidationError(
                _("Missing Lane Calibration configuration: %s") % ", ".join(missing)
            )

        # Parent validity is enforced immediately by Device Node create/write.
        # Release only checks branch completeness, using parent-id sets instead of
        # nested recordset filtering.
        server_parent_ids = set(controller_nodes.mapped("parent_id").ids)
        controller_parent_ids = set(reader_nodes.mapped("parent_id").ids)

        servers_without_controllers = server_nodes.filtered(
            lambda server: server.id not in server_parent_ids
        )
        if servers_without_controllers:
            raise ValidationError(
                _("Add at least one Controller under every Server. Missing: %s")
                % ", ".join(servers_without_controllers.mapped("device_name"))
            )

        controllers_without_readers = controller_nodes.filtered(
            lambda controller: controller.id not in controller_parent_ids
        )
        if controllers_without_readers:
            raise ValidationError(
                _("Add at least one Reader under every Controller. Missing: %s")
                % ", ".join(controllers_without_readers.mapped("device_name"))
            )

        missing_ports = reader_nodes.filtered(lambda node: not node.reader_port_ids)
        if missing_ports:
            raise ValidationError(
                _("Select at least one Reader Port for each RFID Reader. Missing: %s")
                % ", ".join(missing_ports.mapped("device_name"))
            )

        # Re-run identity/data-integrity checks after topology completeness passes.
        self._validate_measurement_scope()
        return True

    def _set_ready(self):
        sessions = self._check_public_action_access("write")
        for session in sessions:
            if session.status == "ready":
                continue
            session._require_ready_configuration()
            session._apply_status_transition("ready", allow_same=False)
        return True

    def action_revise(self):
        """Open the next editable Draft revision.

        Revise is intentionally separate from Release: edits to the new Draft are
        Cloud-local and are not part of any runtime snapshot until Release is
        pressed again. Historical detections/results remain keyed by their old
        revision and are never rewritten.
        """
        sessions = self._check_public_action_access("write")
        if len(sessions) != 1:
            raise ValidationError(_("Revise one Lane Calibration at a time."))
        session = sessions
        if session.status not in CalibrationStatusPolicy.REVISION_SOURCES:
            raise ValidationError(
                _("Revise is available only for Released or Completed Lane Calibration.")
            )
        if session.pass_ids.filtered(lambda item: item.state == "running"):
            raise ValidationError(_("Stop the running Calibration Run before Revise."))
        session.with_context(measurement_sync=True).write({
            "revision": int(session.revision or 1) + 1,
            "status": "draft",
            "started_at": False,
            "ended_at": False,
        })
        return {"type": "ir.actions.client", "tag": "reload"}

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
