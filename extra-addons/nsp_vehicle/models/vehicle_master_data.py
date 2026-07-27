# -*- coding: utf-8 -*-
from odoo import fields, models
from odoo.addons.nsp_core.utils import new_management_code


class VehicleCloudSyncMixin(models.AbstractModel):
    _name = "nsp.vehicle.cloud.sync.mixin"
    _description = "Vehicle-specific master data"


class VehicleType(models.Model):
    _name = "nsp.vehicle.type"
    _description = "Vehicle Type"
    _inherit = ["nsp.vehicle.cloud.sync.mixin"]
    _rec_name = "name"
    _order = "name"

    name = fields.Char(required=True)
    code = fields.Char(copy=False, index=True, required=True, readonly=True, default=lambda self: new_management_code("VTYPE"))
    active = fields.Boolean(default=True)

    _sql_constraints = [
        ("nsp_vehicle_type_name_uniq", "unique(name)", "Vehicle Type already exists."),
        ("nsp_vehicle_type_code_uniq", "unique(code)", "Vehicle Type Code already exists."),
    ]


class VehicleColor(models.Model):
    _name = "nsp.vehicle.color"
    _description = "Vehicle Color"
    _inherit = ["nsp.vehicle.cloud.sync.mixin"]
    _rec_name = "name"
    _order = "name"

    name = fields.Char(required=True)
    code = fields.Char(copy=False, index=True, required=True, readonly=True, default=lambda self: new_management_code("COLOR"))
    active = fields.Boolean(default=True)

    _sql_constraints = [
        ("nsp_vehicle_color_name_uniq", "unique(name)", "Color already exists."),
        ("nsp_vehicle_color_code_uniq", "unique(code)", "Vehicle Color Code already exists."),
    ]
