# Part of T4 Core API. See LICENSE file for full copyright and licensing details.

import json
import logging
import time

from werkzeug.exceptions import BadRequest, TooManyRequests, Unauthorized

from odoo import http
from odoo.http import request

from odoo.addons.t4_coreapi.utils.response import auth_success_response
from odoo.addons.t4_coreapi.utils.routing import AUTH_REFRESH_PATH, AUTH_TOKEN_PATH
from odoo.addons.t4_coreapi.utils.security import check_ip_auth_rate_limit, get_client_ip

_logger = logging.getLogger(__name__)


class CoreApiAuthController(http.Controller):
    """Initial shared-credential login plus rotating refresh tokens."""

    def _log_auth(self, application, route, ip, ua, status_code, success, duration_ms=0, error=None, token=None):
        request.env['core.api.log'].sudo().log_event(
            event_type='auth', route=route, method='POST', ip_address=ip,
            status_code=status_code, success=success, application=application,
            token=token, duration_ms=duration_ms, error_message=error, user_agent=ua,
        )

    def _parse_json_object(self):
        try:
            raw = request.httprequest.get_data(as_text=True) or ''
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            raise BadRequest('Invalid JSON body.') from None
        if not isinstance(data, dict):
            raise BadRequest('Request body must be a JSON object.')
        return data

    def _require_exact_fields(self, data, allowed):
        unsupported = sorted(set(data) - set(allowed))
        if unsupported:
            raise BadRequest(f'Unsupported field(s): {", ".join(unsupported)}.')

    @http.route(AUTH_TOKEN_PATH, type='http', auth='none', methods=['POST'], csrf=False, save_session=False)
    def issue_token(self, **kw):
        """First login: client sends only client_id and client_secret."""
        try:
            check_ip_auth_rate_limit(request.env, get_client_ip())
        except Exception as exc:
            raise TooManyRequests(str(exc)) from exc

        data = self._parse_json_object()
        self._require_exact_fields(data, {'client_id', 'client_secret'})
        client_id = (data.get('client_id') or '').strip()
        client_secret = data.get('client_secret') or ''
        if not client_id:
            raise BadRequest('client_id is required.')
        if not client_secret:
            raise BadRequest('client_secret is required.')

        route = AUTH_TOKEN_PATH
        t0 = time.time()
        ip = get_client_ip()
        ua = request.httprequest.headers.get('User-Agent')
        Application = request.env['core.api.application'].sudo()
        candidate = Application.search([('client_id', '=', client_id)], limit=1)
        if candidate:
            candidate.check_ip_allowed(ip)

        application, auth_error = Application.authenticate_client_with_reason(client_id, client_secret, ip_address=ip)
        if not application:
            self._log_auth(candidate, route, ip, ua, 401, False, (time.time() - t0) * 1000, auth_error)
            raise Unauthorized(auth_error)

        result = request.env['core.api.token'].sudo().issue_for_application(application)
        self._log_auth(application, route, ip, ua, 200, True, (time.time() - t0) * 1000, token=result['access_token_rec'])
        return auth_success_response(result, application)

    @http.route(AUTH_REFRESH_PATH, type='http', auth='none', methods=['POST'], csrf=False, save_session=False)
    def refresh_token(self, **kw):
        """Rotate a refresh token without resending shared client credentials."""
        try:
            check_ip_auth_rate_limit(request.env, get_client_ip())
        except Exception as exc:
            raise TooManyRequests(str(exc)) from exc

        data = self._parse_json_object()
        self._require_exact_fields(data, {'refresh_token'})
        plaintext = data.get('refresh_token') or ''
        if not plaintext:
            raise BadRequest('refresh_token is required.')

        route = AUTH_REFRESH_PATH
        t0 = time.time()
        ip = get_client_ip()
        ua = request.httprequest.headers.get('User-Agent')
        Token = request.env['core.api.token'].sudo()
        application, source_token = Token.consume_refresh_token(plaintext, token_kind='application')
        if not application:
            self._log_auth(False, route, ip, ua, 401, False, (time.time() - t0) * 1000, 'Invalid or expired refresh token.')
            raise Unauthorized('Invalid or expired refresh token.')
        application.check_ip_allowed(ip)
        result = Token.issue_for_application(application)
        self._log_auth(application, route, ip, ua, 200, True, (time.time() - t0) * 1000, token=result['access_token_rec'])
        return auth_success_response(result, application)
