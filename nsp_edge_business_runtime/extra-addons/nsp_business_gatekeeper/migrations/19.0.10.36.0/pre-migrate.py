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

    # Detection Log must be able to persist a valid physical receipt before Edge
    # resolves Reader/Lane business context. Legacy rows remain fully resolved.
    for column in ("layout_lane_id", "lane_id", "reader_id"):
        if _column_exists(cr, table, column):
            cr.execute(f'ALTER TABLE "{table}" ALTER COLUMN "{column}" DROP NOT NULL')

    _logger.info("NSP Detection Log raw-receipt nullable context migration prepared")
