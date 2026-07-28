# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError
from odoo.addons.nsp_core.utils import new_management_code


class NspDeviceType(models.Model):
    _name = "nsp.device.type"
    _description = "NSP Device Type"
    _rec_name = "name"
    _order = "name, id"

    name = fields.Char(required=True, index=True)
    code = fields.Char(
        required=True,
        readonly=True,
        copy=False,
        index=True,
        default=lambda self: new_management_code("DTYPE"),
        help="Stable identifier used for Cloud/Edge synchronization.",
    )
    active = fields.Boolean(default=True, index=True)

    _sql_constraints = [
        ("nsp_device_type_name_uniq", "unique(name)", "Device Type already exists."),
        ("nsp_device_type_code_uniq", "unique(code)", "Device Type Code already exists."),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for source in vals_list:
            vals = dict(source)
            vals["name"] = str(vals.get("name") or "").strip()
            vals["code"] = str(vals.get("code") or new_management_code("DTYPE")).strip().upper()
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
    def _check_required_values(self):
        for record in self:
            if not (record.name or "").strip():
                raise ValidationError(_("Device Type Name is required."))
            if not (record.code or "").strip():
                raise ValidationError(_("Device Type Code is required."))
