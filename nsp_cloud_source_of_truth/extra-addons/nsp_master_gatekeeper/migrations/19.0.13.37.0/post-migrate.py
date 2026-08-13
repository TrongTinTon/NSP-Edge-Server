# -*- coding: utf-8 -*-
"""Normalize published Parking Layout snapshots after timing tolerance removal.

This is a one-time data migration, not runtime compatibility.  A snapshot that
only carries the retired ``timing_tolerance`` Lane key can be represented
losslessly by the current contract because Max Duration already lives on each
Antenna Sequence transition.  The migration removes that obsolete key, advances
the published revision, and invalidates the affected Edge configuration revision
so Edge requests the normalized snapshot again.
"""

import json


def _normalized_payload(raw_payload, stored_revision):
    try:
        payload = json.loads(raw_payload)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None

    lanes = payload.get("lanes")
    if not isinstance(lanes, list):
        return None

    changed = False
    edge_codes = set()
    for lane in lanes:
        if not isinstance(lane, dict):
            continue
        if "timing_tolerance" in lane:
            lane.pop("timing_tolerance", None)
            changed = True
        server_code = str(lane.get("server_code") or "").strip().upper()
        if server_code:
            edge_codes.add(server_code)

    if not changed:
        return None

    try:
        payload_revision = int(payload.get("published_revision") or 0)
    except (TypeError, ValueError):
        payload_revision = 0
    try:
        database_revision = int(stored_revision or 0)
    except (TypeError, ValueError):
        database_revision = 0

    revision = max(payload_revision, database_revision) + 1
    payload["published_revision"] = revision
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        revision,
        edge_codes,
    )


def migrate(cr, version):
    cr.execute(
        """
        SELECT id, published_revision, published_payload_json,
               published_edge_server_codes
          FROM nsp_parking_area
         WHERE published_payload_json IS NOT NULL
           AND published_payload_json <> ''
        """
    )

    affected_edge_codes = set()
    for area_id, stored_revision, raw_payload, stored_edge_codes in cr.fetchall():
        normalized = _normalized_payload(raw_payload, stored_revision)
        if not normalized:
            continue
        normalized_payload, revision, payload_edge_codes = normalized
        cr.execute(
            """
            UPDATE nsp_parking_area
               SET published_revision = %s,
                   published_payload_json = %s
             WHERE id = %s
            """,
            (revision, normalized_payload, area_id),
        )
        affected_edge_codes.update(payload_edge_codes)
        affected_edge_codes.update(
            item.strip().upper()
            for item in str(stored_edge_codes or "").split(",")
            if item.strip()
        )

    if affected_edge_codes:
        cr.execute(
            """
            UPDATE nsp_edge_server
               SET config_revision = COALESCE(config_revision, 0) + 1
             WHERE UPPER(edge_server_code) IN %s
            """,
            (tuple(sorted(affected_edge_codes)),),
        )
