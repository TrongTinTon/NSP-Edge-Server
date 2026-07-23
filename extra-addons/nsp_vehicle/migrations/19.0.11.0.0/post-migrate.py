# -*- coding: utf-8 -*-
from odoo import SUPERUSER_ID, api
from odoo.addons.nsp_core.utils import new_management_code


def migrate(cr, version):
    """Ensure every existing Vehicle has a stable synchronization code."""
    env = api.Environment(cr, SUPERUSER_ID, {})
    vehicles = env["nsp.vehicle"].with_context(active_test=False).search([
        ("vehicle_code", "=", False),
    ])
    for vehicle in vehicles:
        vehicle.write({"vehicle_code": new_management_code("VEH")})
