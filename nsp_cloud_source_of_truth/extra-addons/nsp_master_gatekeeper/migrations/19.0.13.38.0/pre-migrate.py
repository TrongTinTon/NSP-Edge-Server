# -*- coding: utf-8 -*-


def _table_exists(cr, table):
    cr.execute("SELECT to_regclass(%s)", (table,))
    return bool(cr.fetchone()[0])


def _column_exists(cr, table, column):
    cr.execute("""
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = current_schema() AND table_name = %s AND column_name = %s
    """, (table, column))
    return bool(cr.fetchone())


def migrate(cr, version):
    # Preserve immutable history while moving the model to Parking Log semantics.
    if _table_exists(cr, "nsp_parking_transaction") and not _table_exists(cr, "nsp_parking_log"):
        cr.execute("ALTER TABLE nsp_parking_transaction RENAME TO nsp_parking_log")

    # Keep physical schema names aligned with the new business model.
    cr.execute("SELECT to_regclass('nsp_parking_transaction_id_seq')")
    if cr.fetchone()[0]:
        cr.execute("SELECT to_regclass('nsp_parking_log_id_seq')")
        if not cr.fetchone()[0]:
            cr.execute("ALTER SEQUENCE nsp_parking_transaction_id_seq RENAME TO nsp_parking_log_id_seq")
    cr.execute("""
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'nsp_parking_log'::regclass AND conname = 'nsp_parking_transaction_pkey'
    """)
    if cr.fetchone():
        cr.execute("ALTER TABLE nsp_parking_log RENAME CONSTRAINT nsp_parking_transaction_pkey TO nsp_parking_log_pkey")

    table = "nsp_parking_log"
    if not _table_exists(cr, table):
        return
    for old, new in (
        ("transaction_uid", "log_uid"),
        ("status", "decision"),
        ("error_code", "reason_code"),
    ):
        if _column_exists(cr, table, old) and not _column_exists(cr, table, new):
            cr.execute('ALTER TABLE "%s" RENAME COLUMN "%s" TO "%s"' % (table, old, new))

    # Constraint names are not renamed by PostgreSQL when the table/column is renamed.
    # Remove legacy constraints so the new lean model owns only its current contract.
    for constraint in (
        "transaction_uid_unique",
        "transaction_port_range",
        "transaction_duration_consistency",
    ):
        cr.execute('ALTER TABLE nsp_parking_log DROP CONSTRAINT IF EXISTS "%s"' % constraint)

    # Reuse existing XML records during module update so old actions/views/menus do not linger.
    mappings = {
        "view_nsp_parking_transaction_search": "view_nsp_parking_log_search",
        "view_nsp_parking_transaction_list": "view_nsp_parking_log_list",
        "view_nsp_parking_transaction_form": "view_nsp_parking_log_form",
        "action_nsp_parking_transaction": "action_nsp_parking_log",
        "menu_nsp_transactions": "menu_nsp_parking_logs",
        "access_nsp_parking_transaction_it": "access_nsp_parking_log_it",
        "access_nsp_parking_transaction_operator": "access_nsp_parking_log_operator",
        "api_master_parking_transactions": "api_master_parking_logs",
    }
    for old, new in mappings.items():
        cr.execute("""
            UPDATE ir_model_data
               SET name = %s
             WHERE module = 'nsp_master_gatekeeper' AND name = %s
               AND NOT EXISTS (
                   SELECT 1 FROM ir_model_data x
                    WHERE x.module = 'nsp_master_gatekeeper' AND x.name = %s
               )
        """, (new, old, new))
