# -*- coding: utf-8 -*-
from passlib.context import CryptContext
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

MOBILE_PASSWORD_CONTEXT = CryptContext(['pbkdf2_sha512'], pbkdf2_sha512__rounds=12000)


class NspUserMobile(models.Model):
    _inherit = 'nsp.user'

    mobile_enabled = fields.Boolean(string='Mobile Access', default=True, index=True)
    mobile_login = fields.Char(string='Mobile Login', copy=False, index=True)
    mobile_password_hash = fields.Char(copy=False, readonly=True, groups='base.group_system')
    mobile_password = fields.Char(
        string='Set Mobile Password', compute='_compute_mobile_password', inverse='_inverse_mobile_password',
        help='Enter a new password to replace the current Mobile password. Plaintext is never stored.',
    )

    _mobile_login_unique = models.Constraint('unique(mobile_login)', 'Mobile Login must be unique.')

    def _compute_mobile_password(self):
        for rec in self:
            rec.mobile_password = False

    def _inverse_mobile_password(self):
        for rec in self:
            value = rec.mobile_password
            if value:
                rec._set_mobile_password(value)

    @api.model
    def _normalize_mobile_login(self, value):
        return str(value or '').strip().lower()

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for rec in records:
            if not rec.mobile_login:
                rec.mobile_login = self._normalize_mobile_login(rec.user_code)
        return records

    def write(self, vals):
        values = dict(vals)
        if 'mobile_login' in values:
            values['mobile_login'] = self._normalize_mobile_login(values.get('mobile_login')) or False
        return super().write(values)

    def _set_mobile_password(self, plaintext):
        self.ensure_one()
        plaintext = str(plaintext or '')
        if len(plaintext) < 8:
            raise ValidationError(_('Mobile password must contain at least 8 characters.'))
        self.sudo().write({'mobile_password_hash': MOBILE_PASSWORD_CONTEXT.hash(plaintext)})
        return True

    def check_mobile_password(self, plaintext):
        self.ensure_one()
        return bool(
            self.mobile_enabled and self.active and self.mobile_password_hash
            and plaintext and self._verify_mobile_password(str(plaintext))
        )


    def _verify_mobile_password(self, plaintext):
        self.ensure_one()
        try:
            return MOBILE_PASSWORD_CONTEXT.verify(plaintext, self.mobile_password_hash or '')
        except (ValueError, TypeError):
            return False

    @api.model
    def authenticate_mobile(self, login, password):
        normalized = self._normalize_mobile_login(login)
        if not normalized:
            return self.browse()
        user = self.sudo().search([
            ('mobile_login', '=', normalized), ('mobile_enabled', '=', True), ('active', '=', True),
        ], limit=1)
        if user and user.check_mobile_password(password):
            return user
        return self.browse()
