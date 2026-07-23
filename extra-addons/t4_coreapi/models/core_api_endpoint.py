# Part of T4 Core API. See LICENSE file for full copyright and licensing details.

import json
import logging
import re

from werkzeug.exceptions import BadRequest, Forbidden, NotFound

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError
from odoo.http import request

from odoo.addons.t4_coreapi.utils.exception import (
    CoreApiBadRequest,
    CoreApiInvalidBody,
)
from odoo.addons.t4_coreapi.utils.response import (
    api_error_response,
    make_json_response,
    normalize_gateway_response,
)
from odoo.addons.t4_coreapi.utils.routing import build_gateway_path

_logger = logging.getLogger(__name__)

_GATEWAY_ROUTE_RE = re.compile(r'^/([^/]+)(?:/(.*))?$')


class CoreApiEndpoint(models.Model):
    _name = 'core.api.endpoint'
    _description = 'Core API Gateway Route'
    _order = 'version_id, route_suffix, code'

    name = fields.Char(required=True, translate=True)
    code = fields.Char(
        required=True,
        index=True,
        help='Unique route code per application and API version. Used in logs and API context.',
    )
    version_id = fields.Many2one(
        'core.api.version',
        string='API Version',
        required=True,
        ondelete='restrict',
        index=True,
    )
    route_suffix = fields.Char(
        string='Route Path',
        required=True,
        help='Route Path only, e.g. edge-server/status. Public URL is /{version}/{route_path}.' ,
    )
    route_pattern = fields.Char(
        string='Gateway Path',
        compute='_compute_route_pattern',
        store=True,
        readonly=True,
        help='Computed public path, e.g. /v1/edge-server/status.',
    )
    public_gateway_url = fields.Char(
        string='Full Gateway URL',
        compute='_compute_public_gateway_url',
        help='Full public URL including host domain, e.g. https://localhost:8069/v1/edge-server/status.',
    )
    http_methods = fields.Char(
        string='Allowed Methods',
        default='GET,POST,PUT,PATCH,DELETE',
        help='Comma-separated HTTP methods applications may use.',
    )
    action_id = fields.Many2one(
        # 'ir.actions.server',
        'ir.actions.core_api',
        string='Server Action',
        help='Executed after auth check. Use env.context core_api_* keys in the action.',
    )
    description = fields.Text(translate=True)
    route_active = fields.Boolean(string='Active', default=True)
    application_id = fields.Many2one(
        'core.api.application',
        string='Application',
        ondelete='cascade',
        index=True,
    )
    endpoint_manager_id = fields.Many2one(
        'action.endpoint.manager',
        string='Endpoint Manager',
        ondelete='set null',
        index=True,
        help='Action Endpoint Manager that generated this Gateway Route.',
    )

    _code_unique_per_application_version = models.Constraint(
        'unique(application_id, version_id, code)',
        'Endpoint code must be unique per application and API version.',
    )
    _route_unique_per_application_version = models.Constraint(
        'unique(application_id, version_id, route_suffix)',
        'Route path must be unique per application and API version.',
    )

    @api.depends('version_id.code', 'route_suffix')
    def _compute_route_pattern(self):
        for endpoint in self:
            endpoint.route_pattern = build_gateway_path(
                endpoint.version_id.code if endpoint.version_id else False,
                endpoint.route_suffix,
            )

    @api.depends(
        'route_pattern',
        'application_id.domain_id.base_url',
    )
    def _compute_public_gateway_url(self):
        """Build the full public URL using the application host domain."""
        web_base = (
            self.env['ir.config_parameter'].sudo().get_param('web.base.url') or ''
        ).rstrip('/')
        for endpoint in self:
            base = web_base
            if endpoint.application_id and endpoint.application_id.domain_id:
                domain_base = (endpoint.application_id.domain_id.base_url or '').rstrip('/')
                if domain_base:
                    base = domain_base
            path = (endpoint.route_pattern or '').strip()
            endpoint.public_gateway_url = f'{base}{path}' if path else base

    @api.constrains('application_id')
    def _check_application_id(self):
        """Block saving a route that is not linked to an application."""
        for endpoint in self:
            if not endpoint.application_id:
                raise ValidationError(
                    _('Each gateway route must belong to an application.')
                )

    @api.constrains('route_suffix')
    def _check_route_suffix(self):
        """Reject empty route paths."""
        for endpoint in self:
            if not (endpoint.route_suffix or '').strip().strip('/'):
                raise ValidationError(_('Route path is required.'))

    @api.model
    def _version_id_from_context(self, application_id=None):
        """Resolve API version from x2many context (tab-specific defaults)."""
        Version = self.env['core.api.version']
        default_version_id = self.env.context.get('default_version_id')
        if default_version_id:
            version = Version.browse(default_version_id).exists()
            if version:
                return version.id

        version_code = self.env.context.get('default_version_code')
        if version_code:
            version = Version.search([('code', '=', version_code), ('active', '=', True)], limit=1)
            if version:
                return version.id
        return False

    @api.model
    def default_get(self, fields_list):
        """Apply tab context so new inline rows get the correct API version."""
        defaults = super().default_get(fields_list)
        if 'version_id' not in fields_list:
            return defaults
        application_id = (
            defaults.get('application_id')
            or self.env.context.get('default_application_id')
        )
        version_id = self._version_id_from_context(application_id=application_id)
        if version_id:
            defaults['version_id'] = version_id
        elif not defaults.get('version_id'):
            default_version = self.env['core.api.version'].get_default_version()
            if default_version:
                defaults['version_id'] = default_version.id
        return defaults

    @api.model_create_multi
    def create(self, vals_list):
        """Fill application_id and version_id from context without dropping rows.

        Fresh-install note:
        Generate API Actions & Routes creates core.api.endpoint rows with an
        explicit application_id and version_id. The previous implementation only
        appended vals when version_id was missing and found from context, so
        generated routes could silently create zero records. Always append every
        prepared row, then let required-field constraints report real errors.
        """
        prepared = []
        for vals in vals_list:
            vals = dict(vals)
            if not vals.get('application_id'):
                default_app = self.env.context.get('default_application_id')
                if default_app:
                    vals['application_id'] = default_app

            default_version_id = self.env.context.get('default_version_id')
            if default_version_id and not vals.get('version_id'):
                vals['version_id'] = default_version_id

            if not vals.get('version_id'):
                version_id = self._version_id_from_context(
                    application_id=vals.get('application_id'),
                )
                if version_id:
                    vals['version_id'] = version_id

            if 'route_suffix' in vals:
                vals['route_suffix'] = self._route_path_from_input(
                    vals.get('route_suffix'),
                    application_id=vals.get('application_id'),
                    version_id=vals.get('version_id'),
                )

            prepared.append(vals)
        return super().create(prepared)

    @api.model
    def _normalize_route_suffix(self, suffix):
        """Return a canonical route suffix without leading or trailing slashes."""
        return (suffix or '').strip().strip('/')

    @api.model
    def _route_path_from_input(self, route_path, application_id=False, version_id=False):
        """Store only Route Path; tolerate a pasted /v1/route full path."""
        normalized = self._normalize_route_suffix(route_path)
        if not normalized:
            return normalized
        version = self.env['core.api.version'].browse(version_id).exists()
        parts = normalized.split('/')
        if version and parts and parts[0] == (version.code or '').strip('/'):
            parts = parts[1:]
        return '/'.join(parts).strip('/')

    def write(self, vals):
        vals = dict(vals)
        if 'route_suffix' not in vals:
            return super().write(vals)
        for endpoint in self:
            endpoint_vals = dict(vals)
            endpoint_vals['route_suffix'] = self._route_path_from_input(
                vals.get('route_suffix'),
                application_id=endpoint_vals.get('application_id') or endpoint.application_id.id,
                version_id=endpoint_vals.get('version_id') or endpoint.version_id.id,
            )
            super(CoreApiEndpoint, endpoint).write(endpoint_vals)
        return True

    def _parsed_methods(self):
        """Return the list of allowed HTTP methods for this route."""
        self.ensure_one()
        raw = (self.http_methods or 'GET').upper().replace(' ', '')
        return [m for m in raw.split(',') if m]

    def allows_method(self, method):
        """Return True when the given HTTP method is allowed on this route."""
        self.ensure_one()
        allowed = self._parsed_methods()
        return not allowed or (method or '').upper() in allowed

    @api.model
    def _endpoint_matches_request(self, endpoint, path, method):
        """Return True when the route pattern and HTTP method match the request."""
        normalized = (path or '').split('?')[0].rstrip('/') or '/'
        method = (method or 'GET').upper()
        pattern = (endpoint.route_pattern or '').rstrip('/') or '/'
        if normalized != pattern and not normalized.startswith(f'{pattern}/'):
            return False
        return endpoint.allows_method(method)

    @api.model
    def find_for_request(self, path, method, application=None):
        """Find the best matching active route for path, method, and application."""
        method = (method or 'GET').upper()
        app_domain = [('application_id', '=', application.id)] if application else []
        version_domain = [('version_id.active', '=', True)]

        inactive = self.sudo().search(
            version_domain + app_domain + [('route_active', '=', False)],
        )
        for endpoint in inactive:
            if self._endpoint_matches_request(endpoint, path, method):
                return endpoint, 'inactive'

        candidates = []
        for endpoint in self.sudo().search(version_domain + app_domain + [('route_active', '=', True)]):
            if self._endpoint_matches_request(endpoint, path, method):
                pattern = (endpoint.route_pattern or '').rstrip('/') or '/'
                candidates.append((len(pattern), endpoint))
        if not candidates:
            return self.browse(), 'missing'
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1], 'ok'

    def _parse_request_body(self, httprequest):
        """Parse JSON body from the incoming HTTP request."""
        raw = httprequest.get_data(as_text=True) or ''
        if not raw.strip():
            return {}
        if raw.strip().startswith(('{', '[')):
            try:
                return json.loads(raw)
            except json.JSONDecodeError as e:
                raise CoreApiInvalidBody('Invalid JSON body.') from e
        return raw

    def _server_action_context(self, application, httprequest):
        """Build the context dict passed to the linked server action."""
        self.ensure_one()
        ctx = {
            'core_api_application_id': application.id,
            'core_api_method': httprequest.method,
            'core_api_route': self.route_pattern,
            'core_api_endpoint_id': self.id,
            'core_api_endpoint_code': self.code,
            'core_api_version_id': self.version_id.id,
            'core_api_version_code': self.version_id.code,
            'core_api_body': self._parse_request_body(httprequest),
            'core_api_params': dict(httprequest.args),
        }
        action_model = self.action_id.model_id.model
        if action_model == 'core.api.application':
            ctx.update({
                'active_model': application._name,
                'active_id': application.id,
                'active_ids': application.ids,
            })
        else:
            ctx.update({
                'active_model': action_model,
                'active_id': False,
                'active_ids': [],
            })
        return ctx

    def _run_server_action(self, application, httprequest):
        """Execute the linked server action and return a JSON HTTP response."""
        self.ensure_one()
        if not self.action_id:
            raise ValidationError(_(
                'Gateway route "%s" has no Server Action configured.', self.name
            ))

        request.core_api_response = None
        ctx = self._server_action_context(application, httprequest)
        self.action_id.sudo().with_context(**ctx).run()

        response_data = getattr(request, 'core_api_response', None)
        status_code, payload = normalize_gateway_response(response_data)
        return make_json_response(payload, status_code)

    def _error_response(self, message, status=400):
        return api_error_response(message, status_code=status)

    def dispatch(self, application):
        """Validate access and run this route for the authenticated application."""
        self.ensure_one()
        try:
            if not self.route_active:
                raise AccessError(_('Gateway route "%s" is inactive.', self.name))
            if application:
                if self.application_id != application:
                    raise AccessError(_(
                        'Gateway route "%(route)s" does not belong to application "%(app)s".',
                        route=self.name, app=application.name,
                    ))
                application.check_api_access(self.code, version_id=self.version_id.id)
            return self._run_server_action(application, request.httprequest)
        except CoreApiBadRequest as e:
            return self._error_response(str(e), 400)
        except BadRequest as e:
            return self._error_response(e.description or str(e), 400)
        except ValidationError as e:
            return self._error_response(str(e), 400)
        except AccessError as e:
            return self._error_response(str(e), 403)
        except ValueError as e:
            _logger.exception('Core API server action failed on route %s', self.route_pattern)
            message = str(e)
            if 'while evaluating' in message:
                message = message.split('while evaluating', 1)[0].strip().strip("'")
            return self._error_response(message or 'Server action failed.', 500)
        except Exception as e:
            _logger.exception('Core API route %s failed', self.route_pattern)
            return self._error_response(str(e) or 'Internal server error.', 500)

    @api.model
    def dispatch_request(self, path, application):
        """Entry point from the HTTP gateway controller."""
        endpoint, status = self.find_for_request(
            path, request.httprequest.method, application=application,
        )
        if status == 'inactive':
            raise Forbidden(_('This gateway route is inactive.'))
        if not endpoint:
            raise NotFound(f'No gateway route configured for: {path}.')
        return endpoint.dispatch(application)
