# -*- coding: utf-8 -*-
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})

    # Remove the obsolete Cloud callback endpoint owned by old NSP Sync.
    for xmlid in (
        "nsp_sync.action_core_api_nsp_sync_status",
        "nsp_sync.action_endpoint_manager_nsp_sync",
    ):
        record = env.ref(xmlid, raise_if_not_found=False)
        if record:
            record.sudo().unlink()

    # Callback-only jobs have no meaning in the Local-only design.
    cr.execute("DELETE FROM nsp_sync_job WHERE direction = 'callback_only'")

    # Keep one active Cloud credential per Local Server.
    cr.execute("""
        WITH ranked AS (
            SELECT id, ROW_NUMBER() OVER (PARTITION BY local_server_id ORDER BY id) AS rn
              FROM nsp_sync_auth
             WHERE active = TRUE AND local_server_id IS NOT NULL
        )
        UPDATE nsp_sync_auth AS auth
           SET active = FALSE
          FROM ranked
         WHERE auth.id = ranked.id AND ranked.rn > 1
    """)

    # Normalize old remote callback states into Local Server states.
    cr.execute("UPDATE nsp_sync_record SET status = 'synced' WHERE status IN ('sent', 'applied')")
    cr.execute("UPDATE nsp_sync_record SET operation = 'pull' WHERE operation NOT IN ('pull', 'push') OR operation IS NULL")

    # Drop obsolete callback and Cloud ownership fields.
    cr.execute("ALTER TABLE nsp_sync_job DROP COLUMN IF EXISTS status_action_id CASCADE")
    cr.execute("ALTER TABLE nsp_sync_job DROP COLUMN IF EXISTS status_route_suffix CASCADE")
    cr.execute("ALTER TABLE nsp_sync_job DROP COLUMN IF EXISTS pull_page CASCADE")
    cr.execute("ALTER TABLE nsp_sync_job DROP COLUMN IF EXISTS pull_cursor_to CASCADE")
    cr.execute("ALTER TABLE nsp_sync_record DROP COLUMN IF EXISTS api_application_id CASCADE")
    cr.execute("ALTER TABLE nsp_sync_record DROP COLUMN IF EXISTS sync_endpoint_id CASCADE")

    # Remove obsolete setup-role state. NSP Sync itself now marks a Local Server.
    cr.execute("DELETE FROM ir_config_parameter WHERE key IN ('nsp.deployment_role')")
    cr.execute("DROP TABLE IF EXISTS nsp_system_setup CASCADE")
