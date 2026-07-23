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
    """Convert the removed vehicle approval workflow to the standard active flag."""
    if _column_exists(cr, "nsp_vehicle", "state"):
        cr.execute(
            """
            UPDATE nsp_vehicle
               SET active = CASE WHEN state = 'approved' THEN TRUE ELSE FALSE END
            """
        )
        cr.execute("ALTER TABLE nsp_vehicle DROP COLUMN state")
    if _column_exists(cr, "nsp_vehicle", "reject_reason"):
        cr.execute("ALTER TABLE nsp_vehicle DROP COLUMN reject_reason")

    env = api.Environment(cr, SUPERUSER_ID, {})

    # Remove records from the retired reject wizard. The model itself is no
    # longer loaded and must not remain reachable from old database metadata.
    views = env["ir.ui.view"].search([("model", "=", "nsp.vehicle.reject.wizard")])
    if views:
        views.unlink()

    model_rec = env["ir.model"].search([("model", "=", "nsp.vehicle.reject.wizard")], limit=1)
    if model_rec:
        accesses = env["ir.model.access"].search([("model_id", "=", model_rec.id)])
        if accesses:
            accesses.unlink()
        model_rec.unlink()
