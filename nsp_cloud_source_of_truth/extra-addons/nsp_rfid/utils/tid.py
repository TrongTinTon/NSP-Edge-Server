import re
import unicodedata

_TID_SEPARATOR_RE = re.compile(r"[\s:_\-\u200B-\u200D\uFEFF]+")
_TID_HEX_RE = re.compile(r"^[0-9A-F]+$")


def normalize_tid(value):
    """Return the canonical RFID TID used across Cloud APIs and UI."""
    tid = unicodedata.normalize("NFKC", str(value or "")).strip().upper()
    if tid.startswith("0X"):
        tid = tid[2:]
    return _TID_SEPARATOR_RE.sub("", tid)


def is_valid_tid(value):
    return bool(value and _TID_HEX_RE.fullmatch(value))
