# -*- coding: utf-8 -*-
import uuid


def new_management_code(prefix, length=16):
    """Return a system-generated, collision-resistant code for NSP technical identifiers."""
    normalized_prefix = str(prefix or "NSP").strip().upper() or "NSP"
    return "%s-%s" % (normalized_prefix, uuid.uuid4().hex[:length].upper())

def strip_empty_x2many_create_commands(commands, required_field, ignored_fields=None):
    """Drop only empty client-side placeholder rows from x2many commands.

    Editable one2many lists can create a virtual blank row for keyboard scanning.
    That placeholder must not reach ORM ``create()`` when its mandatory target
    field is still unset. Commands containing any meaningful user data remain
    untouched so normal required-field validation is preserved.
    """
    if not commands:
        return commands

    ignored = set(ignored_fields or ())
    cleaned = []
    for command in commands:
        try:
            operation = command[0]
            values = command[2] if len(command) > 2 else None
        except (TypeError, IndexError):
            cleaned.append(command)
            continue

        if operation != 0 or not isinstance(values, dict) or values.get(required_field):
            cleaned.append(command)
            continue

        meaningful = {
            key: value
            for key, value in values.items()
            if key not in ignored and value not in (False, None, "", [], {})
        }
        if meaningful:
            cleaned.append(command)

    return cleaned

