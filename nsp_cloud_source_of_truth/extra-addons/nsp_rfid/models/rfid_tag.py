from collections import Counter

from psycopg2 import IntegrityError, errorcodes

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from ..utils import is_valid_tid, normalize_tid


class NspRfidTag(models.Model):
    _name = "nsp.rfid.tag"
    _description = "NSP RFID Tag Whitelist"
    _rec_name = "tid"
    _order = "tid, id"

    tid = fields.Char(required=True, copy=False, index=True)

    _tid_unique = models.Constraint(
        "UNIQUE(tid)",
        "TID must be unique in RFID Tag Whitelist.",
    )

    @api.model
    def _normalize_tid(self, value):
        return normalize_tid(value)

    @api.model
    def _prepare_tid(self, value):
        tid = normalize_tid(value)
        if not tid:
            raise ValidationError(_("TID is required."))
        if not is_valid_tid(tid):
            raise ValidationError(
                _("TID must contain hexadecimal characters only (0-9, A-F).")
            )
        return tid

    @api.model
    def _raise_if_duplicate_tids(self, tids, excluded_ids=None):
        duplicate_values = sorted(
            tid for tid, count in Counter(tids).items() if count > 1
        )
        if duplicate_values:
            raise ValidationError(
                _("Duplicate TID in this request: %s")
                % ", ".join(duplicate_values)
            )

        domain = [("tid", "in", list(tids))]
        if excluded_ids:
            domain.append(("id", "not in", list(excluded_ids)))
        duplicate = self.sudo().search(domain, limit=1)
        if duplicate:
            raise ValidationError(
                _("TID %s already exists in RFID Tag Whitelist.") % duplicate.tid
            )

    @staticmethod
    def _is_unique_violation(error):
        return getattr(error, "pgcode", None) == errorcodes.UNIQUE_VIOLATION

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        tids = []
        for source in vals_list:
            values = dict(source)
            values["tid"] = self._prepare_tid(values.get("tid"))
            tids.append(values["tid"])
            prepared.append(values)

        self._raise_if_duplicate_tids(tids)
        try:
            with self.env.cr.savepoint():
                return super().create(prepared)
        except IntegrityError as error:
            if not self._is_unique_violation(error):
                raise
            raise ValidationError(
                _("TID already exists in RFID Tag Whitelist.")
            ) from error

    def write(self, vals):
        values = dict(vals)
        if "tid" not in values:
            return super().write(values)
        if len(self) != 1:
            raise ValidationError(
                _("TID cannot be changed for multiple RFID Tags at once.")
            )

        values["tid"] = self._prepare_tid(values.get("tid"))
        self._raise_if_duplicate_tids([values["tid"]], excluded_ids=self.ids)
        try:
            with self.env.cr.savepoint():
                return super().write(values)
        except IntegrityError as error:
            if not self._is_unique_violation(error):
                raise
            raise ValidationError(
                _("TID %s already exists in RFID Tag Whitelist.") % values["tid"]
            ) from error

    def unlink(self):
        if not self.env.context.get("module_uninstall"):
            raise ValidationError(_("RFID Tag Whitelist records cannot be deleted."))
        return super().unlink()

    @api.model
    def get_or_create_by_tid(self, tid):
        normalized = self._prepare_tid(tid)
        Tag = self.sudo()
        existing = Tag.search([("tid", "=", normalized)], limit=1)
        if existing:
            return existing

        try:
            with self.env.cr.savepoint():
                return Tag.create({"tid": normalized})
        except (IntegrityError, ValidationError):
            existing = Tag.search([("tid", "=", normalized)], limit=1)
            if existing:
                return existing
            raise

    @api.model
    def nsp_validate_new_tid(self, tid, current_id=False):
        try:
            normalized = self._prepare_tid(tid)
        except ValidationError as error:
            return {
                "valid": False,
                "tid": normalize_tid(tid),
                "message": error.args[0] if error.args else _("TID is invalid."),
            }

        domain = [("tid", "=", normalized)]
        if current_id:
            domain.append(("id", "!=", int(current_id)))
        duplicate = self.sudo().search(domain, limit=1)
        if duplicate:
            return {
                "valid": False,
                "tid": normalized,
                "duplicate_id": duplicate.id,
                "message": _("TID %s already exists in RFID Tag Whitelist.")
                % normalized,
            }
        return {
            "valid": True,
            "tid": normalized,
            "message": _("TID %s is normalized and available.") % normalized,
        }

    @api.model
    def nsp_validate_scan(self, tid, create_missing=False, **kwargs):
        try:
            normalized = self._prepare_tid(tid)
        except ValidationError as error:
            return {
                "valid": False,
                "tid": normalize_tid(tid),
                "message": error.args[0] if error.args else _("RFID TID is invalid."),
            }

        Tag = self.sudo()
        tag = Tag.search([("tid", "=", normalized)], limit=1)
        if not tag and create_missing:
            tag = Tag.get_or_create_by_tid(normalized)
        if not tag:
            return {
                "valid": False,
                "tid": normalized,
                "message": _("RFID Tag %s is not in RFID Tag Whitelist.")
                % normalized,
            }
        return {
            "valid": True,
            "tid": tag.tid,
            "tag_id": tag.id,
            "message": _("RFID Tag is valid."),
        }
