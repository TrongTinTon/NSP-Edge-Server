# -*- coding: utf-8 -*-
from passlib.context import CryptContext

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


MOBILE_PASSWORD_CONTEXT = CryptContext(
    ["pbkdf2_sha512"],
    pbkdf2_sha512__rounds=210000,
)


class NspUserMobile(models.Model):
    _inherit = "nsp.user"

    mobile_enabled = fields.Boolean(string="Mobile Access", default=False, index=True)
    mobile_login = fields.Char(string="Mobile Login", copy=False, index=True)
    mobile_password_hash = fields.Char(copy=False, readonly=True, groups="base.group_system")
    mobile_password_set = fields.Boolean(
        string="Password Set",
        compute="_compute_mobile_password_set",
        compute_sudo=True,
        readonly=True,
    )
    mobile_password = fields.Char(
        string="Set Mobile Password",
        compute="_compute_mobile_password",
        inverse="_inverse_mobile_password",
        help="Enter a new password to replace the current Mobile password. Plaintext is never stored.",
    )

    _mobile_login_unique = models.Constraint(
        "unique(mobile_login)",
        "Mobile Login must be unique.",
    )

    def _compute_mobile_password(self):
        for rec in self:
            rec.mobile_password = False

    @api.depends("mobile_password_hash")
    def _compute_mobile_password_set(self):
        for rec in self:
            rec.mobile_password_set = bool(rec.mobile_password_hash)

    def _inverse_mobile_password(self):
        for rec in self:
            value = rec.mobile_password
            if value:
                rec._set_mobile_password(value)

    @api.model
    def _normalize_mobile_login(self, value):
        return str(value or "").strip().lower()

    def write(self, vals):
        values = dict(vals)
        if "mobile_login" in values:
            values["mobile_login"] = self._normalize_mobile_login(values.get("mobile_login")) or False
        result = super().write(values)

        # Identity/access changes invalidate existing Mobile sessions immediately.
        revoke_users = self.browse()
        if values.get("active") is False or values.get("mobile_enabled") is False:
            revoke_users |= self
        if "mobile_login" in values:
            revoke_users |= self
        if revoke_users and "nsp.mobile.session" in self.env.registry.models:
            sessions = self.env["nsp.mobile.session"].sudo().search([
                ("user_id", "in", revoke_users.ids),
                ("state", "=", "active"),
            ])
            if sessions:
                sessions.revoke()
        return result

    @api.constrains("mobile_enabled", "mobile_login")
    def _check_mobile_login(self):
        for rec in self:
            if rec.mobile_enabled and not self._normalize_mobile_login(rec.mobile_login):
                raise ValidationError(_("Mobile Login is required when Mobile Access is enabled."))

    def _set_mobile_password(self, plaintext):
        self.ensure_one()
        plaintext = str(plaintext or "")
        if len(plaintext) < 8:
            raise ValidationError(_("Mobile password must contain at least 8 characters."))
        # Use direct field write with a context flag so the credential update itself does not
        # trigger the login-change session revocation path.
        self.sudo().with_context(nsp_mobile_password_write=True).write({
            "mobile_password_hash": MOBILE_PASSWORD_CONTEXT.hash(plaintext),
        })

        keep_session_uid = self.env.context.get("keep_mobile_session_uid")
        if "nsp.mobile.session" in self.env.registry.models:
            domain = [("user_id", "=", self.id), ("state", "=", "active")]
            if keep_session_uid:
                domain.append(("session_uid", "!=", keep_session_uid))
            sessions = self.env["nsp.mobile.session"].sudo().search(domain)
            if sessions:
                sessions.revoke()
        return True

    def check_mobile_password(self, plaintext):
        self.ensure_one()
        return bool(
            self.mobile_enabled
            and self.active
            and self.mobile_login
            and self.mobile_password_hash
            and plaintext
            and self._verify_mobile_password(str(plaintext))
        )

    def _verify_mobile_password(self, plaintext):
        self.ensure_one()
        try:
            return MOBILE_PASSWORD_CONTEXT.verify(plaintext, self.mobile_password_hash or "")
        except (ValueError, TypeError):
            return False

    @api.model
    def authenticate_mobile(self, login, password):
        normalized = self._normalize_mobile_login(login)
        if not normalized:
            return self.browse()
        user = self.sudo().search([
            ("mobile_login", "=", normalized),
            ("mobile_enabled", "=", True),
            ("active", "=", True),
        ], limit=1)
        if user and user.check_mobile_password(password):
            return user
        return self.browse()
