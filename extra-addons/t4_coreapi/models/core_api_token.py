# Part of T4 Core API. See LICENSE file for full copyright and licensing details.

import binascii
import datetime
import logging
import os
import uuid

from passlib.context import CryptContext

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.http import request

_logger = logging.getLogger(__name__)

TOKEN_SIZE = 32
INDEX_SIZE = 8

TOKEN_CRYPT_CONTEXT = CryptContext(['pbkdf2_sha512'], pbkdf2_sha512__rounds=6000)


class CoreApiToken(models.Model):
    _name = 'core.api.token'
    _description = 'Core API Access Token'
    _order = 'create_date desc'

    name = fields.Char(required=True)
    application_id = fields.Many2one(
        'core.api.application',
        required=True,
        ondelete='cascade',
        index=True,
    )
    application_name = fields.Char(related='application_id.name', store=True)
    service_code = fields.Char(related='application_id.service_code', store=True, readonly=True)
    client_id = fields.Char(related='application_id.client_id', store=True, index=True)
    application_state = fields.Selection(related='application_id.state', readonly=True)
    traffic_status = fields.Selection(related='application_id.traffic_status', readonly=True)
    api_requests_per_minute = fields.Integer(
        related='application_id.api_requests_per_minute',
        readonly=True,
        string='API Requests (last min)',
    )
    auth_requests_per_minute = fields.Integer(
        related='application_id.auth_requests_per_minute',
        readonly=True,
        string='Auth Requests (last min)',
    )
    rate_limit_per_minute = fields.Integer(
        related='application_id.rate_limit_per_minute',
        readonly=True,
    )
    auth_rate_limit_per_minute = fields.Integer(
        related='application_id.auth_rate_limit_per_minute',
        readonly=True,
    )
    token_type = fields.Selection(
        [('access', 'Access Token'), ('refresh', 'Refresh Token')],
        string='Type',
        required=True,
        default='access',
        index=True,
    )
    token_pair_id = fields.Char(
        string='Token Pair',
        index=True,
        help='Links access and refresh tokens issued together.',
    )
    active = fields.Boolean(default=True)
    token_state = fields.Selection(
        [
            ('active', 'Active'),
            ('revoked', 'Revoked'),
        ],
        string='Status',
        compute='_compute_token_state',
        search='_search_token_state',
    )
    expiration_date = fields.Datetime(index=True)
    last_used_at = fields.Datetime(readonly=True)
    last_used_ip = fields.Char(readonly=True)
    token_index = fields.Char(size=INDEX_SIZE, readonly=True, index=True)
    token_hash = fields.Char(readonly=True, groups='base.group_system')

    _index_unique = models.Constraint('unique(token_index)', 'Token index must be unique.')

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
    def _generate_plaintext(self):
        """Return a new random token string."""
        return binascii.hexlify(os.urandom(TOKEN_SIZE)).decode()

    @api.model
    def _create_token_record(self, application, token_type, expiration, pair_id, name_suffix):
        """Create one hashed token record and return (plaintext, record)."""
        plaintext = self._generate_plaintext()
        token_rec = self.sudo().create({
            'name': f'{application.name} — {name_suffix}',
            'application_id': application.id,
            'token_type': token_type,
            'token_pair_id': pair_id,
            'expiration_date': expiration,
            'token_index': plaintext[:INDEX_SIZE],
            'token_hash': TOKEN_CRYPT_CONTEXT.hash(plaintext),
        })
        return plaintext, token_rec

    @api.model
    def _expiration_from_hours(self, hours):
        """Return an expiration datetime from TTL hours, or False when non-expiring."""
        if not hours:
            return False
        return fields.Datetime.now() + datetime.timedelta(hours=hours)

    @api.model
    def _revoke_active_tokens(self, domain):
        """Deactivate tokens matching the given search domain."""
        tokens = self.sudo().search(domain + [('active', '=', True)])
        if tokens:
            tokens.write({'active': False})

    @api.model
    def issue_for_application(self, application, revoke_existing=False):
        """Issue an independent access + refresh token pair.

        Existing token pairs remain active by default so multiple clients can
        safely share one Core API Application. ``revoke_existing=True`` is
        reserved for explicit administrative/security rotation workflows.

        Returns a dict with plaintext tokens and their records.
        """
        application.ensure_one()
        if application.state != 'active':
            raise UserError(_('Cannot issue a token for an inactive application.'))

        if revoke_existing:
            self._revoke_active_tokens([('application_id', '=', application.id)])

        pair_id = str(uuid.uuid4())
        access_expiration = self._expiration_from_hours(application.token_ttl_hours)
        refresh_expiration = self._expiration_from_hours(application.refresh_token_ttl_hours)

        refresh_plaintext, refresh_rec = self._create_token_record(
            application,
            'refresh',
            refresh_expiration,
            pair_id,
            'Refresh',
        )
        access_plaintext, access_rec = self._create_token_record(
            application,
            'access',
            access_expiration,
            pair_id,
            'Access',
        )

        ip = request.httprequest.environ.get('REMOTE_ADDR', 'n/a') if request else 'n/a'
        _logger.info(
            'Core API token pair issued for application %s from %s',
            application.client_id,
            ip,
        )
        return {
            'access_token': access_plaintext,
            'refresh_token': refresh_plaintext,
            'access_token_rec': access_rec,
            'refresh_token_rec': refresh_rec,
        }

    @api.model
    def _authenticate_token(self, plaintext_token, token_type):
        """Validate a token of the given type. Returns (application, token) or empty."""
        empty_application = self.env['core.api.application']
        empty_token = self.browse()
        if not plaintext_token or len(plaintext_token) < INDEX_SIZE:
            return empty_application, empty_token

        index = plaintext_token[:INDEX_SIZE]
        tokens = self.sudo().search([
            ('active', '=', True),
            ('token_type', '=', token_type),
            ('token_index', '=', index),
            ('application_id.state', '=', 'active'),
            '|',
            ('expiration_date', '=', False),
            ('expiration_date', '>=', fields.Datetime.now()),
        ])
        for token in tokens:
            if TOKEN_CRYPT_CONTEXT.verify(plaintext_token, token.token_hash):
                ip = request.httprequest.environ.get('REMOTE_ADDR') if request else None
                token.write({'last_used_at': fields.Datetime.now(), 'last_used_ip': ip})
                return token.application_id, token
        return empty_application, empty_token

    @api.model
    def authenticate(self, plaintext_token):
        """Validate a bearer access token. Returns (application, token) or empty."""
        return self._authenticate_token(plaintext_token, 'access')

    @api.model
    def authenticate_refresh(self, plaintext_token):
        """Validate a refresh token. Returns (application, token) or empty."""
        return self._authenticate_token(plaintext_token, 'refresh')

    @api.model
    def refresh_for_application(self, refresh_plaintext):
        """Rotate an access + refresh pair using a valid refresh token."""
        application, refresh_token = self.authenticate_refresh(refresh_plaintext)
        if not application:
            return None

        self._revoke_active_tokens([('token_pair_id', '=', refresh_token.token_pair_id)])
        return self.issue_for_application(application, revoke_existing=False)

    def action_revoke(self):
        """Deactivate the selected token records."""
        if not self.env.user.has_group('t4_coreapi.group_core_api_manager'):
            raise AccessError(_('Only Core API managers can revoke tokens.'))
        for token in self:
            if token.token_pair_id:
                self.sudo().search([
                    ('token_pair_id', '=', token.token_pair_id),
                    ('active', '=', True),
                ]).write({'active': False})
            else:
                token.sudo().write({'active': False})
            _logger.info(
                'Core API token revoked: application %s #%s',
                token.client_id,
                token.id,
            )
            if token.application_id:
                token.application_id._notify_application_form_reload()

    @api.autovacuum
    def _gc_expired_tokens(self):
        """Deactivate expired tokens during the autovacuum job."""
        expired = self.sudo().search([
            ('active', '=', True),
            ('expiration_date', '!=', False),
            ('expiration_date', '<', fields.Datetime.now()),
        ])
        if expired:
            expired.write({'active': False})
            _logger.info('Core API: deactivated %s expired token(s).', len(expired))

    @api.model
    def _gc_old_tokens(self):
        """Delete revoked tokens older than the configured retention window."""
        days = int(self.env['ir.config_parameter'].sudo().get_param(
            't4_coreapi.token_retention_days', '7',
        ))
        limit = fields.Datetime.subtract(fields.Datetime.now(), days=days)
        old = self.sudo().with_context(active_test=False).search([
            ('active', '=', False),
            ('create_date', '<', limit),
        ])
        if old:
            count = len(old)
            old.unlink()
            _logger.info(
                'Core API: deleted %s revoked token(s) older than %s day(s).',
                count, days,
            )
        return True
