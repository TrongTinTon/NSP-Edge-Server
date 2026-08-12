# -*- coding: utf-8 -*-
"""Move legacy Parking Lane-owned configuration into contextual Layout Lane rows."""


def _table_exists(cr, table):
    cr.execute("SELECT to_regclass(%s)", ("public.%s" % table,))
    return bool(cr.fetchone()[0])


def _column_exists(cr, table, column):
    cr.execute(
        """
        SELECT 1
          FROM information_schema.columns
         WHERE table_schema = 'public'
           AND table_name = %s
           AND column_name = %s
        """,
        (table, column),
    )
    return bool(cr.fetchone())


def migrate(cr, version):
    required_tables = (
        "nsp_parking_lane",
        "nsp_parking_area",
        "nsp_parking_layout_lane",
    )
    if not all(_table_exists(cr, table) for table in required_tables):
        return
    if not _column_exists(cr, "nsp_parking_lane", "parking_area_id"):
        # Fresh database or migration already completed.
        return

    # Contextual rows require an Edge Server. Legacy Lane derived it from its
    # Controller, so fail explicitly rather than silently losing runtime topology.
    if _table_exists(cr, "nsp_controller"):
        cr.execute(
            """
            SELECT lane.id, lane.code
              FROM nsp_parking_lane lane
              LEFT JOIN nsp_controller controller ON controller.id = lane.controller_id
             WHERE lane.parking_area_id IS NOT NULL
               AND (lane.controller_id IS NULL OR controller.edge_server_id IS NULL)
             LIMIT 10
            """
        )
        invalid = cr.fetchall()
        if invalid:
            raise RuntimeError(
                "Cannot migrate Lane Configuration without Controller/Server: %s"
                % ", ".join("%s:%s" % (row[0], row[1] or "") for row in invalid)
            )

    # 1. Create contextual Lane Configuration rows while preserving the same Lane
    # record IDs as stable Lane Masters.
    cr.execute(
        """
        INSERT INTO nsp_parking_layout_lane
            (parking_area_id, lane_id, branch_id, sequence,
             edge_server_id, controller_id, active,
             tolerance_type, tolerance_value, display_name,
             create_date, write_date)
        SELECT lane.parking_area_id,
               lane.id,
               area.branch_id,
               10,
               controller.edge_server_id,
               lane.controller_id,
               COALESCE(lane.active, TRUE),
               COALESCE(lane.tolerance_type, 'percent'),
               COALESCE(lane.tolerance_value, 30.0),
               COALESCE(area.name, 'Parking Layout') || ' / ' || COALESCE(lane.name, lane.code, 'Lane'),
               NOW(), NOW()
          FROM nsp_parking_lane lane
          JOIN nsp_parking_area area ON area.id = lane.parking_area_id
          JOIN nsp_controller controller ON controller.id = lane.controller_id
         WHERE lane.parking_area_id IS NOT NULL
        ON CONFLICT (parking_area_id, lane_id) DO UPDATE SET
            branch_id = EXCLUDED.branch_id,
            edge_server_id = EXCLUDED.edge_server_id,
            controller_id = EXCLUDED.controller_id,
            active = EXCLUDED.active,
            tolerance_type = EXCLUDED.tolerance_type,
            tolerance_value = EXCLUDED.tolerance_value,
            display_name = EXCLUDED.display_name,
            write_date = NOW()
        """
    )

    # 2. Move applied Reader Configuration. Legacy rows had no independent Port
    # collection, so ports are recovered from the legacy Antenna Sequence below.
    if _table_exists(cr, "nsp_parking_lane_reader_config") and _table_exists(cr, "nsp_parking_layout_lane_reader_config"):
        cr.execute(
            """
            INSERT INTO nsp_parking_layout_lane_reader_config
                (layout_lane_id, reader_id, power_dbm, read_interval_ms,
                 tid_start_address, tid_length, source_type,
                 create_date, write_date)
            SELECT layout.id,
                   config.reader_id,
                   config.power_dbm,
                   config.read_interval_ms,
                   config.tid_start_address,
                   config.tid_length,
                   'published_layout',
                   NOW(), NOW()
              FROM nsp_parking_lane_reader_config config
              JOIN nsp_parking_layout_lane layout ON layout.lane_id = config.lane_id
            ON CONFLICT (layout_lane_id, reader_id) DO UPDATE SET
                power_dbm = EXCLUDED.power_dbm,
                read_interval_ms = EXCLUDED.read_interval_ms,
                tid_start_address = EXCLUDED.tid_start_address,
                tid_length = EXCLUDED.tid_length,
                source_type = EXCLUDED.source_type,
                write_date = NOW()
            """
        )

    if _table_exists(cr, "nsp_parking_lane_timeline"):
        # 3. Recover contextual Reader Ports from the old sequence. This is the
        # best possible migration for old Edge data; future snapshots carry the
        # independent Device Configuration port collection explicitly.
        if _table_exists(cr, "nsp_parking_layout_lane_reader_port"):
            cr.execute(
                """
                INSERT INTO nsp_parking_layout_lane_reader_port
                    (reader_config_id, layout_lane_id, reader_id, port_no,
                     create_date, write_date)
                SELECT DISTINCT config_new.id,
                       layout.id,
                       timeline.reader_id,
                       timeline.port_no,
                       NOW(), NOW()
                  FROM nsp_parking_lane_timeline timeline
                  JOIN nsp_parking_layout_lane layout ON layout.lane_id = timeline.lane_id
                  JOIN nsp_parking_layout_lane_reader_config config_new
                    ON config_new.layout_lane_id = layout.id
                   AND config_new.reader_id = timeline.reader_id
                 WHERE timeline.port_no BETWEEN 1 AND 16
                ON CONFLICT (reader_config_id, port_no) DO NOTHING
                """
            )

        # 4. Move Antenna Sequence to the contextual model.
        if _table_exists(cr, "nsp_parking_layout_lane_sequence"):
            cr.execute(
                """
                INSERT INTO nsp_parking_layout_lane_sequence
                    (layout_lane_id, sequence, reader_id, port_no,
                     duration_from_previous, cumulative_time,
                     create_date, write_date)
                SELECT layout.id,
                       timeline.sequence,
                       timeline.reader_id,
                       timeline.port_no,
                       timeline.duration_from_previous,
                       COALESCE(timeline.cumulative_time, 0.0),
                       NOW(), NOW()
                  FROM nsp_parking_lane_timeline timeline
                  JOIN nsp_parking_layout_lane layout ON layout.lane_id = timeline.lane_id
                ON CONFLICT (layout_lane_id, sequence) DO UPDATE SET
                    reader_id = EXCLUDED.reader_id,
                    port_no = EXCLUDED.port_no,
                    duration_from_previous = EXCLUDED.duration_from_previous,
                    cumulative_time = EXCLUDED.cumulative_time,
                    write_date = NOW()
                """
            )

    # 5. Preserve immutable Parking Transaction history while attaching a
    # contextual reference wherever the old Lane ownership makes it unambiguous.
    if _table_exists(cr, "nsp_parking_transaction") and _column_exists(cr, "nsp_parking_transaction", "layout_lane_id"):
        cr.execute(
            """
            UPDATE nsp_parking_transaction tx
               SET layout_lane_id = layout.id
              FROM nsp_parking_layout_lane layout
             WHERE tx.layout_lane_id IS NULL
               AND tx.lane_id = layout.lane_id
               AND (tx.parking_area_id IS NULL OR tx.parking_area_id = layout.parking_area_id)
            """
        )

    # Stored display name of existing Lane Masters changed semantics from
    # "Layout / Lane" to "Branch / Lane".
    if _column_exists(cr, "nsp_parking_lane", "display_name"):
        cr.execute(
            """
            UPDATE nsp_parking_lane lane
               SET display_name = COALESCE(branch.name, '') ||
                   CASE WHEN COALESCE(branch.name, '') <> '' THEN ' / ' ELSE '' END ||
                   COALESCE(lane.name, lane.code, 'Lane')
              FROM nsp_branch branch
             WHERE branch.id = lane.branch_id
            """
        )

    # 6. Remove legacy contextual ownership from the Lane Master table itself.
    # Old child tables may remain as inert upgrade history, but runtime source no
    # longer references them and Lane Master cannot be cascade-owned by a Layout.
    for column in ("parking_area_id", "controller_id", "tolerance_type", "tolerance_value"):
        if _column_exists(cr, "nsp_parking_lane", column):
            cr.execute(
                'ALTER TABLE nsp_parking_lane DROP COLUMN IF EXISTS "%s" CASCADE'
                % column
            )
