# Part of T4 Core API. See LICENSE file for full copyright and licensing details.

import re

from werkzeug.exceptions import HTTPException, TooManyRequests, Unauthorized

from odoo import models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.http import request

from odoo.addons.t4_coreapi.utils.response import api_error_response
from odoo.addons.t4_coreapi.utils.routing import is_auth_token_path, is_gateway_path
from odoo.addons.t4_coreapi.utils.security import get_client_ip


class IrHttp(models.AbstractModel):
    _inherit = 'ir.http'

    @classmethod
    def _is_core_api_request(cls):
        """Return True when the current request targets a Core API HTTP route."""
        path = request.httprequest.path or ''
        if is_auth_token_path(path):
            return True
        if not is_gateway_path(path):
            return False
        service_code = path.strip('/').split('/')[0]
        if not service_code:
            return False
        try:
            if request.db and getattr(request, 'env', None):
                return bool(request.env['core.api.application'].sudo().search_count([
                    ('service_code', '=', service_code),
                    ('state', '=', 'active'),
                ]))
        except Exception:
            return False
        return False

    @classmethod
    def _extract_bearer_token(cls):
        """Read the bearer token value from the Authorization header."""
        header = request.httprequest.headers.get('Authorization')
        if header and (m := re.match(r'^bearer\s+(.+)$', header, re.IGNORECASE)):
            return m.group(1).strip()
        return None

    @classmethod
    def _auth_method_core_api(cls):
        """Validate bearer token, IP, and rate limits before API controllers run."""
        token = cls._extract_bearer_token()
        if not token:
            raise Unauthorized(
                'Missing Authorization: Bearer <token>.',
                www_authenticate='Bearer realm="Core API"',
            )

        application, token_rec = request.env['core.api.token'].sudo().authenticate(token)
        if not application:
            raise Unauthorized(
                'Invalid or expired access token.',
                www_authenticate='Bearer realm="Core API"',
            )

        ip = get_client_ip()
        try:
            application.check_ip_allowed(ip)
            application.check_api_rate_limit()
        except Exception as e:
            if 'rate limit' in str(e).lower():
                raise TooManyRequests(str(e)) from e
            raise

        request.update_env(user=request.env.ref('base.public_user').id)
        request.update_context(
            core_api_application_id=application.id,
            core_api_token_id=token_rec.id,
            core_api_client_id=application.client_id,
            core_api_client_instance_id=token_rec.client_instance_id or False,
        )
        request.session.can_save = False

    @classmethod
    def _auth_method_validate_core_api(cls):
        """Alias auth method. Same gatekeeper as core_api."""
        cls._auth_method_core_api()

    @classmethod
    def _handle_error(cls, exception):
        """Return JSON error bodies for Core API gateway routes."""
        if cls._is_core_api_request():
            return cls._handle_core_api_error(exception)
        return super()._handle_error(exception)

    @classmethod
    def _handle_core_api_error(cls, exception):
        """Map exceptions to standard Core API JSON error responses."""
        if isinstance(exception, HTTPException):
            message = exception.description or str(exception)
            status_code = exception.code or 500
            return api_error_response(message, status_code=status_code)

        if isinstance(exception, AccessError):
            return api_error_response(exception.args[0], status_code=403)

        if isinstance(exception, (UserError, ValidationError)):
            return api_error_response(exception.args[0], status_code=400)

        return api_error_response('Internal server error.', status_code=500)
