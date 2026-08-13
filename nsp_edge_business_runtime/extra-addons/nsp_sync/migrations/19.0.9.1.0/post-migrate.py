# -*- coding: utf-8 -*-


def _table_exists(cr, table):
    cr.execute("SELECT to_regclass(%s)", (table,))
    row = cr.fetchone()
    return bool(row and row[0])


def _column_exists(cr, table, column):
    cr.execute(
        """
        SELECT 1
          FROM information_schema.columns
         WHERE table_schema = current_schema()
           AND table_name = %s
           AND column_name = %s
        """,
        (table, column),
    )
    return bool(cr.fetchone())


def _replace(cr, table, column, old, new):
    if _table_exists(cr, table) and _column_exists(cr, table, column):
        cr.execute(
            f'UPDATE "{table}" SET "{column}" = %s WHERE "{column}" = %s',
            (new, old),
        )


def migrate(cr, version):
    # Stored/computed metadata and durable delivery rows must follow the renamed
    # Core API action immediately. This also repairs databases previously migrated
    # only at the business-module layer.
    for table in ("nsp_sync_job", "nsp_sync_record"):
        _replace(cr, table, "route_suffix", "edge/parking-transactions", "edge/parking-logs")
        _replace(cr, table, "sync_action_code", "nsp_edge_parking_transactions", "nsp_edge_parking_logs")
        _replace(cr, table, "sync_action_name", "Parking Transactions", "Parking Logs")
