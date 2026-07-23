# -*- coding: utf-8 -*-
import uuid


def new_management_code(prefix, length=16):
    """Return a system-generated, collision-resistant code for NSP technical identifiers."""
    normalized_prefix = str(prefix or "NSP").strip().upper() or "NSP"
    return "%s-%s" % (normalized_prefix, uuid.uuid4().hex[:length].upper())
