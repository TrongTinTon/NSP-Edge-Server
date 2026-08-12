# -*- coding: utf-8 -*-
import logging

_logger = logging.getLogger(__name__)


def _table_exists(cr, table):
    cr.execute("SELECT to_regclass(%s)", (f"public.{table}",))
    row = cr.fetchone()
    return bool(row and row[0])


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
    table = "nsp_parking_detection_event"
    if not _table_exists(cr, table):
        return

    # Backfill physical source identity for historical Detection Logs. New rows
    # receive these values directly from the authenticated Controller payload.
    if _column_exists(cr, table, "controller_id") and _column_exists(cr, table, "layout_lane_id"):
        cr.execute(
            """
            UPDATE nsp_parking_detection_event event
               SET controller_id = ll.controller_id
              FROM nsp_parking_layout_lane ll
             WHERE event.layout_lane_id = ll.id
               AND event.controller_id IS NULL
            """
        )
    if _column_exists(cr, table, "serial_number") and _column_exists(cr, table, "reader_id"):
        cr.execute(
            """
            UPDATE nsp_parking_detection_event event
               SET serial_number = device.serial_number
              FROM nsp_device device
             WHERE event.reader_id = device.id
               AND (event.serial_number IS NULL OR event.serial_number = '')
            """
        )

    cr.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS nsp_parking_detection_unresolved_uid_unique
            ON nsp_parking_detection_event (event_uid)
         WHERE layout_lane_id IS NULL
        """
    )
    _logger.info("NSP Detection Log raw-receipt visibility migration completed")
