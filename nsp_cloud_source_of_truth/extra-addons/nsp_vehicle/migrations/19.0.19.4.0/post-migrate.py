# -*- coding: utf-8 -*-
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Repair impossible Returned rows created without an explicit End action.

    A valid Returned borrow is produced only by action_return_vehicle(), which always
    sets returned_at.  Rows with state='returned' and no returned_at therefore cannot
    represent a completed lending period and are safe to restore to Active.
    """
    cr.execute(
        """
        UPDATE nsp_vehicle_borrow
           SET state = 'active'
         WHERE state = 'returned'
           AND returned_at IS NULL
        """
    )
    repaired = cr.rowcount
    if repaired:
        _logger.warning(
            "Repaired %s Vehicle Borrow record(s) incorrectly created as Returned without returned_at",
            repaired,
        )
