# -*- coding: utf-8 -*-
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    """Remove standalone child-model views/actions replaced by inline Vehicle tabs."""
    env = api.Environment(cr, SUPERUSER_ID, {})
    for xmlid in (
        "nsp_vehicle.action_nsp_vehicle_card",
        "nsp_vehicle.view_nsp_vehicle_card_search",
        "nsp_vehicle.view_nsp_vehicle_card_list",
        "nsp_vehicle.action_nsp_vehicle_borrow",
        "nsp_vehicle.view_nsp_vehicle_borrow_search",
        "nsp_vehicle.view_nsp_vehicle_borrow_list",
    ):
        record = env.ref(xmlid, raise_if_not_found=False)
        if record:
            record.unlink()
