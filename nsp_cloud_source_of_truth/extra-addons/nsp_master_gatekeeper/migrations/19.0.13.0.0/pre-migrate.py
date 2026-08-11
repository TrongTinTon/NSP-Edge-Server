# -*- coding: utf-8 -*-
"""Detach Server, Controller and Reader inventory identities.

The old schema stored physical ownership on ``nsp_controller.edge_server_id``
and ``nsp_device.controller_id``. The new schema stores those associations only
on Lane Calibration and Parking Lane runtime/configuration records.

This pre-migration first preserves any recoverable contextual association and
only then removes the obsolete inventory foreign-key columns. Ambiguous data is
never guessed.
"""


def _table_exists(cr, table):
    cr.execute("SELECT to_regclass(%s)", (table,))
    row = cr.fetchone()
    return bool(row and row[0])


def _column_exists(cr, table, column):
    cr.execute(
        """
        SELECT EXISTS (
            SELECT 1
              FROM information_schema.columns
             WHERE table_schema = current_schema()
               AND table_name = %s
               AND column_name = %s
        )
        """,
        (table, column),
    )
    return bool(cr.fetchone()[0])


def _assert_global_reader_codes(cr):
    if not _table_exists(cr, "nsp_device") or not _column_exists(cr, "nsp_device", "device_code"):
        return
    cr.execute(
        """
        SELECT upper(btrim(device_code)) AS normalized_code, COUNT(*)
          FROM nsp_device
         WHERE device_code IS NOT NULL AND btrim(device_code) <> ''
         GROUP BY upper(btrim(device_code))
        HAVING COUNT(*) > 1
         ORDER BY normalized_code
        """
    )
    duplicates = cr.fetchall()
    if duplicates:
        codes = ", ".join(str(code) for code, _count in duplicates[:20])
        raise RuntimeError(
            "Independent Reader migration cannot continue because Device Code must "
            "be globally unique. Resolve duplicated Device Code(s) first: %s" % codes
        )



def _ensure_lane_context_columns(cr):
    """Ensure contextual Lane FKs exist before legacy inventory ownership is removed."""
    table = "nsp_parking_lane"
    if not _table_exists(cr, table):
        return
    for column in ("edge_server_id", "controller_id"):
        if not _column_exists(cr, table, column):
            cr.execute(
                'ALTER TABLE "nsp_parking_lane" ADD COLUMN "%s" integer' % column
            )


def _backfill_calibration_context(cr):
    table = "nsp_measurement_reader_line"
    if not _table_exists(cr, table):
        return
    if (
        _column_exists(cr, table, "reader_id")
        and _column_exists(cr, table, "controller_id")
        and _table_exists(cr, "nsp_device")
        and _column_exists(cr, "nsp_device", "controller_id")
    ):
        cr.execute(
            f"""
            UPDATE {table} line
               SET controller_id = reader.controller_id
              FROM nsp_device reader
             WHERE line.reader_id = reader.id
               AND line.controller_id IS NULL
               AND reader.controller_id IS NOT NULL
            """
        )
    if (
        _column_exists(cr, table, "controller_id")
        and _column_exists(cr, table, "edge_server_id")
        and _table_exists(cr, "nsp_controller")
        and _column_exists(cr, "nsp_controller", "edge_server_id")
    ):
        cr.execute(
            f"""
            UPDATE {table} line
               SET edge_server_id = controller.edge_server_id
              FROM nsp_controller controller
             WHERE line.controller_id = controller.id
               AND line.edge_server_id IS NULL
               AND controller.edge_server_id IS NOT NULL
            """
        )


def _backfill_lane_context(cr):
    lane_table = "nsp_parking_lane"
    if not _table_exists(cr, lane_table):
        return

    has_lane_controller = _column_exists(cr, lane_table, "controller_id")
    has_lane_server = _column_exists(cr, lane_table, "edge_server_id")
    has_reader_owner = (
        _table_exists(cr, "nsp_device")
        and _column_exists(cr, "nsp_device", "controller_id")
    )

    # Only derive a missing Lane Controller when every Reader referenced by that
    # Lane resolves to exactly one legacy Controller. Multiple Controllers are a
    # legitimate new topology, so ambiguous legacy rows remain unguessed.
    if has_lane_controller and has_reader_owner and _table_exists(cr, "nsp_parking_lane_timeline"):
        cr.execute(
            """
            WITH candidates AS (
                SELECT timeline.lane_id,
                       MIN(reader.controller_id) AS controller_id,
                       COUNT(DISTINCT reader.controller_id) AS controller_count
                  FROM nsp_parking_lane_timeline timeline
                  JOIN nsp_device reader ON reader.id = timeline.reader_id
                 WHERE reader.controller_id IS NOT NULL
                 GROUP BY timeline.lane_id
            )
            UPDATE nsp_parking_lane lane
               SET controller_id = candidates.controller_id
              FROM candidates
             WHERE lane.id = candidates.lane_id
               AND lane.controller_id IS NULL
               AND candidates.controller_count = 1
            """
        )

    if (
        has_lane_controller
        and has_lane_server
        and _table_exists(cr, "nsp_controller")
        and _column_exists(cr, "nsp_controller", "edge_server_id")
    ):
        cr.execute(
            """
            UPDATE nsp_parking_lane lane
               SET edge_server_id = controller.edge_server_id
              FROM nsp_controller controller
             WHERE lane.controller_id = controller.id
               AND lane.edge_server_id IS NULL
               AND controller.edge_server_id IS NOT NULL
            """
        )


def _drop_obsolete_inventory_columns(cr):
    if _table_exists(cr, "nsp_controller") and _column_exists(cr, "nsp_controller", "edge_server_id"):
        cr.execute('ALTER TABLE "nsp_controller" DROP COLUMN "edge_server_id" CASCADE')
    if _table_exists(cr, "nsp_device") and _column_exists(cr, "nsp_device", "controller_id"):
        cr.execute('ALTER TABLE "nsp_device" DROP COLUMN "controller_id" CASCADE')
    # Historical related fields were normally non-stored; this is defensive for
    # databases that had a custom stored variant.
    if _table_exists(cr, "nsp_device_antenna") and _column_exists(cr, "nsp_device_antenna", "controller_id"):
        cr.execute('ALTER TABLE "nsp_device_antenna" DROP COLUMN "controller_id" CASCADE')



def _cleanup_removed_lane_creation_ui(cr):
    """Remove obsolete batch-Lane UI metadata after the direct popup replaced it."""
    model_names = (
        "nsp.parking.lane.create.wizard",
        "nsp.parking.lane.create.line",
    )
    if _table_exists(cr, "ir_ui_view"):
        cr.execute(
            "DELETE FROM ir_ui_view WHERE model = ANY(%s)",
            (list(model_names),),
        )
    if _table_exists(cr, "ir_model_access") and _table_exists(cr, "ir_model"):
        cr.execute(
            """
            DELETE FROM ir_model_access access
             USING ir_model model
             WHERE access.model_id = model.id
               AND model.model = ANY(%s)
            """,
            (list(model_names),),
        )


def migrate(cr, version):
    _assert_global_reader_codes(cr)
    _ensure_lane_context_columns(cr)
    _backfill_calibration_context(cr)
    _backfill_lane_context(cr)
    _drop_obsolete_inventory_columns(cr)
    _cleanup_removed_lane_creation_ui(cr)
