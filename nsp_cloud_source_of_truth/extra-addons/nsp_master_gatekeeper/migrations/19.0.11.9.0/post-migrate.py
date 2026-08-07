# -*- coding: utf-8 -*-

"""Remove the obsolete Lane Test UI contract after the feature is retired.

Historical Lane Test tables are intentionally left untouched in this migration;
only active Odoo metadata and obsolete Calibration Result state are cleaned.
"""


def _unlink_xmlid(cr, module, name, table):
    cr.execute(
        "SELECT res_id FROM ir_model_data WHERE module = %s AND name = %s",
        (module, name),
    )
    row = cr.fetchone()
    if not row:
        return
    cr.execute(f'DELETE FROM "{table}" WHERE id = %s', (row[0],))
    cr.execute(
        "DELETE FROM ir_model_data WHERE module = %s AND name = %s",
        (module, name),
    )


def migrate(cr, version):
    module = "nsp_master_gatekeeper"

    for name in (
        "menu_nsp_lane_test",
    ):
        _unlink_xmlid(cr, module, name, "ir_ui_menu")

    for name in (
        "view_nsp_measurement_validation_run_search",
        "view_nsp_measurement_validation_run_list",
        "view_nsp_measurement_validation_run_form",
    ):
        _unlink_xmlid(cr, module, name, "ir_ui_view")

    for name in (
        "action_nsp_measurement_validation_run",
    ):
        _unlink_xmlid(cr, module, name, "ir_act_window")

    for name in (
        "access_nsp_measurement_validation_run_it",
        "access_nsp_measurement_validation_run_operator",
        "access_nsp_lane_test_tag_it",
        "access_nsp_lane_test_tag_operator",
        "access_nsp_measurement_validation_port_stat_it",
        "access_nsp_measurement_validation_port_stat_operator",
        "access_nsp_measurement_validation_transition_stat_it",
        "access_nsp_measurement_validation_transition_stat_operator",
    ):
        _unlink_xmlid(cr, module, name, "ir_model_access")

    # A result that was waiting for Lane Test becomes directly reviewable/acceptable.
    cr.execute(
        "UPDATE nsp_measurement_result SET state = 'draft' WHERE state = 'validation'"
    )
