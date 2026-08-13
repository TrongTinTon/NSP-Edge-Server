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


def migrate(cr, version):
    """Replay the Parking Log stream once after the route-contract migration.

    ``last_push_record_id`` belongs to the semantic outbound stream.  Older
    databases kept the cursor when ``edge/parking-transactions`` was renamed to
    ``edge/parking-logs``.  A retained cursor can therefore make the new Parking
    Log serializer search only ``id > <legacy cursor>`` and report an empty batch
    forever, even though local Parking Logs exist.

    Resetting this cursor is safe because Parking Logs use ``log_uid`` as an
    idempotency key and the Cloud endpoint classifies already accepted events as
    duplicates.  The reset is migration-scoped and therefore happens once.
    """
    if not (
        _table_exists(cr, "nsp_sync_job")
        and _table_exists(cr, "ir_actions_core_api")
        and _column_exists(cr, "nsp_sync_job", "last_push_record_id")
    ):
        return

    cr.execute(
        """
        UPDATE nsp_sync_job job
           SET last_push_record_id = 0,
               last_push_at = NULL,
               status = CASE WHEN job.active THEN 'idle' ELSE job.status END,
               last_message = CASE
                   WHEN job.active
                   THEN 'Parking Logs push cursor reset for idempotent replay.'
                   ELSE job.last_message
               END
          FROM ir_actions_core_api action
         WHERE job.sync_action_id = action.id
           AND trim(BOTH '/' FROM COALESCE(action.route_suffix, '')) = 'edge/parking-logs'
        """
    )
