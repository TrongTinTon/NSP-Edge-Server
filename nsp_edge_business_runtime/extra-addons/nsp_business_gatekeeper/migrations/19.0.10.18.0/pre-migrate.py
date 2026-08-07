# -*- coding: utf-8 -*-
"""Upgrade the existing Edge calibration target table to raw-TID semantics."""


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
    table = "nsp_measurement_target_line"
    if not _table_exists(cr, table):
        return
    if not _column_exists(cr, table, "tid"):
        cr.execute('ALTER TABLE "%s" ADD COLUMN "tid" varchar' % table)
    if _column_exists(cr, table, "vehicle_tid"):
        cr.execute(
            'UPDATE "%s" SET "tid" = "vehicle_tid" '
            'WHERE ("tid" IS NULL OR "tid" = \'\') AND "vehicle_tid" IS NOT NULL'
            % table
        )
    cr.execute('SELECT COUNT(*) FROM "%s" WHERE "tid" IS NULL OR "tid" = \'\'' % table)
    if cr.fetchone()[0]:
        raise RuntimeError(
            "Edge Lane Calibration migration found legacy rows without a recoverable raw TID."
        )
    if _column_exists(cr, table, "vehicle_tid"):
        cr.execute('ALTER TABLE "%s" ALTER COLUMN "vehicle_tid" DROP NOT NULL' % table)
    if _column_exists(cr, table, "vehicle_id"):
        cr.execute('ALTER TABLE "%s" ALTER COLUMN "vehicle_id" DROP NOT NULL' % table)
