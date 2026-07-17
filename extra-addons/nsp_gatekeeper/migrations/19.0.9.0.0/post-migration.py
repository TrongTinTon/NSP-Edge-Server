# -*- coding: utf-8 -*-
from odoo import SUPERUSER_ID, api


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


def _drop_column(cr, table_name, column_name):
    if _table_exists(cr, table_name) and _column_exists(cr, table_name, column_name):
        cr.execute('ALTER TABLE "%s" DROP COLUMN "%s" CASCADE' % (table_name, column_name))


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})

    # Controller topology synchronization is removed. Runtime status continues
    # through heartbeat/device-status APIs and configuration has dedicated APIs.
    controller_sync_action = env.ref(
        'nsp_gatekeeper.action_core_api_nsp_gatekeeper_controllers_sync',
        raise_if_not_found=False,
    )
    if controller_sync_action:
        if _table_exists(cr, 'nsp_sync_job'):
            cr.execute("DELETE FROM nsp_sync_job WHERE sync_action_id = %s", (controller_sync_action.id,))
        if _table_exists(cr, 'core_api_endpoint'):
            cr.execute("DELETE FROM core_api_endpoint WHERE action_id = %s OR route_suffix = 'controllers/sync'", (controller_sync_action.id,))
        controller_sync_action.sudo().unlink()
    cr.execute("""
        DELETE FROM ir_model_data
         WHERE module = 'nsp_gatekeeper'
           AND name = 'action_core_api_nsp_gatekeeper_controllers_sync'
    """)

    # Remove topology-only columns from Controllers and Edge Servers.
    for column in ('sync_uid', 'source_server_code', 'sync_state', 'last_synced_at', 'sync_message'):
        _drop_column(cr, 'nsp_controller', column)

    # Remove stale generated routes and regenerate the renamed Edge Server route.
    if _table_exists(cr, 'core_api_endpoint'):
        cr.execute("""
            DELETE FROM core_api_endpoint
             WHERE route_suffix IN ('local-server/status', 'controllers/sync')
                OR code IN ('nsp_gatekeeper_local_server_status', 'nsp_gatekeeper_controllers_sync')
        """)
    manager = env.ref('nsp_gatekeeper.action_endpoint_manager_nsp_gatekeeper', raise_if_not_found=False)
    if manager:
        applications = env['nsp.controller'].sudo().search([
            ('node_type', '=', 'edge_server'),
            ('core_api_application_id', '!=', False),
        ]).mapped('core_api_application_id')
        if applications:
            manager.sudo()._generate_core_api_routes_for_applications(applications)
