# -*- coding: utf-8 -*-
import json
import time

from werkzeug.exceptions import BadRequest, TooManyRequests, Unauthorized

from odoo import http, fields
from odoo.exceptions import AccessError, ValidationError
from odoo.http import request

from odoo.addons.t4_coreapi.utils.response import api_error_response, api_success_response
from odoo.addons.t4_coreapi.utils.security import check_ip_auth_rate_limit, get_client_ip


class NspMobileAuthController(http.Controller):
    LOGIN_PATH = '/v1/mobile/auth/login'
    REFRESH_PATH = '/v1/mobile/auth/refresh'
    LOGOUT_PATH = '/v1/mobile/auth/logout'

    def _application(self):
        return request.env.ref('nsp_mobile.core_api_application_nsp_mobile').sudo()

    def _json_object(self):
        try:
            raw = request.httprequest.get_data(as_text=True) or ''
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            raise BadRequest('Invalid JSON body.') from None
        if not isinstance(data, dict):
            raise BadRequest('Request body must be a JSON object.')
        return data

    def _log(self, route, application, status, success, started, error=False, token=False):
        request.env['core.api.log'].sudo().log_event(
            event_type='auth', route=route, method='POST', ip_address=get_client_ip(),
            status_code=status, success=success, application=application, token=token,
            duration_ms=(time.time() - started) * 1000,
            error_message=error or False,
            user_agent=request.httprequest.headers.get('User-Agent'),
        )

    def _token_payload(self, result, application, user, device, session):
        data = {
            'access_token': result['access_token'],
            'refresh_token': result['refresh_token'],
            'token_type': 'Bearer',
            'session_uid': session.session_uid,
            'device_uid': device.device_uid,
            'user': {
                'id': user.id,
                'name': user.name,
                'email': user.email or None,
                'phone': user.phone or None,
            },
        }
        if application.token_ttl_hours:
            data['expires_in'] = application.token_ttl_hours * 3600
        if application.refresh_token_ttl_days:
            data['refresh_expires_in'] = application.refresh_token_ttl_days * 86400
        return data

    @http.route(LOGIN_PATH, type='http', auth='none', methods=['POST'], csrf=False, save_session=False)
    def login(self, **kw):
        started = time.time()
        application = self._application()
        application.check_ip_allowed(get_client_ip())
        try:
            check_ip_auth_rate_limit(request.env, get_client_ip())
        except Exception as exc:
            raise TooManyRequests(str(exc)) from exc
        try:
            body = self._json_object()
            unsupported = sorted(set(body) - {'login', 'password', 'device'})
            if unsupported:
                raise BadRequest('Unsupported field(s): %s.' % ', '.join(unsupported))
            login = str(body.get('login') or '').strip()
            password = str(body.get('password') or '')
            device_data = body.get('device') or {}
            if not isinstance(device_data, dict):
                raise BadRequest('device must be a JSON object.')
            if not login or not password:
                raise BadRequest('login and password are required.')
            if not str(device_data.get('device_uid') or '').strip():
                raise BadRequest('device.device_uid is required.')

            user = request.env['nsp.user'].sudo().authenticate_mobile(login, password)
            if not user:
                self._log(self.LOGIN_PATH, application, 401, False, started, 'Invalid mobile credentials.')
                return api_error_response('Invalid login or password.', status_code=401)

            device = request.env['nsp.mobile.device'].sudo().register_or_update(user, device_data)
            session = request.env['nsp.mobile.session'].sudo().open_session(
                user, device, ip=get_client_ip(), user_agent=request.httprequest.headers.get('User-Agent')
            )
            result = request.env['core.api.token'].sudo().issue_for_subject(
                application,
                token_kind='mobile',
                subject_model='nsp.user',
                subject_record_id=user.id,
                session_uid=session.session_uid,
                device_uid=device.device_uid,
            )
            self._log(self.LOGIN_PATH, application, 200, True, started, token=result['access_token_rec'])
            return api_success_response('OK', data=self._token_payload(result, application, user, device, session))
        except BadRequest as exc:
            self._log(self.LOGIN_PATH, application, 400, False, started, exc.description)
            return api_error_response(exc.description, status_code=400)
        except (ValidationError, AccessError) as exc:
            message = exc.args[0] if exc.args else str(exc)
            status = 403 if isinstance(exc, AccessError) else 400
            self._log(self.LOGIN_PATH, application, status, False, started, message)
            return api_error_response(message, status_code=status)
        except Exception as exc:
            self._log(self.LOGIN_PATH, application, 500, False, started, str(exc))
            return api_error_response('Internal server error.', status_code=500)

    @http.route(REFRESH_PATH, type='http', auth='none', methods=['POST'], csrf=False, save_session=False)
    def refresh(self, **kw):
        started = time.time()
        application = self._application()
        application.check_ip_allowed(get_client_ip())
        try:
            check_ip_auth_rate_limit(request.env, get_client_ip())
        except Exception as exc:
            raise TooManyRequests(str(exc)) from exc
        body = self._json_object()
        if set(body) - {'refresh_token'}:
            return api_error_response('Only refresh_token is supported.', status_code=400)
        plaintext = str(body.get('refresh_token') or '')
        if not plaintext:
            return api_error_response('refresh_token is required.', status_code=400)
        resolved_app, source_token = request.env['core.api.token'].sudo().consume_refresh_token(plaintext, token_kind='mobile')
        if (
            not resolved_app or resolved_app != application or source_token.token_kind != 'mobile'
            or source_token.subject_model != 'nsp.user'
        ):
            self._log(self.REFRESH_PATH, application, 401, False, started, 'Invalid or expired Mobile refresh token.')
            return api_error_response('Invalid or expired refresh token.', status_code=401)
        session = request.env['nsp.mobile.session'].sudo().search([
            ('session_uid', '=', source_token.session_uid), ('state', '=', 'active')
        ], limit=1)
        user = request.env['nsp.user'].sudo().browse(source_token.subject_record_id).exists()
        device = request.env['nsp.mobile.device'].sudo().search([
            ('device_uid', '=', source_token.device_uid), ('user_id', '=', user.id), ('active', '=', True)
        ], limit=1)
        if not session or session.user_id != user or not user or not user.active or not user.mobile_enabled or not device:
            self._log(self.REFRESH_PATH, application, 401, False, started, 'Mobile session revoked.')
            return api_error_response('Mobile session is no longer active.', status_code=401)
        session.touch(ip=get_client_ip())
        result = request.env['core.api.token'].sudo().issue_for_subject(
            application,
            token_kind='mobile', subject_model='nsp.user', subject_record_id=user.id,
            session_uid=session.session_uid, device_uid=device.device_uid,
        )
        self._log(self.REFRESH_PATH, application, 200, True, started, token=result['access_token_rec'])
        return api_success_response('OK', data=self._token_payload(result, application, user, device, session))

    @http.route(LOGOUT_PATH, type='http', auth='none', methods=['POST'], csrf=False, save_session=False)
    def logout(self, **kw):
        started = time.time()
        application = self._application()
        header = request.httprequest.headers.get('Authorization') or ''
        parts = header.split(None, 1)
        plaintext = parts[1].strip() if len(parts) == 2 and parts[0].lower() == 'bearer' else ''
        resolved_app, token = request.env['core.api.token'].sudo().authenticate(plaintext)
        if not resolved_app or resolved_app != application or token.token_kind != 'mobile':
            self._log(self.LOGOUT_PATH, application, 401, False, started, 'Invalid Mobile access token.')
            return api_error_response('Invalid or expired access token.', status_code=401)
        session = request.env['nsp.mobile.session'].sudo().search([
            ('session_uid', '=', token.session_uid), ('state', '=', 'active')
        ], limit=1)
        if session:
            session.revoke()
        else:
            token.write({
                'active': False, 'refresh_token_index': False, 'refresh_token_hash': False,
                'refresh_expiration_date': False,
            })
        self._log(self.LOGOUT_PATH, application, 200, True, started, token=token)
        return api_success_response('OK', data={})
