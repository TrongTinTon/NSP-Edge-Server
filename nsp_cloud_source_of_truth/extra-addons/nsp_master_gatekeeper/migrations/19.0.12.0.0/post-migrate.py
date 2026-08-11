# -*- coding: utf-8 -*-
"""Migrate legacy Lane In/Lane Out rows to one canonical Antenna Sequence.

If both legacy directions differ, Check-in is used only as a draft seed and the
Lane is marked Draft so an operator must review it before a new publish. This
avoids silently inventing a canonical physical Lane path.
"""

from collections import defaultdict


def _table_exists(cr, table):
    cr.execute("SELECT to_regclass(%s)", (table,))
    return bool(cr.fetchone()[0])


def _column_exists(cr, table, column):
    cr.execute(
        """
        SELECT EXISTS (
            SELECT 1 FROM information_schema.columns
             WHERE table_schema = current_schema()
               AND table_name = %s
               AND column_name = %s
        )
        """,
        (table, column),
    )
    return bool(cr.fetchone()[0])


def _normalized_path(rows):
    return [
        (
            int(reader_id),
            int(port_no),
            round(float(duration or 0.0), 6),
        )
        for _sequence, reader_id, port_no, duration in rows
    ]


def _is_valid_seed(rows):
    if len(rows) < 2:
        return False
    if [int(row[0]) for row in rows] != list(range(1, len(rows) + 1)):
        return False
    keys = [(int(row[1]), int(row[2])) for row in rows]
    if len(keys) != len(set(keys)):
        return False
    if float(rows[0][3] or 0.0) != 0.0:
        return False
    return all(float(row[3] or 0.0) > 0.0 for row in rows[1:])


def migrate(cr, version):
    lane_table = "nsp_parking_lane"
    timeline_table = "nsp_parking_lane_timeline"
    legacy_table = "nsp_parking_lane_event_sequence"
    if not all(_table_exists(cr, table) for table in (lane_table, timeline_table, legacy_table)):
        return
    if not _column_exists(cr, legacy_table, "duration_from_previous"):
        return

    cr.execute(
        f"""
        SELECT lane_id, sequence_type, sequence, reader_id, port_no,
               duration_from_previous
          FROM {legacy_table}
         ORDER BY lane_id, sequence_type, sequence, id
        """
    )
    grouped = defaultdict(lambda: defaultdict(list))
    for lane_id, sequence_type, sequence, reader_id, port_no, duration in cr.fetchall():
        grouped[int(lane_id)][str(sequence_type or "")].append(
            (int(sequence), int(reader_id), int(port_no), float(duration or 0.0))
        )

    has_setup_state = _column_exists(cr, lane_table, "setup_state")
    has_setup_applied_at = _column_exists(cr, lane_table, "setup_applied_at")
    id_sequence = None
    cr.execute("SELECT pg_get_serial_sequence(%s, 'id')", (timeline_table,))
    row = cr.fetchone()
    if row:
        id_sequence = row[0]
    if not id_sequence:
        return

    for lane_id, directions in grouped.items():
        check_in = directions.get("check_in") or []
        check_out = directions.get("check_out") or []
        seed = check_in or check_out
        if not seed:
            continue

        ambiguous = bool(
            check_in
            and check_out
            and _normalized_path(check_in) != _normalized_path(check_out)
        )
        valid_seed = _is_valid_seed(seed)

        cr.execute(f"DELETE FROM {timeline_table} WHERE lane_id = %s", (lane_id,))
        for index, (_old_sequence, reader_id, port_no, duration) in enumerate(seed, start=1):
            normalized_duration = 0.0 if index == 1 else float(duration or 0.0)
            cr.execute(
                f"""
                INSERT INTO {timeline_table}
                    (id, lane_id, sequence, reader_id, port_no, duration_from_previous)
                VALUES (nextval(%s::regclass), %s, %s, %s, %s, %s)
                """,
                (id_sequence, lane_id, index, reader_id, port_no, normalized_duration),
            )

        if has_setup_state:
            target_state = "draft" if ambiguous or not valid_seed else "applied"
            if has_setup_applied_at and target_state == "draft":
                cr.execute(
                    f"UPDATE {lane_table} SET setup_state = %s, setup_applied_at = NULL WHERE id = %s",
                    (target_state, lane_id),
                )
            else:
                cr.execute(
                    f"UPDATE {lane_table} SET setup_state = %s WHERE id = %s",
                    (target_state, lane_id),
                )

    # The old model is intentionally not registered by 19.0.12.0.0.
    cr.execute(f"DELETE FROM {legacy_table}")
