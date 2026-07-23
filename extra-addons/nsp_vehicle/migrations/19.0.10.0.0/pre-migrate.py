# -*- coding: utf-8 -*-


_RENAMES = {
    "seq_nsp_vehicle_borrow_request": "seq_nsp_vehicle_borrow",
    "view_nsp_vehicle_borrow_request_tree": "view_nsp_vehicle_borrow_list",
    "view_nsp_vehicle_borrow_request_form": "view_nsp_vehicle_borrow_form",
    "view_nsp_vehicle_borrow_request_search": "view_nsp_vehicle_borrow_search",
    "action_nsp_vehicle_borrow_request": "action_nsp_vehicle_borrow",
    "access_nsp_vehicle_borrow_request_hr": "access_nsp_vehicle_borrow_hr",
    "access_nsp_vehicle_borrow_request_it": "access_nsp_vehicle_borrow_it",
    "access_nsp_vehicle_borrow_request_operator": "access_nsp_vehicle_borrow_operator",
}


def migrate(cr, version):
    """Rename legacy external IDs before loading the cleaned Borrow data files."""
    for old_name, new_name in _RENAMES.items():
        cr.execute(
            """
            UPDATE ir_model_data
               SET name = %s
             WHERE module = 'nsp_vehicle'
               AND name = %s
               AND NOT EXISTS (
                    SELECT 1 FROM ir_model_data existing
                     WHERE existing.module = 'nsp_vehicle'
                       AND existing.name = %s
               )
            """,
            (new_name, old_name, new_name),
        )
