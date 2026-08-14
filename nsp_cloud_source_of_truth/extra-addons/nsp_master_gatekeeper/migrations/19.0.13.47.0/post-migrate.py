# -*- coding: utf-8 -*-


def _table_exists(cr, table):
    cr.execute("SELECT to_regclass(%s)", (table,))
    row = cr.fetchone()
    return bool(row and row[0])


def migrate(cr, version):
    """Remove the superseded Edge -> Cloud Master Data mutation endpoint."""
    if not _table_exists(cr, "ir_actions_core_api"):
        return
    cr.execute(
        """
        SELECT id
          FROM ir_actions_core_api
         WHERE trim(BOTH '/' FROM COALESCE(route_suffix, '')) = 'edge/self-service-changes'
        """
    )
    action_ids = [row[0] for row in cr.fetchall()]
    if action_ids:
        cr.execute(
            "DELETE FROM ir_model_data WHERE module = 'nsp_master_gatekeeper' AND name = 'api_master_self_service_changes'"
        )
        cr.execute("DELETE FROM ir_actions_core_api WHERE id = ANY(%s)", (action_ids,))
