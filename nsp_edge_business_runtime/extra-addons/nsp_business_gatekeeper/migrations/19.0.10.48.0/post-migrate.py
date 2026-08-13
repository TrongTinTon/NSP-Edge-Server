# -*- coding: utf-8 -*-


def migrate(cr, version):
    # Older builds could classify Cloud business status=ignored as transport
    # success. Preserve the receipt but mark it terminal/non-persisted.
    cr.execute(
        """
        UPDATE nsp_sync_record
           SET status = 'skipped',
               message = COALESCE(NULLIF(message, ''), 'Ignored by Cloud.')
         WHERE route_suffix = 'edge/lane-calibrations/events'
           AND operation = 'push'
           AND status = 'synced'
           AND response_json IS NOT NULL
           AND response_json ~* '\"status\"[[:space:]]*:[[:space:]]*\"ignored\"'
        """
    )
