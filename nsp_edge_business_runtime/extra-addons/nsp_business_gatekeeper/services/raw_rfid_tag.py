# -*- coding: utf-8 -*-
"""Pure helpers for raw RFID TIDs used by Lane Calibration."""

import re

_TID_SEPARATORS = re.compile(r"[\s:\-]+")
_TID_PATTERN = re.compile(r"^[0-9A-F]+$")


def normalize_raw_tid(value):
    text = str(value or "").strip().upper()
    if text.startswith("0X"):
        text = text[2:]
    text = _TID_SEPARATORS.sub("", text)
    if not text:
        return ""
    if not _TID_PATTERN.fullmatch(text):
        raise ValueError("invalid_raw_tid")
    return text


def normalize_reader_serial(value):
    """Canonicalize the physical Reader SDK SerialNumber like the Windows Controller.

    Controller strips a leading 0x and collapses separators only when the resulting
    SDK serial is the canonical 8 hexadecimal characters. Longer/non-standard
    serials keep their normalized uppercase representation.
    """
    text = str(value or "").strip().upper()
    if text.startswith("0X"):
        text = text[2:]
    compact = "".join(ch for ch in text if ch in "0123456789ABCDEF")
    return compact if len(compact) == 8 else text
