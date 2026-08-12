# -*- coding: utf-8 -*-
"""Prepare legacy Edge Parking tables for Lane Master + Layout Lane migration."""


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
    if not _table_exists(cr, "nsp_parking_lane"):
        return

    # Lane Master is Branch-scoped. Populate the new master scope while the old
    # Parking Layout ownership column is still available.
    cr.execute("ALTER TABLE nsp_parking_lane ADD COLUMN IF NOT EXISTS branch_id INTEGER")
    if _column_exists(cr, "nsp_parking_lane", "parking_area_id") and _table_exists(cr, "nsp_parking_area"):
        cr.execute(
            """
            UPDATE nsp_parking_lane lane
               SET branch_id = area.branch_id
              FROM nsp_parking_area area
             WHERE lane.branch_id IS NULL
               AND lane.parking_area_id = area.id
            """
        )

    cr.execute("SELECT id, code FROM nsp_parking_lane WHERE branch_id IS NULL LIMIT 10")
    unresolved = cr.fetchall()
    if unresolved:
        raise RuntimeError(
            "Cannot migrate Lane Master without Branch: %s"
            % ", ".join("%s:%s" % (row[0], row[1] or "") for row in unresolved)
        )

    # The new ORM no longer owns these contextual fields on Lane Master. Relax
    # legacy NOT NULL constraints so registry initialization can use the table as
    # a pure master before post-migrate copies/drops the old columns.
    for column in ("parking_area_id", "controller_id", "tolerance_type", "tolerance_value"):
        if _column_exists(cr, "nsp_parking_lane", column):
            cr.execute(
                'ALTER TABLE nsp_parking_lane ALTER COLUMN "%s" DROP NOT NULL' % column
            )

    # Most importantly, remove the legacy Parking Layout -> Lane ON DELETE CASCADE
    # ownership before the new Lane Master becomes authoritative.
    if _column_exists(cr, "nsp_parking_lane", "parking_area_id"):
        cr.execute(
            """
            SELECT con.conname
              FROM pg_constraint con
              JOIN pg_attribute att
                ON att.attrelid = con.conrelid
               AND att.attnum = ANY(con.conkey)
             WHERE con.contype = 'f'
               AND con.conrelid = 'nsp_parking_lane'::regclass
               AND att.attname = 'parking_area_id'
            """
        )
        for (constraint_name,) in cr.fetchall():
            cr.execute(
                'ALTER TABLE nsp_parking_lane DROP CONSTRAINT IF EXISTS "%s"'
                % constraint_name.replace('"', '""')
            )

    # Raw detection events are a short-lived Edge cache. They cannot be safely
    # mapped across an in-flight runtime topology migration and are regenerated
    # from Controller reads after upgrade. Final Parking Transactions are retained.
    if _table_exists(cr, "nsp_parking_detection_event"):
        cr.execute("DELETE FROM nsp_parking_detection_event")
        # Replace the old per-Lane unique key with the contextual key declared by
        # the new model. Explicit removal avoids retaining stale schema semantics
        # on upgraded databases.
        cr.execute(
            "ALTER TABLE nsp_parking_detection_event "
            "DROP CONSTRAINT IF EXISTS nsp_parking_detection_event_event_uid_lane_unique"
        )
        cr.execute(
            "ALTER TABLE nsp_parking_detection_event "
            "DROP CONSTRAINT IF EXISTS event_uid_lane_unique"
        )
