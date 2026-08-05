# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError
from odoo.addons.nsp_core.utils import new_management_code


class NspReferenceBase(models.AbstractModel):
    _name = "nsp.reference.base"
    _description = "NSP Reference Data Base"
    _rec_name = "name"
    _order = "name, id"

    _management_prefix = "REF"
    _label = "Reference"

    name = fields.Char(required=True, index=True)
    code = fields.Char(
        string="Technical Code",
        required=True,
        readonly=True,
        copy=False,
        index=True,
        default=lambda self: self._new_reference_code(),
        help="Stable technical identifier used for synchronization. It is generated automatically and is independent from the display Name.",
    )
    active = fields.Boolean(default=True, index=True)

    @api.model
    def _new_reference_code(self):
        return new_management_code(self._management_prefix)

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for source in vals_list:
            vals = dict(source)
            vals["name"] = str(vals.get("name") or "").strip()
            vals["code"] = str(vals.get("code") or self._new_reference_code()).strip().upper()
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
    def _check_required_reference_values(self):
        for record in self:
            if not (record.name or "").strip():
                raise ValidationError(_("%s Name is required.") % self._label)
            if not (record.code or "").strip():
                raise ValidationError(_("%s Code is required.") % self._label)


class NspReferenceBrand(models.Model):
    _name = "nsp.reference.brand"
    _description = "NSP Brand"
    _inherit = "nsp.reference.base"

    _management_prefix = "BRAND"
    _label = "Brand"

    _sql_constraints = [
        ("nsp_reference_brand_name_uniq", "unique(name)", "Brand already exists."),
        ("nsp_reference_brand_code_uniq", "unique(code)", "Brand Code already exists."),
    ]


class NspReferenceModel(models.Model):
    _name = "nsp.reference.model"
    _description = "NSP Model"
    _inherit = "nsp.reference.base"
    _order = "brand_id, name, id"

    _management_prefix = "MODEL"
    _label = "Model"

    brand_id = fields.Many2one(
        "nsp.reference.brand",
        string="Brand",
        required=True,
        ondelete="restrict",
        index=True,
    )

    _sql_constraints = [
        ("nsp_reference_model_brand_name_uniq", "unique(brand_id, name)", "Model already exists for this Brand."),
        ("nsp_reference_model_code_uniq", "unique(code)", "Model Code already exists."),
    ]


class NspReferenceVendor(models.Model):
    _name = "nsp.reference.vendor"
    _description = "NSP Vendor"
    _inherit = "nsp.reference.base"

    _management_prefix = "VENDOR"
    _label = "Vendor"

    _sql_constraints = [
        ("nsp_reference_vendor_name_uniq", "unique(name)", "Vendor already exists."),
        ("nsp_reference_vendor_code_uniq", "unique(code)", "Vendor Code already exists."),
    ]
