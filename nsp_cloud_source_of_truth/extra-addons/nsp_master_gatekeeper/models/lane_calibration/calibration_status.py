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

        invalid_servers = server_nodes.filtered(lambda node: bool(node.parent_id))
        if invalid_servers:
            raise ValidationError(
                _("Server nodes must be roots of the Device Tree: %s")
                % ", ".join(invalid_servers.mapped("device_name"))
            )

        unassigned_controllers = controller_nodes.filtered(
            lambda node: not node.parent_id or node.parent_id.device_type != "server"
        )
        if unassigned_controllers:
            raise ValidationError(
                _("Assign every Controller to a Server before Release. Missing: %s")
                % ", ".join(unassigned_controllers.mapped("device_name"))
            )

        unassigned_readers = reader_nodes.filtered(
            lambda node: not node.parent_id or node.parent_id.device_type != "controller"
        )
        if unassigned_readers:
            raise ValidationError(
                _("Assign every Reader to a Controller before Release. Missing: %s")
                % ", ".join(unassigned_readers.mapped("device_name"))
            )

        servers_without_controllers = server_nodes.filtered(
            lambda server: not controller_nodes.filtered(lambda node: node.parent_id == server)
        )
        if servers_without_controllers:
            raise ValidationError(
                _("Add at least one Controller under every Server. Missing: %s")
                % ", ".join(servers_without_controllers.mapped("device_name"))
            )

        controllers_without_readers = controller_nodes.filtered(
            lambda controller: not reader_nodes.filtered(lambda node: node.parent_id == controller)
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
            node_by_id = {node.id: node for node in self._reader_nodes()}
            seen = set()
            updates_by_values = {}
            for item in reader_settings:
                if not isinstance(item, dict):
                    raise ValidationError(_("Invalid Reader settings."))
                try:
                    node_id = int(item.get("reader_node_id") or 0)
                    power = int(item.get("power_dbm"))
                    interval = int(item.get("read_interval_ms"))
                except (TypeError, ValueError) as exc:
                    raise ValidationError(_("Invalid Reader settings.")) from exc
                node = node_by_id.get(node_id)
                if not node or node_id in seen:
                    raise ValidationError(_(
                        "Reader settings do not match this Lane Calibration."
                    ))
                seen.add(node_id)
                updates_by_values.setdefault((power, interval), self.env[node._name].browse())
                updates_by_values[(power, interval)] |= node
            for (power, interval), nodes in updates_by_values.items():
                nodes.with_context(measurement_sync=True).write({
                    "power_dbm": power,
                    "read_interval_ms": interval,
                })
        return self._release_new_revision("ready")

    def action_apply_reader_settings(self, reader_node_id, power_dbm, read_interval_ms):
        """Apply one contextual Reader-node configuration and release a new revision."""
        self.ensure_one()
        self._check_public_action_access("write")
        if self.status not in CalibrationStatusPolicy.REVISION_SOURCES["ready"]:
            raise ValidationError(_(
                "Reader settings can be applied only while running, completed, or failed."
            ))
        try:
            node_id = int(reader_node_id or 0)
            power = int(power_dbm)
            interval = int(read_interval_ms)
        except (TypeError, ValueError) as exc:
            raise ValidationError(_("Invalid Reader settings.")) from exc
        node = self._reader_nodes().filtered(lambda item: item.id == node_id)[:1]
        if not node:
            raise ValidationError(_("Reader does not belong to this Lane Calibration."))
        node.with_context(measurement_sync=True).write({
            "power_dbm": power,
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
        for node in self._reader_nodes():
            values = (
                int(node.power_dbm or 0),
                int(node.read_interval_ms or 200),
            )
            readers_by_settings.setdefault(values, self.env[node.reader_id._name].browse())
            readers_by_settings[values] |= node.reader_id
        for (power, interval), readers in readers_by_settings.items():
            # Device-node scope and contextual Reader settings were validated above.
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
