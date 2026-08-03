# -*- coding: utf-8 -*-
import re

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class NspRfidRuntimeAssignment(models.Model):
    _name = "nsp.rfid.runtime.assignment"
    _description = "NSP RFID Runtime Assignment"
    _rec_name = "tid"
    _order = "tid, id"

    tid = fields.Char(required=True, index=True, copy=False)
    target_type = fields.Selection(
        [("user", "User"), ("vehicle", "Vehicle")],
        required=True,
        index=True,
    )
    user_id = fields.Many2one(
        "nsp.user", ondelete="cascade", index=True,
    )
    vehicle_id = fields.Many2one(
        "nsp.vehicle", ondelete="cascade", index=True,
    )
    assigned_at = fields.Datetime(index=True)

    _sql_constraints = [
        ("rfid_runtime_tid_unique", "unique(tid)", "RFID TID must be unique."),
        (
            "rfid_runtime_target_check",
            "CHECK((target_type = 'user' AND user_id IS NOT NULL AND vehicle_id IS NULL) OR "
            "(target_type = 'vehicle' AND vehicle_id IS NOT NULL AND user_id IS NULL))",
            "RFID runtime assignment must reference exactly one matching target.",
        ),
    ]

    @api.model
    def _normalize_tid(self, value):
        tid = re.sub(r"\s+", "", str(value or "")).upper()
        return tid[2:] if tid.startswith("0X") else tid

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for source in vals_list:
            vals = dict(source)
            vals["tid"] = self._normalize_tid(vals.get("tid"))
            if not vals["tid"]:
                raise ValidationError(_("RFID TID is required."))
            prepared.append(vals)
        return super().create(prepared)

    def write(self, vals):
        values = dict(vals)
        if "tid" in values:
            values["tid"] = self._normalize_tid(values.get("tid"))
            if not values["tid"]:
                raise ValidationError(_("RFID TID is required."))
        return super().write(values)
