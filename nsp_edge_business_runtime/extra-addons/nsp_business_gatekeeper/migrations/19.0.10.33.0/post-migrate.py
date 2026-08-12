# -*- coding: utf-8 -*-
import logging


_logger = logging.getLogger(__name__)


def _table_exists(cr, table):
    cr.execute("SELECT to_regclass(%s)", (f"public.{table}",))
    return bool(cr.fetchone()[0])


def _column_exists(cr, table, column):
    cr.execute(
        """
        SELECT 1
          FROM information_schema.columns
         WHERE table_schema = 'public'
           AND table_name = %s
           AND column_name = %s
         LIMIT 1
        """,
        (table, column),
    )
    return bool(cr.fetchone())


def migrate(cr, version):
    table = "nsp_parking_log"
    if not _table_exists(cr, table):
        return

    # Backfill the two intentionally denormalized query keys before removing
    # obsolete code snapshots. This makes Live Monitor a direct indexed lookup.
    if _column_exists(cr, table, "layout_lane_id"):
        cr.execute(
            """
            UPDATE nsp_parking_log log
               SET parking_area_id = COALESCE(log.parking_area_id, ll.parking_area_id),
                   lane_id = COALESCE(log.lane_id, ll.lane_id)
              FROM nsp_parking_layout_lane ll
             WHERE log.layout_lane_id = ll.id
               AND (log.parking_area_id IS NULL OR log.lane_id IS NULL)
            """
        )

    # These columns duplicated master/context data or technical detection evidence.
    # Long-lived Parking Logs keep only business identity and business outcome.
    obsolete_columns = (
        "controller_id",
        "controller_code",
        "lane_code",
        "parking_area_code",
        "sequence_path",
        "observed_duration_seconds",
        "allowed_duration_seconds",
        "reader_id",
        "serial_number",
        "port_no",
        "primary_detection_id",
        "vehicle_code",
        "license_plate",
        "user_code",
        "observed_user_codes",
        "observed_user_tids",
        "borrow_code",
        "error_message",
        "reason_message",
        # nsp.parking.log is append-only and uses _log_access = False.
        "create_uid",
        "create_date",
        "write_uid",
        "write_date",
    )
    for column in obsolete_columns:
        if _column_exists(cr, table, column):
            cr.execute(f'ALTER TABLE "{table}" DROP COLUMN "{column}" CASCADE')

    # Old custom/index names can survive a PostgreSQL table/column rename. Odoo
    # has already created the new model indexes by post-migrate time, so remove
    # non-constraint legacy indexes to avoid duplicate write amplification.
    cr.execute("DROP INDEX IF EXISTS nsp_parking_tx_vehicle_continuity_idx")
    cr.execute(
        """
        SELECT indexname
          FROM pg_indexes
         WHERE schemaname = 'public'
           AND tablename = 'nsp_parking_log'
           AND indexname LIKE 'nsp_parking_transaction_%'
           AND indexname NOT IN (
               SELECT ci.relname
                 FROM pg_constraint c
                 JOIN pg_class ci ON ci.oid = c.conindid
                WHERE c.conrelid = 'nsp_parking_log'::regclass
                  AND c.conindid <> 0
           )
        """
    )
    for (index_name,) in cr.fetchall():
        cr.execute('DROP INDEX IF EXISTS "%s"' % index_name.replace('"', '""'))
    cr.execute("DROP INDEX IF EXISTS nsp_parking_detection_event_transaction_id_index")

    _logger.info("NSP Parking Log schema cleanup completed")
