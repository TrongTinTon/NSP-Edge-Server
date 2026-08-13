# -*- coding: utf-8 -*-


def _column_exists(cr, table, column):
    cr.execute("""
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = current_schema() AND table_name = %s AND column_name = %s
    """, (table, column))
    return bool(cr.fetchone())


def migrate(cr, version):
    table = "nsp_sync_job"
    cr.execute("SELECT to_regclass(%s)", (table,))
    job_exists = bool(cr.fetchone()[0])
    if job_exists and _column_exists(cr, table, "route_suffix"):
        cr.execute("""
            UPDATE nsp_sync_job
               SET route_suffix = 'edge/parking-logs'
             WHERE route_suffix = 'edge/parking-transactions'
        """)
    if job_exists and _column_exists(cr, table, "sync_action_code"):
        cr.execute("""
            UPDATE nsp_sync_job
               SET sync_action_code = 'parking_log'
             WHERE sync_action_code = 'parking_transaction'
        """)
    cr.execute("SELECT to_regclass('nsp_sync_record')")
    if cr.fetchone()[0]:
        if _column_exists(cr, "nsp_sync_record", "route_suffix"):
            cr.execute("""
                UPDATE nsp_sync_record
                   SET route_suffix = 'edge/parking-logs'
                 WHERE route_suffix = 'edge/parking-transactions'
            """)
        if _column_exists(cr, "nsp_sync_record", "sync_action_code"):
            cr.execute("""
                UPDATE nsp_sync_record
                   SET sync_action_code = 'parking_log'
                 WHERE sync_action_code = 'parking_transaction'
            """)

