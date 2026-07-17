# Part of T4 Core API. See LICENSE file for full copyright and licensing details.

import json
import logging
import time

from werkzeug.exceptions import BadRequest, TooManyRequests, Unauthorized

from odoo import http
from odoo.http import request

from odoo.addons.t4_coreapi.utils.response import auth_success_response
from odoo.addons.t4_coreapi.utils.routing import AUTH_TOKEN_PATH
from odoo.addons.t4_coreapi.utils.security import (
    check_ip_auth_rate_limit,
    get_client_ip,
)

_logger = logging.getLogger(__name__)


class CoreApiAuthController(http.Controller):
    """OAuth2-style token endpoint (client credentials + refresh token)."""

    def _log_auth(self, application, route, ip, ua, status_code, success, duration_ms=0, error=None):
        """Write an authentication attempt to core.api.log."""
        request.env['core.api.log'].sudo().log_event(
            event_type='auth',
            route=route,
            method='POST',
            ip_address=ip,
            status_code=status_code,
            success=success,
            application=application,
            duration_ms=duration_ms,
            error_message=error,
            user_agent=ua,
        )

    def _parse_request_data(self, kw):
        """Parse JSON or form body from the incoming token request."""
        try:
            raw = request.httprequest.get_data(as_text=True) or ''
            if request.httprequest.content_type and 'json' in request.httprequest.content_type:
                return json.loads(raw) if raw else {}
            return dict(request.httprequest.form) or (json.loads(raw) if raw else {})
        except json.JSONDecodeError:
            raise BadRequest('Invalid JSON body.') from None

    def _issue_token_impl(self, data, kw):
        """Handle client_credentials and refresh_token grants."""
        auth_route = AUTH_TOKEN_PATH
        t0 = time.time()
        ip = get_client_ip()
        ua = request.httprequest.headers.get('User-Agent')
        application = request.env['core.api.application']
        grant_type = (data.get('grant_type') or kw.get('grant_type') or 'client_credentials').strip()
        success_message = 'Authentication successful.'

        if grant_type == 'client_credentials':
            client_id = (data.get('client_id') or kw.get('client_id') or '').strip()
            client_secret = data.get('client_secret') or kw.get('client_secret') or ''

            candidate = request.env['core.api.application'].sudo().search([
                ('client_id', '=', client_id),
            ], limit=1) if client_id else request.env['core.api.application']

            if candidate:
                try:
                    candidate.check_ip_allowed(ip)
                    candidate.check_auth_rate_limit()
                except Exception as e:
                    duration = (time.time() - t0) * 1000
                    status = 429 if 'rate limit' in str(e).lower() else 403
                    self._log_auth(candidate, auth_route, ip, ua, status, False, duration, str(e))
                    if status == 429:
                        raise TooManyRequests(str(e)) from e
                    raise

            application, auth_error = request.env['core.api.application'].sudo().authenticate_client_with_reason(
                client_id, client_secret, ip_address=ip,
            )
            if not application:
                duration = (time.time() - t0) * 1000
                self._log_auth(candidate, auth_route, ip, ua, 401, False, duration, auth_error)
                _logger.warning(
                    'Core API auth failed for client_id=%s from %s: %s',
                    client_id or '<empty>', ip, auth_error,
                )
                raise Unauthorized(auth_error)

            token_result = request.env['core.api.token'].sudo().issue_for_application(application, revoke_existing=False)

        elif grant_type == 'refresh_token':
            refresh_token = (data.get('refresh_token') or kw.get('refresh_token') or '').strip()
            if not refresh_token:
                raise BadRequest('refresh_token is required for grant_type refresh_token.')

            token_result = request.env['core.api.token'].sudo().refresh_for_application(refresh_token)
            if not token_result:
                duration = (time.time() - t0) * 1000
                error = 'Invalid or expired refresh_token. Re-authenticate with client credentials.'
                self._log_auth(application, auth_route, ip, ua, 401, False, duration, error)
                raise Unauthorized(error)

            application = token_result['access_token_rec'].application_id
            success_message = 'Token refreshed successfully.'
            try:
                application.check_ip_allowed(ip)
                application.check_auth_rate_limit()
            except Exception as e:
                duration = (time.time() - t0) * 1000
                status = 429 if 'rate limit' in str(e).lower() else 403
                self._log_auth(application, auth_route, ip, ua, status, False, duration, str(e))
                if status == 429:
                    raise TooManyRequests(str(e)) from e
                raise

        else:
            raise BadRequest(
                f'Unsupported grant_type "{grant_type}". Use client_credentials or refresh_token.'
            )

        duration = (time.time() - t0) * 1000
        self._log_auth(application, auth_route, ip, ua, 200, True, duration)
        # Authentication rate limiting must reject only the excessive token
        # request. It must never revoke token pairs already used by other
        # clients sharing the same Core API Application.
        return auth_success_response(success_message, token_result, application)

    @http.route(
        AUTH_TOKEN_PATH,
        type='http',
        auth='none',
        methods=['POST'],
        csrf=False,
        save_session=False,
    )
    def issue_token(self, **kw):
        """Exchange client credentials or a refresh token for new API tokens."""
        try:
            check_ip_auth_rate_limit(request.env, get_client_ip())
        except Exception as e:
            raise TooManyRequests(str(e)) from e

        data = self._parse_request_data(kw)
        return self._issue_token_impl(data, kw)

