# -*- coding: utf-8 -*-


def _table_exists(cr, table_name):
    cr.execute("SELECT to_regclass(%s)", ("public.%s" % table_name,))
    return bool(cr.fetchone()[0])


def _column_exists(cr, table_name, column_name):
    cr.execute("""
        SELECT 1
          FROM information_schema.columns
         WHERE table_schema = 'public'
           AND table_name = %s
           AND column_name = %s
    """, (table_name, column_name))
    return bool(cr.fetchone())


def _rename_column(cr, table_name, old_name, new_name):
    if _table_exists(cr, table_name) and _column_exists(cr, table_name, old_name) and not _column_exists(cr, table_name, new_name):
        cr.execute('ALTER TABLE "%s" RENAME COLUMN "%s" TO "%s"' % (table_name, old_name, new_name))


def migrate(cr, version):
    _rename_column(cr, 'nsp_sync_auth', 'local_server_id', 'edge_server_id')
    _rename_column(cr, 'nsp_sync_job', 'local_server_id', 'edge_server_id')
