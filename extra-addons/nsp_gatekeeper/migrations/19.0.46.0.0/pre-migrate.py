# -*- coding: utf-8 -*-


def migrate(cr, version):
    """Rename the legacy Employees Sync external ID to the User terminology."""
    cr.execute(
        """
        UPDATE ir_model_data
           SET name = 'action_core_api_nsp_gatekeeper_users_sync'
         WHERE module = 'nsp_gatekeeper'
           AND name = 'action_core_api_nsp_gatekeeper_employees_sync'
           AND NOT EXISTS (
                SELECT 1 FROM ir_model_data existing
                 WHERE existing.module = 'nsp_gatekeeper'
                   AND existing.name = 'action_core_api_nsp_gatekeeper_users_sync'
           )
        """
    )
