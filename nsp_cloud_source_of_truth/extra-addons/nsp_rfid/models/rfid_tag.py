from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class NspRfidTag(models.Model):
    _name = "nsp.rfid.tag"
    _description = "NSP RFID Tag Whitelist"
    _rec_name = "tid"
    _order = "tid, id"

    tid = fields.Char(required=True, index=True)

    _sql_constraints = [
        ("tid_unique", "unique(tid)", "TID must be unique in RFID Tag Whitelist."),
    ]

    @api.model
    def _normalize_tid(self, value):
        tid = "".join(str(value or "").strip().upper().split())
        return tid[2:] if tid.startswith("0X") else tid

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for source in vals_list:
            vals = dict(source)
            vals["tid"] = self._normalize_tid(vals.get("tid"))
            prepared.append(vals)
        return super().create(prepared)

    def write(self, vals):
        values = dict(vals)
        if "tid" in values:
            values["tid"] = self._normalize_tid(values.get("tid"))
        return super().write(values)

    def unlink(self):
        if not self.env.context.get("module_uninstall"):
            raise ValidationError(_("RFID Tag Whitelist records cannot be deleted."))
        return super().unlink()

    @api.constrains("tid")
    def _check_tid(self):
        for tag in self:
            normalized = tag._normalize_tid(tag.tid)
            if not normalized:
                raise ValidationError(_("TID is required."))
            if tag.tid != normalized:
                raise ValidationError(_("TID must be uppercase without whitespace."))

    @api.model
    def get_or_create_by_tid(self, tid):
        normalized = self._normalize_tid(tid)
        if not normalized:
            raise ValidationError(_("TID is required."))
        tag = self.sudo().search([("tid", "=", normalized)], limit=1)
        return tag or self.sudo().create({"tid": normalized})

    @api.model
    def nsp_validate_scan(self, tid, create_missing=False, **kwargs):
        normalized = self._normalize_tid(tid)
        if not normalized:
            return {"valid": False, "message": _("Scan or enter an RFID TID first.")}
        tag = self.sudo().search([("tid", "=", normalized)], limit=1)
        if not tag and create_missing:
            tag = self.sudo().create({"tid": normalized})
        if not tag:
            return {
                "valid": False,
                "tid": normalized,
                "message": _("RFID Tag %s is not in RFID Tag Whitelist.") % normalized,
            }
        return {
            "valid": True,
            "tid": tag.tid,
            "tag_id": tag.id,
            "message": _("RFID Tag is valid."),
        }
