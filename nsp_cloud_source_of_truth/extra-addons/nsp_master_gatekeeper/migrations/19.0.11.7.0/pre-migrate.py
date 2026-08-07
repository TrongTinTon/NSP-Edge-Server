# -*- coding: utf-8 -*-
"""Prepare the existing Vehicle-based calibration table for raw-TID workflow.

This migration intentionally uses SQL because it must adjust the existing schema
before the Odoo registry initializes the refactored models. No table or model is
renamed/recreated.
"""


def _table_exists(cr, table):
    cr.execute("SELECT to_regclass(%s)", (table,))
    return bool(cr.fetchone()[0])


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


def _drop_not_null(cr, table, column):
    if _column_exists(cr, table, column):
        cr.execute('ALTER TABLE "%s" ALTER COLUMN "%s" DROP NOT NULL' % (table, column))


def migrate(cr, version):
    calibration_table = "nsp_measurement_target_line"
    if _table_exists(cr, calibration_table):
        if not _column_exists(cr, calibration_table, "tid"):
            cr.execute('ALTER TABLE "%s" ADD COLUMN "tid" varchar' % calibration_table)
        if _column_exists(cr, calibration_table, "vehicle_tid"):
            cr.execute(
                'UPDATE "%s" SET "tid" = "vehicle_tid" '
                'WHERE ("tid" IS NULL OR "tid" = \'\') AND "vehicle_tid" IS NOT NULL'
                % calibration_table
            )
        cr.execute(
            'SELECT COUNT(*) FROM "%s" WHERE "tid" IS NULL OR "tid" = '''
            % calibration_table
        )
        if cr.fetchone()[0]:
            raise RuntimeError(
                "Lane Calibration migration found legacy rows without a recoverable raw TID."
            )
        _drop_not_null(cr, calibration_table, "tag_id")
        _drop_not_null(cr, calibration_table, "vehicle_id")
