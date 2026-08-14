# -*- coding: utf-8 -*-


_XML_ID_RENAMES = {
    'access_nsp_vehicle_brand_self_service': 'access_nsp_reference_brand_self_service',
    'access_nsp_vehicle_model_self_service': 'access_nsp_reference_model_self_service',
}


def migrate(cr, version):
    """Rename legacy Vehicle reference ACL XML IDs before CSV data is loaded."""
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
