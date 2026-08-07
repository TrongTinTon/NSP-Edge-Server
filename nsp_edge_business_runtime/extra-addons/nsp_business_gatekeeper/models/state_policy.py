# -*- coding: utf-8 -*-
"""Small, testable policies shared by Gatekeeper model services."""

from odoo import _
from odoo.exceptions import ValidationError


def validate_state_transition(current, target, transitions, *, label, allow_same=True):
    """Validate one explicit state transition and return the normalized target."""
    current_state = str(current or "").strip()
    target_state = str(target or "").strip()
    if not target_state:
        raise ValidationError(_("Target state is required."))
    if allow_same and current_state == target_state:
        return target_state
    if target_state not in transitions.get(current_state, frozenset()):
        raise ValidationError(_(
            "%(label)s cannot move from %(current)s to %(target)s."
        ) % {
            "label": label,
            "current": current_state or "-",
            "target": target_state,
        })
    return target_state


def compare_revision(incoming, current):
    """Return ``stale``, ``current`` or ``future`` for optimistic concurrency."""
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

def classify_idempotent_replay(idempotency_key, payload_matches):
    """Classify a replay for one already-used idempotency key."""
    key = str(idempotency_key or "").strip()
    if not key:
        raise ValidationError(_("Idempotency key is required."))
    return "duplicate" if bool(payload_matches) else "conflict"

