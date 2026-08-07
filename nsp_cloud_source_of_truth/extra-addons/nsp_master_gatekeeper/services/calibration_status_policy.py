# -*- coding: utf-8 -*-
"""Single state and revision policy for Lane Calibration."""

from odoo import _
from odoo.exceptions import ValidationError

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
