# -*- coding: utf-8 -*-
import json
import time

from werkzeug.exceptions import BadRequest, TooManyRequests

from odoo import http
from odoo.exceptions import AccessDenied, AccessError, ValidationError
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

    def _authenticate_odoo_user(self, login, password):
        credential = {
            'type': 'password',
            'login': login,
            'password': password,
        }
        auth_info = request.env['res.users'].authenticate(
            credential,
            {
                'interactive': True,
                'HTTP_HOST': request.httprequest.environ.get('HTTP_HOST'),
                'REMOTE_ADDR': request.httprequest.environ.get('REMOTE_ADDR'),
            },
        )
        user = request.env['res.users'].sudo().browse(auth_info['uid']).exists()
        if not user or not user.active:
            raise AccessDenied()
        if user.share:
            raise AccessError('NSP Mobile requires an internal Odoo User.')
        if auth_info.get('mfa') != 'skip' and user._mfa_url():
            raise AccessError(
                'This Odoo User requires multi-factor authentication. '
                'NSP Mobile cannot issue a token before the Odoo MFA step is completed.'
            )
        return user

    def _business_user(self, odoo_user):
        # Every internal Odoo User must resolve to exactly one NSP User profile.
        odoo_user.sudo()._ensure_nsp_user_profile()
        mapped_user = request.env['nsp.user'].sudo().search([
            ('odoo_user_id', '=', odoo_user.id),
            ('active', '=', True),
        ])
        if len(mapped_user) != 1:
            raise AccessError(
                'This Odoo User must be linked to exactly one active NSP User.'
            )

        user_env = request.env(user=odoo_user.id, su=False)
        business_user = user_env['nsp.user'].search([
            ('id', '=', mapped_user.id),
            ('active', '=', True),
        ])
        if len(business_user) != 1:
            raise AccessError(
                'This Odoo User has no permission to access its NSP User profile.'
            )
        return business_user

    def _token_payload(self, result, application, odoo_user, business_user, device, session):
        data = {
            'access_token': result['access_token'],
            'refresh_token': result['refresh_token'],
            'token_type': 'Bearer',
            'session_uid': session.session_uid,
            'device_uid': device.device_uid,
            'user': {
                'id': business_user.id,
                'odoo_user_id': odoo_user.id,
                'login': odoo_user.login,
                'name': business_user.name,
                'email': business_user.email or None,
                'phone': business_user.phone or None,
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
            allowed_device_fields = {
                'device_uid', 'platform', 'device_name', 'app_version',
                'push_provider', 'push_token', 'push_enabled',
            }
            unsupported_device = sorted(set(device_data) - allowed_device_fields)
            if unsupported_device:
                raise BadRequest(
                    'Unsupported device field(s): %s.' % ', '.join(unsupported_device)
                )
            if not login or not password:
                raise BadRequest('login and password are required.')
            if not str(device_data.get('device_uid') or '').strip():
                raise BadRequest('device.device_uid is required.')

            odoo_user = self._authenticate_odoo_user(login, password)
            business_user = self._business_user(odoo_user)
            device = request.env['nsp.mobile.device'].sudo().register_or_update(
                business_user.sudo(), device_data
            )
            session = request.env['nsp.mobile.session'].sudo().open_session(
                business_user.sudo(), device,
                ip=get_client_ip(),
                user_agent=request.httprequest.headers.get('User-Agent'),
            )
            result = request.env['core.api.token'].sudo().issue_for_subject(
                application,
                token_kind='mobile',
                subject_model='res.users',
                subject_record_id=odoo_user.id,
                session_uid=session.session_uid,
                device_uid=device.device_uid,
            )
            self._log(self.LOGIN_PATH, application, 200, True, started, token=result['access_token_rec'])
            return api_success_response(
                'OK',
                data=self._token_payload(
                    result, application, odoo_user, business_user, device, session
                ),
            )
        except BadRequest as exc:
            self._log(self.LOGIN_PATH, application, 400, False, started, exc.description)
            return api_error_response(exc.description, status_code=400)
        except AccessDenied:
            self._log(self.LOGIN_PATH, application, 401, False, started, 'Invalid Odoo credentials.')
            return api_error_response('Invalid login or password.', status_code=401)
        except AccessError as exc:
            message = exc.args[0] if exc.args else str(exc)
            self._log(self.LOGIN_PATH, application, 403, False, started, message)
            return api_error_response(message, status_code=403)
        except ValidationError as exc:
            message = exc.args[0] if exc.args else str(exc)
            self._log(self.LOGIN_PATH, application, 400, False, started, message)
            return api_error_response(message, status_code=400)
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
        try:
            body = self._json_object()
            if set(body) - {'refresh_token'}:
                raise BadRequest('Only refresh_token is supported.')
            plaintext = str(body.get('refresh_token') or '')
            if not plaintext:
                raise BadRequest('refresh_token is required.')

            resolved_app, source_token = request.env['core.api.token'].sudo().consume_refresh_token(
                plaintext, token_kind='mobile'
            )
            if (
                not resolved_app
                or resolved_app != application
                or source_token.token_kind != 'mobile'
                or source_token.subject_model != 'res.users'
            ):
                raise AccessDenied()

            odoo_user = request.env['res.users'].sudo().browse(
                source_token.subject_record_id
            ).exists()
            if not odoo_user or not odoo_user.active:
                raise AccessDenied()
            business_user = self._business_user(odoo_user)
            session = request.env['nsp.mobile.session'].sudo().search([
                ('session_uid', '=', source_token.session_uid),
                ('user_id', '=', business_user.id),
                ('state', '=', 'active'),
            ], limit=1)
            device = request.env['nsp.mobile.device'].sudo().search([
                ('device_uid', '=', source_token.device_uid),
                ('user_id', '=', business_user.id),
                ('active', '=', True),
            ], limit=1)
            if not session or session.user_id.id != business_user.id or not device:
                raise AccessDenied()

            session.touch(ip=get_client_ip())
            result = request.env['core.api.token'].sudo().issue_for_subject(
                application,
                token_kind='mobile',
                subject_model='res.users',
                subject_record_id=odoo_user.id,
                session_uid=session.session_uid,
                device_uid=device.device_uid,
            )
            self._log(self.REFRESH_PATH, application, 200, True, started, token=result['access_token_rec'])
            return api_success_response(
                'OK',
                data=self._token_payload(
                    result, application, odoo_user, business_user, device, session
                ),
            )
        except BadRequest as exc:
            self._log(self.REFRESH_PATH, application, 400, False, started, exc.description)
            return api_error_response(exc.description, status_code=400)
        except (AccessDenied, AccessError):
            self._log(self.REFRESH_PATH, application, 401, False, started, 'Mobile session revoked.')
            return api_error_response('Invalid or expired refresh token.', status_code=401)
        except Exception as exc:
            self._log(self.REFRESH_PATH, application, 500, False, started, str(exc))
            return api_error_response('Internal server error.', status_code=500)

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
                'active': False,
                'refresh_token_index': False,
                'refresh_token_hash': False,
                'refresh_expiration_date': False,
            })
        self._log(self.LOGOUT_PATH, application, 200, True, started, token=token)
        return api_success_response('OK', data={})
