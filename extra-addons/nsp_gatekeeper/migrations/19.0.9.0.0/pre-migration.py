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


def _rename_xmlid(cr, old_name, new_name):
    cr.execute("""
        UPDATE ir_model_data
           SET name = %s
         WHERE module = 'nsp_gatekeeper'
           AND name = %s
           AND NOT EXISTS (
                SELECT 1
                  FROM ir_model_data existing
                 WHERE existing.module = 'nsp_gatekeeper'
                   AND existing.name = %s
           )
    """, (new_name, old_name, new_name))


def migrate(cr, version):
    # Preserve existing infrastructure records while adopting the new name.
    if _table_exists(cr, 'nsp_controller'):
        cr.execute("UPDATE nsp_controller SET node_type = 'edge_server' WHERE node_type = 'local_server'")

    # Pairing ownership follows the renamed Edge Server node.
    _rename_column(cr, 'nsp_controller_pairing_request', 'local_server_id', 'edge_server_id')

    # Keep the same UI/action records instead of creating duplicate Local/Edge menus.
    for old_name, new_name in (
        ('view_nsp_local_servers_list', 'view_nsp_edge_servers_list'),
        ('view_nsp_local_servers_form', 'view_nsp_edge_servers_form'),
        ('action_nsp_local_servers', 'action_nsp_edge_servers'),
        ('menu_nsp_local_servers', 'menu_nsp_edge_servers'),
        ('action_core_api_nsp_gatekeeper_local_server_status', 'action_core_api_nsp_gatekeeper_edge_server_status'),
    ):
        _rename_xmlid(cr, old_name, new_name)
