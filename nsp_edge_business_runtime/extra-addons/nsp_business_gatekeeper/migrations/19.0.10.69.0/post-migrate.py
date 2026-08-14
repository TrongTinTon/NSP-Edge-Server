# -*- coding: utf-8 -*-


def _table_exists(cr, table):
    cr.execute("SELECT to_regclass(%s)", (table,))
    row = cr.fetchone()
    return bool(row and row[0])


def migrate(cr, version):
    """Clean metadata left by the superseded Edge self-service outbox implementation."""
    if not _table_exists(cr, "ir_model_data"):
        return
    cr.execute(
        """
        DELETE FROM ir_model_data
         WHERE module = 'nsp_business_gatekeeper'
           AND name IN (
               'access_nsp_self_service_sync_event_it',
               'access_nsp_self_service_sync_event_system'
           )
        """
    )
