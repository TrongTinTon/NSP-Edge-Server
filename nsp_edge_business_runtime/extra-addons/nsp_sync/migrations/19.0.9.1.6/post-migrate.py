# -*- coding: utf-8 -*-


def _table_exists(cr, table):
    cr.execute("SELECT to_regclass(%s)", (table,))
    row = cr.fetchone()
    return bool(row and row[0])


def migrate(cr, version):
    """Refresh Cloud-owned identity projections promptly after module upgrade.

    Master Data remains Cloud authoritative. Edge only polls Cloud snapshots.
    Existing jobs that still use the old five-minute default are moved to one
    minute and made immediately due; explicit custom schedules are preserved.
    """
    if not (_table_exists(cr, "nsp_sync_job") and _table_exists(cr, "ir_actions_core_api")):
        return

    # Remove the superseded reverse Master Data route if 19.0.9.1.5 was ever installed.
    cr.execute(
        """
        SELECT id
          FROM ir_actions_core_api
         WHERE trim(BOTH '/' FROM COALESCE(route_suffix, '')) = 'edge/self-service-changes'
        """
    )
    action_ids = [row[0] for row in cr.fetchall()]
    if action_ids:
        cr.execute("DELETE FROM nsp_sync_job WHERE sync_action_id = ANY(%s)", (action_ids,))
        cr.execute(
            "DELETE FROM ir_model_data WHERE module = 'nsp_sync' AND name = 'api_self_service_changes'"
        )
        cr.execute("DELETE FROM ir_actions_core_api WHERE id = ANY(%s)", (action_ids,))

    cr.execute(
        """
        UPDATE nsp_sync_job job
           SET schedule_interval_minutes = 1,
               next_run_at = NOW(),
               status = CASE WHEN job.active THEN 'idle' ELSE job.status END
          FROM ir_actions_core_api action
         WHERE job.sync_action_id = action.id
           AND trim(BOTH '/' FROM COALESCE(action.route_suffix, '')) IN (
               'edge/users/snapshot',
               'edge/vehicles/snapshot',
               'edge/vehicle-borrows/snapshot'
           )
           AND job.schedule_interval_minutes = 5
        """
    )
