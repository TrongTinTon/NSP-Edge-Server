# -*- coding: utf-8 -*-


def _table_exists(cr, table):
    cr.execute("SELECT to_regclass(%s)", (table,))
    row = cr.fetchone()
    return bool(row and row[0])


def migrate(cr, version):
    """Converge stored Sync Job metadata to the live Core API action catalogue."""
    if not (_table_exists(cr, "nsp_sync_job") and _table_exists(cr, "ir_actions_core_api")):
        return
    # ``ir.actions.core_api.name`` is a translated field in Odoo 19 and is
    # physically stored as JSONB.  ``nsp_sync_job.sync_action_name`` is a Char
    # (varchar), therefore both SET and comparison must use a scalar text
    # representation rather than comparing varchar directly with jsonb.
    cr.execute(
        """
        WITH action_meta AS (
            SELECT action.id,
                   action.route_suffix,
                   action.endpoint_code,
                   COALESCE(
                       action.name ->> 'en_US',
                       (SELECT value
                          FROM jsonb_each_text(action.name)
                         ORDER BY key
                         LIMIT 1),
                       ''
                   ) AS action_name
              FROM ir_actions_core_api action
        )
        UPDATE nsp_sync_job job
           SET route_suffix = action.route_suffix,
               sync_action_code = action.endpoint_code,
               sync_action_name = action.action_name
          FROM action_meta action
         WHERE job.sync_action_id = action.id
           AND (
                job.route_suffix IS DISTINCT FROM action.route_suffix
             OR job.sync_action_code IS DISTINCT FROM action.endpoint_code
             OR job.sync_action_name IS DISTINCT FROM action.action_name
           )
        """
    )
