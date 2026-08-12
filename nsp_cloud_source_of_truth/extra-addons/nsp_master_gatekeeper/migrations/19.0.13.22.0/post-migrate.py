# -*- coding: utf-8 -*-
"""Migrate owned Parking Lanes into Lane master + contextual Layout-Lane rows."""


def _table(cr, name):
    cr.execute("SELECT to_regclass(%s)", ("public.%s" % name,))
    return bool(cr.fetchone()[0])


def _column(cr, table, column):
    cr.execute("""
        SELECT 1
          FROM information_schema.columns
         WHERE table_schema='public' AND table_name=%s AND column_name=%s
    """, (table, column))
    return bool(cr.fetchone())


def migrate(cr, version):
    required = ["nsp_parking_lane", "nsp_parking_area", "nsp_parking_layout_lane"]
    if not all(_table(cr, name) for name in required):
        return

    # 1. Lane master keeps the same row/id/code, but Branch becomes its stable scope.
    if _column(cr, "nsp_parking_lane", "parking_area_id"):
        cr.execute("""
            UPDATE nsp_parking_lane lane
               SET branch_id = area.branch_id
              FROM nsp_parking_area area
             WHERE lane.branch_id IS NULL
               AND lane.parking_area_id = area.id
        """)

        # 2. Each legacy owned Lane becomes one contextual Layout-Lane configuration.
        cr.execute("""
            INSERT INTO nsp_parking_layout_lane
                (parking_area_id, lane_id, branch_id, sequence,
                 edge_server_id, controller_id, active,
                 setup_state, setup_applied_at,
                 tolerance_type, tolerance_value,
                 create_uid, write_uid, create_date, write_date)
            SELECT lane.parking_area_id,
                   lane.id,
                   area.branch_id,
                   ROW_NUMBER() OVER (
                       PARTITION BY lane.parking_area_id ORDER BY lane.id
                   ) * 10,
                   lane.edge_server_id,
                   lane.controller_id,
                   COALESCE(lane.active, TRUE),
                   COALESCE(lane.setup_state, 'draft'),
                   lane.setup_applied_at,
                   COALESCE(lane.tolerance_type, 'percent'),
                   COALESCE(lane.tolerance_value, 30.0),
                   lane.create_uid,
                   lane.write_uid,
                   lane.create_date,
                   lane.write_date
              FROM nsp_parking_lane lane
              JOIN nsp_parking_area area ON area.id = lane.parking_area_id
             WHERE lane.parking_area_id IS NOT NULL
               AND NOT EXISTS (
                   SELECT 1
                     FROM nsp_parking_layout_lane mapped
                    WHERE mapped.parking_area_id = lane.parking_area_id
                      AND mapped.lane_id = lane.id
               )
        """)

    # 3. Copy contextual Reader snapshots to the new Layout-Lane child table.
    if (
        _table(cr, "nsp_parking_lane_reader_config")
        and _table(cr, "nsp_parking_layout_lane_reader_config")
    ):
        cr.execute("""
            INSERT INTO nsp_parking_layout_lane_reader_config
                (layout_lane_id, reader_id, power_dbm, read_interval_ms,
                 tid_start_address, tid_length, source_type, source_reference,
                 source_revision, applied_at,
                 create_uid, write_uid, create_date, write_date)
            SELECT mapped.id,
                   old.reader_id,
                   old.power_dbm,
                   old.read_interval_ms,
                   old.tid_start_address,
                   old.tid_length,
                   old.source_type,
                   old.source_reference,
                   old.source_revision,
                   old.applied_at,
                   old.create_uid,
                   old.write_uid,
                   old.create_date,
                   old.write_date
              FROM nsp_parking_lane_reader_config old
              JOIN nsp_parking_layout_lane mapped ON mapped.lane_id = old.lane_id
             WHERE NOT EXISTS (
                   SELECT 1
                     FROM nsp_parking_layout_lane_reader_config target
                    WHERE target.layout_lane_id = mapped.id
                      AND target.reader_id = old.reader_id
               )
        """)

    # 4. Copy Antenna Sequence preserving UI order and Max Duration.
    if (
        _table(cr, "nsp_parking_lane_timeline")
        and _table(cr, "nsp_parking_layout_lane_sequence")
    ):
        cr.execute("""
            INSERT INTO nsp_parking_layout_lane_sequence
                (layout_lane_id, sequence, reader_id, port_no,
                 duration_from_previous,
                 create_uid, write_uid, create_date, write_date)
            SELECT mapped.id,
                   old.sequence,
                   old.reader_id,
                   old.port_no,
                   old.duration_from_previous,
                   old.create_uid,
                   old.write_uid,
                   old.create_date,
                   old.write_date
              FROM nsp_parking_lane_timeline old
              JOIN nsp_parking_layout_lane mapped ON mapped.lane_id = old.lane_id
             WHERE NOT EXISTS (
                   SELECT 1
                     FROM nsp_parking_layout_lane_sequence target
                    WHERE target.layout_lane_id = mapped.id
                      AND target.reader_id = old.reader_id
                      AND target.port_no = old.port_no
               )
        """)

    # 5. Bind existing immutable Parking Transactions to their contextual mapping.
    if _table(cr, "nsp_parking_transaction") and _column(cr, "nsp_parking_transaction", "layout_lane_id"):
        cr.execute("""
            UPDATE nsp_parking_transaction tx
               SET layout_lane_id = mapped.id
              FROM nsp_parking_layout_lane mapped
             WHERE tx.layout_lane_id IS NULL
               AND tx.parking_area_id = mapped.parking_area_id
               AND tx.lane_id = mapped.lane_id
        """)

    # 6. The master no longer owns contextual configuration. Remove obsolete columns
    # only after every mapping and child snapshot has been copied successfully.
    legacy_columns = [
        "parking_area_id", "edge_server_id", "controller_id",
        "setup_state", "setup_applied_at", "tolerance_type", "tolerance_value",
    ]
    for column in legacy_columns:
        if _column(cr, "nsp_parking_lane", column):
            cr.execute('ALTER TABLE nsp_parking_lane DROP COLUMN "%s" CASCADE' % column)
