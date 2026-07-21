# -*- coding: utf-8 -*-
import uuid


def new_management_code(prefix, length=16):
    """Return an editable, collision-resistant default code for NSP master data."""
    normalized_prefix = str(prefix or "NSP").strip().upper() or "NSP"
    return "%s-%s" % (normalized_prefix, uuid.uuid4().hex[:length].upper())
