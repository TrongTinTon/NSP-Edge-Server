# -*- coding: utf-8 -*-


def migrate(cr, version):
    """Reuse the existing outbound Core API action instead of creating a duplicate."""
    cr.execute(
        """
        UPDATE ir_model_data
           SET name = 'api_parking_logs'
         WHERE module = 'nsp_sync'
           AND name = 'api_parking_transactions'
           AND model = 'ir.actions.core_api'
           AND NOT EXISTS (
                SELECT 1
                  FROM ir_model_data current
                 WHERE current.module = 'nsp_sync'
                   AND current.name = 'api_parking_logs'
                   AND current.model = 'ir.actions.core_api'
           )
        """
    )
