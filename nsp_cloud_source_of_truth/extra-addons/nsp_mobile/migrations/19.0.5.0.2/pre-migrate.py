# -*- coding: utf-8 -*-


_XML_ID_RENAMES = {
    'action_core_api_nsp_mobile_parking_transactions': 'action_core_api_nsp_mobile_parking_logs',
    'endpoint_nsp_mobile_parking_transactions': 'endpoint_nsp_mobile_parking_logs',
    'access_nsp_parking_transaction_self_service': 'access_nsp_parking_log_self_service',
    'rule_nsp_parking_transaction_self_service_read': 'rule_nsp_parking_log_self_service_read',
    'rule_nsp_parking_transaction_operator_all_read': 'rule_nsp_parking_log_operator_all_read',
    'rule_nsp_parking_transaction_it_all_read': 'rule_nsp_parking_log_it_all_read',
}


def migrate(cr, version):
    """Rename legacy Mobile Parking external IDs before module data is loaded.

    The underlying records are preserved so Core API actions/routes and security
    records are updated in place rather than duplicated during the refactor.
    """
    for old_name, new_name in _XML_ID_RENAMES.items():
        cr.execute(
            """
            UPDATE ir_model_data AS old
               SET name = %s
             WHERE old.module = 'nsp_mobile'
               AND old.name = %s
               AND NOT EXISTS (
                    SELECT 1
                      FROM ir_model_data AS new
                     WHERE new.module = old.module
                       AND new.name = %s
               )
            """,
            [new_name, old_name, new_name],
        )
