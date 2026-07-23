# Part of T4 Core API. See LICENSE file for full copyright and licensing details.

import binascii
import datetime
import logging
import os

from passlib.context import CryptContext

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.http import request

_logger = logging.getLogger(__name__)

TOKEN_SIZE = 32
REFRESH_TOKEN_SIZE = 48
INDEX_SIZE = 8
TOKEN_CRYPT_CONTEXT = CryptContext(['pbkdf2_sha512'], pbkdf2_sha512__rounds=6000)


class CoreApiToken(models.Model):
    _name = 'core.api.token'
    _description = 'Core API Token Pair'
    _order = 'create_date desc'

    name = fields.Char(required=True)
    application_id = fields.Many2one('core.api.application', required=True, ondelete='cascade', index=True)
    application_name = fields.Char(related='application_id.name', store=True)
    client_id = fields.Char(related='application_id.client_id', store=True, index=True)
    application_state = fields.Selection(related='application_id.state', readonly=True)
    traffic_status = fields.Selection(related='application_id.traffic_status', readonly=True)
    active = fields.Boolean(default=True)
    token_state = fields.Selection(
        [('active', 'Active'), ('revoked', 'Revoked')],
        string='Access Status', compute='_compute_token_state', search='_search_token_state',
    )
    expiration_date = fields.Datetime(string='Access Expires At', index=True)
    last_used_at = fields.Datetime(readonly=True)
    last_used_ip = fields.Char(readonly=True)
    token_index = fields.Char(size=INDEX_SIZE, readonly=True, index=True)
    token_hash = fields.Char(readonly=True, groups='base.group_system')
    refresh_expiration_date = fields.Datetime(string='Refresh Expires At', index=True, readonly=True)
    refresh_token_index = fields.Char(size=INDEX_SIZE, readonly=True, index=True)
    refresh_token_hash = fields.Char(readonly=True, groups='base.group_system')

    _index_unique = models.Constraint('unique(token_index)', 'Access token index must be unique.')
    _refresh_index_unique = models.Constraint('unique(refresh_token_index)', 'Refresh token index must be unique.')

    @api.depends('active')
    def _compute_token_state(self):
        for token in self:
            token.token_state = 'active' if token.active else 'revoked'

    def _search_token_state(self, operator, value):
        if operator not in ('=', '!='):
            return []
        want_active = value == 'active'
        if operator == '!=':
            want_active = not want_active
        return [('active', '=', want_active)]

    @api.model
    def _generate_plaintext(self, size=TOKEN_SIZE):
        return binascii.hexlify(os.urandom(size)).decode()

    @api.model
    def _expiration_from_hours(self, hours):
        return fields.Datetime.now() + datetime.timedelta(hours=hours) if hours else False

    @api.model
    def _expiration_from_days(self, days):
        return fields.Datetime.now() + datetime.timedelta(days=days) if days else False

    @api.model
    def issue_for_application(self, application):
        """Issue an independent rotating access/refresh pair for one shared credential login."""
        application.ensure_one()
        if application.state != 'active':
            raise UserError(_('Cannot issue tokens for an inactive application.'))

        access = self._generate_plaintext(TOKEN_SIZE)
        refresh = self._generate_plaintext(REFRESH_TOKEN_SIZE)
        token_rec = self.sudo().create({
            'name': f'{application.name} — Token Pair',
            'application_id': application.id,
            'expiration_date': self._expiration_from_hours(application.token_ttl_hours),
            'token_index': access[:INDEX_SIZE],
            'token_hash': TOKEN_CRYPT_CONTEXT.hash(access),
            'refresh_expiration_date': self._expiration_from_days(application.refresh_token_ttl_days),
            'refresh_token_index': refresh[:INDEX_SIZE],
            'refresh_token_hash': TOKEN_CRYPT_CONTEXT.hash(refresh),
        })
        ip = request.httprequest.environ.get('REMOTE_ADDR', 'n/a') if request else 'n/a'
        _logger.info('Core API token pair issued for application %s from %s', application.client_id, ip)
        return {
            'access_token': access,
            'refresh_token': refresh,
            'access_token_rec': token_rec,
        }

    @api.model
    def authenticate(self, plaintext_token):
        """Validate an access token and return (application, token record)."""
        empty_application = self.env['core.api.application']
        empty_token = self.browse()
        if not plaintext_token or len(plaintext_token) < INDEX_SIZE:
            return empty_application, empty_token
        tokens = self.sudo().search([
            ('active', '=', True),
            ('token_index', '=', plaintext_token[:INDEX_SIZE]),
            ('application_id.state', '=', 'active'),
            '|', ('expiration_date', '=', False), ('expiration_date', '>=', fields.Datetime.now()),
        ])
        for token in tokens:
            if TOKEN_CRYPT_CONTEXT.verify(plaintext_token, token.token_hash):
                ip = request.httprequest.environ.get('REMOTE_ADDR') if request else None
                token.write({'last_used_at': fields.Datetime.now(), 'last_used_ip': ip})
                return token.application_id, token
        return empty_application, empty_token

    @api.model
    def consume_refresh_token(self, plaintext_token):
        """Validate and consume one refresh token. The old access token remains valid until expiry."""
        empty_application = self.env['core.api.application']
        empty_token = self.browse()
        if not plaintext_token or len(plaintext_token) < INDEX_SIZE:
            return empty_application, empty_token
        tokens = self.sudo().search([
            ('refresh_token_index', '=', plaintext_token[:INDEX_SIZE]),
            ('refresh_token_hash', '!=', False),
            ('application_id.state', '=', 'active'),
            '|', ('refresh_expiration_date', '=', False), ('refresh_expiration_date', '>=', fields.Datetime.now()),
        ])
        for token in tokens:
            if TOKEN_CRYPT_CONTEXT.verify(plaintext_token, token.refresh_token_hash):
                application = token.application_id
                # Rotation: a refresh token is one-time use. Do not revoke the old access token.
                token.write({
                    'refresh_token_index': False,
                    'refresh_token_hash': False,
                    'refresh_expiration_date': False,
                })
                return application, token
        return empty_application, empty_token

    def action_revoke(self):
        """Revoke selected access tokens and invalidate their refresh tokens."""
        if not self.env.user.has_group('t4_coreapi.group_core_api_manager'):
            raise AccessError(_('Only Core API managers can revoke tokens.'))
        for token in self:
            token.sudo().write({
                'active': False,
                'refresh_token_index': False,
                'refresh_token_hash': False,
                'refresh_expiration_date': False,
            })
            _logger.info('Core API token pair revoked: application %s #%s', token.client_id, token.id)
            if token.application_id:
                token.application_id._notify_application_form_reload()

    @api.autovacuum
    def _gc_expired_tokens(self):
        expired = self.sudo().search([
            ('active', '=', True),
            ('expiration_date', '!=', False),
            ('expiration_date', '<', fields.Datetime.now()),
        ])
        if expired:
            expired.write({'active': False})
            _logger.info('Core API: deactivated %s expired access token(s).', len(expired))

        expired_refresh = self.sudo().search([
            ('refresh_token_hash', '!=', False),
            ('refresh_expiration_date', '!=', False),
            ('refresh_expiration_date', '<', fields.Datetime.now()),
        ])
        if expired_refresh:
            expired_refresh.write({
                'refresh_token_index': False,
                'refresh_token_hash': False,
                'refresh_expiration_date': False,
            })

    @api.model
    def _gc_old_tokens(self):
        days = int(self.env['ir.config_parameter'].sudo().get_param('t4_coreapi.token_retention_days', '7'))
        limit = fields.Datetime.subtract(fields.Datetime.now(), days=days)
        old = self.sudo().with_context(active_test=False).search([
            ('active', '=', False),
            ('refresh_token_hash', '=', False),
            ('create_date', '<', limit),
        ])
        if old:
            count = len(old)
            old.unlink()
            _logger.info('Core API: deleted %s fully expired/revoked token pair(s) older than %s day(s).', count, days)
        return True
