# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError
from odoo.addons.nsp_core.utils import new_management_code


class VehicleCloudSyncMixin(models.AbstractModel):
    _name = "nsp.vehicle.cloud.sync.mixin"
    _description = "Vehicle-specific master data"

    _technical_code_prefix = "VEHREF"
    _technical_code_label = "Vehicle Reference"

    @api.model
    def _new_technical_code(self):
        return new_management_code(self._technical_code_prefix)

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for source in vals_list:
            vals = dict(source)
            vals["name"] = str(vals.get("name") or "").strip()
            vals["code"] = str(vals.get("code") or self._new_technical_code()).strip().upper()
            prepared.append(vals)
        return super().create(prepared)

    def write(self, vals):
        values = dict(vals)
        if "name" in values:
            values["name"] = str(values.get("name") or "").strip()
        if "code" in values:
            values["code"] = str(values.get("code") or "").strip().upper()

        return super().write(values)

    @api.constrains("name", "code")
    def _check_master_values(self):
        for record in self:
            if not (record.name or "").strip():
                raise ValidationError(_("%s Name is required.") % record._technical_code_label)
            if not (record.code or "").strip():
                raise ValidationError(_("%s Technical Code is required.") % record._technical_code_label)


class VehicleType(models.Model):
    _name = "nsp.vehicle.type"
    _description = "Vehicle Type"
    _inherit = ["nsp.vehicle.cloud.sync.mixin"]
    _rec_name = "name"
    _order = "name"

    _technical_code_prefix = "VTYPE"
    _technical_code_label = "Vehicle Type"

    name = fields.Char(required=True)
    code = fields.Char(
        string="Technical Code",
        copy=False,
        index=True,
        required=True,
        readonly=True,
        default=lambda self: self._new_technical_code(),
        help="Stable technical identifier used for synchronization. It is generated automatically and is independent from the display Name.",
    )
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

    _technical_code_prefix = "COLOR"
    _technical_code_label = "Vehicle Color"

    name = fields.Char(required=True)
    code = fields.Char(
        string="Technical Code",
        copy=False,
        index=True,
        required=True,
        readonly=True,
        default=lambda self: self._new_technical_code(),
        help="Stable technical identifier used for synchronization. It is generated automatically and is independent from the display Name.",
    )
    active = fields.Boolean(default=True)

    _sql_constraints = [
        ("nsp_vehicle_color_name_uniq", "unique(name)", "Color already exists."),
        ("nsp_vehicle_color_code_uniq", "unique(code)", "Vehicle Color Code already exists."),
    ]
