# -*- coding: utf-8 -*-
"""Backfill direction-specific durations from the legacy shared Lane Timeline."""


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


def migrate(cr, version):
    sequence_table = "nsp_parking_lane_event_sequence"
    timeline_table = "nsp_parking_lane_timeline"
    if not _table_exists(cr, sequence_table) or not _table_exists(cr, timeline_table):
        return
    if not _column_exists(cr, sequence_table, "duration_from_previous"):
        return

    # Legacy NSP 19.x stored duration only on one shared physical timeline.
    # Existing direction sequences were required to follow adjacent timeline
    # points, so the edge duration is the duration attached to the higher
    # timeline position for either forward or reverse traversal.
    cr.execute(
        f"""
        WITH seq AS (
            SELECT
                s.id,
                s.lane_id,
                s.sequence_type,
                s.sequence,
                s.reader_id,
                s.port_no,
                lag(s.reader_id) OVER (
                    PARTITION BY s.lane_id, s.sequence_type ORDER BY s.sequence, s.id
                ) AS prev_reader_id,
                lag(s.port_no) OVER (
                    PARTITION BY s.lane_id, s.sequence_type ORDER BY s.sequence, s.id
                ) AS prev_port_no
            FROM {sequence_table} s
        ), mapped AS (
            SELECT
                seq.id,
                seq.sequence,
                cur.sequence AS cur_position,
                prev.sequence AS prev_position,
                cur.duration_from_previous AS cur_duration,
                prev.duration_from_previous AS prev_duration
            FROM seq
            LEFT JOIN {timeline_table} cur
              ON cur.lane_id = seq.lane_id
             AND cur.reader_id = seq.reader_id
             AND cur.port_no = seq.port_no
            LEFT JOIN {timeline_table} prev
              ON prev.lane_id = seq.lane_id
             AND prev.reader_id = seq.prev_reader_id
             AND prev.port_no = seq.prev_port_no
        )
        UPDATE {sequence_table} target
           SET duration_from_previous = CASE
               WHEN mapped.sequence = 1 THEN 0.0
               WHEN mapped.cur_position > mapped.prev_position
                    THEN COALESCE(mapped.cur_duration, 0.0)
               WHEN mapped.prev_position > mapped.cur_position
                    THEN COALESCE(mapped.prev_duration, 0.0)
               ELSE COALESCE(target.duration_from_previous, 0.0)
           END
          FROM mapped
         WHERE target.id = mapped.id
        """
    )
