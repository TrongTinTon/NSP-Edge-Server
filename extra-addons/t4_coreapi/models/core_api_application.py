# Part of T4 Core API. See LICENSE file for full copyright and licensing details.

import logging
import secrets

from passlib.context import CryptContext

from odoo import _, api, fields, models
from odoo.http import request
from odoo.exceptions import AccessError, UserError, ValidationError

_logger = logging.getLogger(__name__)

SECRET_CRYPT_CONTEXT = CryptContext(['pbkdf2_sha512'], pbkdf2_sha512__rounds=6000)


class CoreApiApplication(models.Model):
    _name = 'core.api.application'
    _description = 'External API Application'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'name'

    name = fields.Char(required=True, tracking=True)
    client_id = fields.Char(
        string='Client ID',
        required=False,
        copy=False,
        readonly=True,
        index=True,
        tracking=True,
        help='Auto-generated when the application is saved.',
    )
    client_secret = fields.Char(
        string='Client Secret (hashed)',
        copy=False,
        readonly=True,
        groups='base.group_system',
    )
    client_secret_plaintext = fields.Char(
        string='Client Secret',
        copy=False,
        readonly=True,
        groups='t4_coreapi.group_core_api_manager',
        help='Stored so authorized Core API managers can view credentials whenever required.',
    )
    state = fields.Selection(
        [('active', 'Active'), ('inactive', 'Inactive')],
        string='Status',
        default='active',
        required=True,
        tracking=True,
    )
    active = fields.Boolean(default=True, compute='_compute_active', store=True)
    token_ttl_hours = fields.Integer(
        string='Access Token TTL (hours)',
        default=24,
        help='Lifetime of issued access tokens. 0 = non-expiring (not recommended).',
    )
    refresh_token_ttl_days = fields.Integer(
        string='Refresh Token TTL (days)',
        default=30,
        help='Sliding lifetime. Every successful refresh rotates the refresh token and restarts this TTL.',
    )
    token_ids = fields.One2many('core.api.token', 'application_id')
    token_count = fields.Integer(compute='_compute_token_count')
    has_active_token = fields.Boolean(
        string='Has Active Token',
        compute='_compute_traffic_status',
    )
    api_requests_per_minute = fields.Integer(
        string='Aggregate API Requests (last min)',
        compute='_compute_traffic_status',
    )
    auth_requests_per_minute = fields.Integer(
        string='Aggregate Auth Requests (last min)',
        compute='_compute_traffic_status',
    )
    traffic_status = fields.Selection(
        [
            ('normal', 'Normal'),
            ('elevated', 'Elevated'),
            ('suspicious', 'Suspicious'),
        ],
        string='Traffic Status',
        compute='_compute_traffic_status',
        help='Based on request volume in the last minute versus configured rate limits.',
    )
    credentials_pending = fields.Boolean(
        string='Credentials Not Yet Viewed',
        default=False,
        copy=False,
        readonly=True,
    )
    domain_id = fields.Many2one(
        'core.api.domain',
        string='Host Domain',
        required=True,
        default=lambda self: self.env['core.api.domain'].get_default().id,
        tracking=True,
        help='Public hostname used in integration examples for this application.',
    )
    endpoint_ids = fields.One2many(
        'core.api.endpoint',
        'application_id',
        string='Gateway Routes',
        help='All API routes owned by this application.',
    )
    rate_limit_per_minute = fields.Integer(
        string='API Rate Limit per Token (/min)',
        default=60,
        help='Maximum API calls per minute for each independently issued access token. 0 = unlimited.',
    )
    allowed_ips = fields.Text(
        string='Allowed IPs',
        help='One IP or CIDR per line. Empty = allow any IP.',
    )
    log_ids = fields.One2many('core.api.log', 'application_id')
    log_count = fields.Integer(compute='_compute_log_count')
    last_auth_at = fields.Datetime(readonly=True)
    last_auth_ip = fields.Char(readonly=True)
    notes = fields.Text()
    api_base_url = fields.Char(
        string='API Base URL',
        compute='_compute_api_integration_guide',
    )
    auth_endpoint_url = fields.Char(
        string='Token URL',
        compute='_compute_api_integration_guide',
    )
    auth_curl_example = fields.Text(
        string='Token Request (cURL)',
        compute='_compute_api_integration_guide',
    )
    api_call_curl_example = fields.Text(
        string='API Call (cURL)',
        compute='_compute_api_integration_guide',
    )
    api_database_name = fields.Char(
        string='Database Name',
        compute='_compute_api_integration_guide',
        help='PostgreSQL database name. Required for external API calls when multiple databases exist.',
    )

    _client_id_unique = models.Constraint('unique(client_id)', 'Client ID must be unique.')
    @api.onchange('domain_id')
    def _onchange_domain_id(self):
        """Warn when no API versions exist yet."""
        if not self.env['core.api.version'].search_count([('active', '=', True)]):
            return {
                'warning': {
                    'title': _('No API versions'),
                    'message': _(
                        'No active API versions exist yet. Create them under Configuration → API Versions.',
                    ),
                },
            }
        return {}


    @api.model
    def default_get(self, fields_list):
        """Pre-fill the default host domain."""
        defaults = super().default_get(fields_list)
        if 'domain_id' in fields_list and not defaults.get('domain_id'):
            domain = self.env['core.api.domain'].get_default()
            if domain:
                defaults['domain_id'] = domain.id
        return defaults

    @api.depends('state')
    def _compute_active(self):
        """Mirror application state into the active boolean field."""
        for rec in self:
            rec.active = rec.state == 'active'

    @api.depends('token_ids')
    def _compute_token_count(self):
        """Count issued tokens for the application stat button."""
        for rec in self:
            rec.token_count = len(rec.token_ids)

    @api.depends('log_ids')
    def _compute_log_count(self):
        """Count request logs for the application stat button."""
        for rec in self:
            rec.log_count = len(rec.log_ids)

    @api.depends(
        'client_id',
        'domain_id',
        'domain_id.base_url',
        'endpoint_ids.route_pattern',
        'endpoint_ids.version_id',
        'endpoint_ids.route_suffix',
    )
    def _compute_api_integration_guide(self):
        """Build route-path-only authentication and API examples."""
        from odoo.addons.t4_coreapi.utils.routing import AUTH_TOKEN_PATH, build_gateway_path

        for rec in self:
            base = (rec.domain_id.base_url or '').rstrip('/')
            if not base:
                base = (self.env['ir.config_parameter'].sudo().get_param('web.base.url') or '').rstrip('/')
            version = rec.endpoint_ids[:1].version_id if rec.endpoint_ids else self.env['core.api.version'].get_default_version()
            version_code = version.code if version else 'v1'
            auth_url = f'{base}{AUTH_TOKEN_PATH}'
            db_name = self.env.cr.dbname
            rec.api_database_name = db_name
            rec.api_base_url = f'{base}{build_gateway_path(version_code)}'
            rec.auth_endpoint_url = auth_url
            client_id = rec.client_id or '<client_id>'
            rec.auth_curl_example = (
                f"curl -X POST '{auth_url}?db={db_name}'\n"
                f"  -H 'Content-Type: application/json'\n"
                f"  -d '{{\"client_id\": \"{client_id}\", \"client_secret\": \"<client_secret>\"}}'"
            )
            sample_suffix = rec.endpoint_ids[:1].route_suffix if rec.endpoint_ids else 'health'
            sample_url = f'{base}{build_gateway_path(version_code, sample_suffix)}?db={db_name}'
            rec.api_call_curl_example = (
                f"curl -X GET '{sample_url}'\n"
                f"  -H 'Authorization: Bearer <access_token>'\n"
                f"  -H 'Content-Type: application/json'"
            )


    @api.model
    def _traffic_snapshot(self, application):
        """Return one-minute aggregate counts and status for the busiest token."""
        if not application.id:
            return 0, 0, 'normal', False

        cutoff = fields.Datetime.subtract(fields.Datetime.now(), minutes=1)
        self.env.cr.execute(
            """
            WITH recent AS (
                SELECT event_type, token_id
                  FROM core_api_log
                 WHERE application_id = %s
                   AND create_date >= %s
            ), token_counts AS (
                SELECT token_id, COUNT(*) AS request_count
                  FROM recent
                 WHERE event_type = 'api' AND token_id IS NOT NULL
                 GROUP BY token_id
            )
            SELECT
                COUNT(*) FILTER (WHERE event_type = 'api')::integer,
                COUNT(*) FILTER (WHERE event_type = 'auth')::integer,
                COALESCE((SELECT MAX(request_count) FROM token_counts), 0)::integer
              FROM recent
            """,
            [application.id, cutoff],
        )
        api_count, auth_count, max_token_count = self.env.cr.fetchone() or (0, 0, 0)
        has_token = bool(self.env['core.api.token'].sudo().search_count([
            ('application_id', '=', application.id),
            '|', ('active', '=', True), ('refresh_token_hash', '!=', False),
        ]))
        limit = application.rate_limit_per_minute or 0
        if limit and max_token_count >= limit:
            status = 'suspicious'
        elif limit and max_token_count >= max(1, int(limit * 0.8)):
            status = 'elevated'
        else:
            status = 'normal'
        return api_count, auth_count, status, has_token

    def _compute_traffic_status(self):
        """Compute live request rates and traffic health for each application."""
        for rec in self:
            api_count, auth_count, status, has_token = self._traffic_snapshot(rec)
            rec.api_requests_per_minute = api_count
            rec.auth_requests_per_minute = auth_count
            rec.traffic_status = status
            rec.has_active_token = has_token

    @api.model_create_multi
    def create(self, vals_list):
        """Generate client credentials when a new application is created."""
        prepared = []
        for vals in vals_list:
            vals = dict(vals)
            plaintext_secret = vals.pop('plaintext_client_secret', None)
            if not vals.get('client_id'):
                vals['client_id'] = self._generate_client_id()
            if plaintext_secret:
                vals['client_secret'] = SECRET_CRYPT_CONTEXT.hash(plaintext_secret)
                vals['client_secret_plaintext'] = plaintext_secret
            elif not vals.get('client_secret'):
                plaintext_secret = secrets.token_urlsafe(32)
                vals['client_secret'] = SECRET_CRYPT_CONTEXT.hash(plaintext_secret)
                vals['client_secret_plaintext'] = plaintext_secret
            else:
                plaintext_secret = vals.get('client_secret_plaintext')
            prepared.append((vals, plaintext_secret))
        records = super().create([v for v, _ in prepared])
        for record, (_, plaintext_secret) in zip(records, prepared):
            if not record.client_id:
                record.sudo().write({'client_id': self._generate_client_id()})
            if plaintext_secret:
                record.sudo().write({'credentials_pending': False})
                record._notify_application_form_reload()
        return records

    @api.model
    def _generate_client_id(self):
        """Return a unique client_id value for a new application."""
        return f'app_{secrets.token_hex(16)}'

    def _notify_application_form_reload(self):
        """Ask open application forms to reload after credential state changes."""
        self.ensure_one()
        self.env['bus.bus']._sendone(
            'broadcast',
            'core_api_application_reload',
            {'application_id': self.id},
        )

    def _open_secret_wizard(self, plaintext_secret):
        """Open the credentials popup for the current application."""
        self.ensure_one()
        wizard = self.env['core.api.application.secret.wizard'].create({
            'application_id': self.id,
            'client_id': self.client_id,
            'client_secret': plaintext_secret,
        })
        return {
            'type': 'ir.actions.act_window',
            'name': _('Application Credentials'),
            'res_model': 'core.api.application.secret.wizard',
            'res_id': wizard.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def action_view_credentials(self):
        """Show credentials whenever requested by an authorized manager."""
        self.ensure_one()
        plaintext = self.client_secret_plaintext
        if not plaintext:
            raise UserError(_(
                'The existing secret predates persistent credential viewing and cannot be recovered. '
                'Use "Regenerate Secret" once to create a viewable secret.'
            ))
        return self._open_secret_wizard(plaintext)

    def action_regenerate_secret(self):
        """Issue a new client secret and show it in the credentials wizard."""
        self.ensure_one()
        if self.state != 'active':
            raise UserError(_('Cannot regenerate secret for an inactive application.'))
        plaintext = secrets.token_urlsafe(32)
        self.sudo().write({
            'client_secret': SECRET_CRYPT_CONTEXT.hash(plaintext),
            'client_secret_plaintext': plaintext,
            'credentials_pending': False,
        })
        tokens = self.env['core.api.token'].sudo().search([('application_id', '=', self.id)])
        if tokens:
            tokens.write({
                'active': False,
                'refresh_token_index': False,
                'refresh_token_hash': False,
                'refresh_expiration_date': False,
            })
        self._notify_application_form_reload()
        return self._open_secret_wizard(plaintext)

    def action_set_active(self):
        """Activate the application from the form header."""
        self.write({'state': 'active'})

    def action_set_inactive(self):
        """Deactivate the application from the form header."""
        self.write({'state': 'inactive'})

    def action_revoke_token(self):
        """Revoke every active access token owned by this Application."""
        self.ensure_one()
        tokens = self.env['core.api.token'].sudo().search([
            ('application_id', '=', self.id),
            '|', ('active', '=', True), ('refresh_token_hash', '!=', False),
        ])
        if not tokens:
            raise UserError(_('No active tokens to revoke.'))
        count = len(tokens)
        tokens.write({
            'active': False,
            'refresh_token_index': False,
            'refresh_token_hash': False,
            'refresh_expiration_date': False,
        })
        self._notify_application_form_reload()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Tokens Revoked'),
                'message': _('%(count)s active token pairs for application "%(name)s" were revoked.', count=count, name=self.name),
                'type': 'warning',
                'sticky': False,
            },
        }

    def action_view_tokens(self):
        """Open the token list filtered to this application."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Tokens'),
            'res_model': 'core.api.token',
            'view_mode': 'list,form',
            'domain': [('application_id', '=', self.id)],
            'context': {
                'default_application_id': self.id,
                'active_test': False,
            },
        }

    def action_view_logs(self):
        """Open the request log list filtered to this application."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Request Logs'),
            'res_model': 'core.api.log',
            'view_mode': 'list,form',
            'domain': [('application_id', '=', self.id)],
        }

    @api.model
    def set_api_response(self, data):
        """Call from any Server Action to return JSON to the API client."""
        request.core_api_response = data

    def get_api_context(self):
        """Return request data injected by the gateway for server action code."""
        self.ensure_one()
        ctx = self.env.context
        return {
            'method': ctx.get('core_api_method'),
            'route': ctx.get('core_api_route'),
            'endpoint_code': ctx.get('core_api_endpoint_code'),
            'body': ctx.get('core_api_body') or {},
            'params': ctx.get('core_api_params') or {},
        }

    def check_ip_allowed(self, ip_address):
        """Raise AccessError when the client IP is not in the allowlist."""
        self.ensure_one()
        from odoo.addons.t4_coreapi.utils.security import check_ip_allowed
        if not check_ip_allowed(self.allowed_ips, ip_address):
            raise AccessError(
                _('IP address %(ip)s is not allowed for application "%(app)s".',
                  ip=ip_address, app=self.name)
            )
        return True

    def check_api_rate_limit(self, token):
        """Enforce the configured API limit for one access token."""
        self.ensure_one()
        from odoo.addons.t4_coreapi.utils.security import check_token_api_rate_limit
        check_token_api_rate_limit(self, token)
        return True

    @api.model
    def authenticate_client(self, client_id, client_secret, ip_address=None):
        """Validate client credentials. Returns application or empty recordset."""
        application, _error = self.authenticate_client_with_reason(
            client_id, client_secret, ip_address=ip_address,
        )
        return application

    @api.model
    def authenticate_client_with_reason(self, client_id, client_secret, ip_address=None):
        """Validate client credentials. Returns (application, error_message)."""
        if not (client_id or '').strip():
            return self.browse(), _('client_id is required.')
        if not client_secret:
            return self.browse(), _('client_secret is required.')

        client_id = client_id.strip()
        application = self.sudo().search([('client_id', '=', client_id)], limit=1)
        if not application:
            return self.browse(), _('No application found for the given client_id.')

        if application.state != 'active':
            return self.browse(), _('Application "%s" is inactive.') % application.name

        if not application.client_secret or not SECRET_CRYPT_CONTEXT.verify(
            client_secret, application.client_secret
        ):
            return self.browse(), _('Invalid client_secret for the given client_id.')

        application.write({
            'last_auth_at': fields.Datetime.now(),
            'last_auth_ip': ip_address or False,
        })
        return application, None

    def check_api_access(self, endpoint_code, version_id=None):
        """Raise AccessError when the application cannot call the endpoint code."""
        self.ensure_one()
        if self.state != 'active':
            raise AccessError(_('Application "%s" is inactive.', self.name))
        endpoints = self.endpoint_ids.filtered(
            lambda e: e.route_active and e.code == endpoint_code
        )
        if version_id:
            endpoints = endpoints.filtered(lambda e: e.version_id.id == version_id)
        if not endpoints:
            inactive = self.endpoint_ids.filtered(
                lambda e: not e.route_active and e.code == endpoint_code
                and (not version_id or e.version_id.id == version_id)
            )
            if inactive:
                raise AccessError(
                    _('Gateway route "%(endpoint)s" is inactive for application "%(app)s".',
                      endpoint=endpoint_code, app=self.name)
                )
            raise AccessError(
                _('Application "%(app)s" is not allowed to access API: %(endpoint)s',
                  app=self.name, endpoint=endpoint_code)
            )
        return True

    def check_route_access(self, path, method=None):
        """Match request path against allowed endpoint route patterns."""
        self.ensure_one()
        active_endpoints = self.endpoint_ids.filtered('route_active')
        if not active_endpoints:
            raise AccessError(_('Application "%s" has no active APIs configured.', self.name))
        normalized = (path or '').split('?')[0].rstrip('/') or '/'
        for endpoint in active_endpoints:
            pattern = (endpoint.route_pattern or '').rstrip('/') or '/'
            if normalized == pattern or normalized.startswith(f'{pattern}/'):
                return endpoint.code
        raise AccessError(
            _('Application "%(app)s" is not allowed to call route: %(route)s',
              app=self.name, route=path)
        )
