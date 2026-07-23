# -*- coding: utf-8 -*-
from odoo import SUPERUSER_ID, api


def _column_exists(cr, table, column):
    cr.execute(
        """
        SELECT 1
          FROM information_schema.columns
         WHERE table_name = %s
           AND column_name = %s
         LIMIT 1
        """,
        (table, column),
    )
    return bool(cr.fetchone())


def migrate(cr, version):
    """Remove the retired ir.actions.server endpoint-generation path."""
    if not _column_exists(cr, "ir_act_server", "endpoint_manager_id"):
        # Odoo table naming can differ across major versions; try the canonical
        # ORM table name before doing nothing.
        table = "ir_actions_server"
        if not _column_exists(cr, table, "endpoint_manager_id"):
            return
    else:
        table = "ir_act_server"

    cr.execute(
        'SELECT id FROM "%s" WHERE endpoint_manager_id IS NOT NULL' % table
    )
    action_ids = [row[0] for row in cr.fetchall()]
    if action_ids:
        env = api.Environment(cr, SUPERUSER_ID, {})
        env["ir.actions.server"].browse(action_ids).exists().unlink()
    cr.execute('ALTER TABLE "%s" DROP COLUMN IF EXISTS endpoint_manager_id' % table)
