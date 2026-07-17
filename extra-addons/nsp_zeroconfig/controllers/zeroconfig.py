# -*- coding: utf-8 -*-
"""Secure IPv6 Zeroconfig discovery and direct Controller Code bootstrap."""
import hashlib
import hmac
import json
import logging
import time

from odoo import fields, http, SUPERUSER_ID, _
from odoo.http import request

from odoo.addons.t4_coreapi.utils.response import make_json_response, success_body, error_body
from odoo.addons.t4_coreapi.utils.security import get_client_ip
from odoo.addons.nsp_zeroconfig.utils.server import discovery_status

_logger = logging.getLogger(__name__)


def _response(payload, status=200):
    return make_json_response(payload or {}, status)


def _success(message, data=None, **extra):
    body = success_body(message, data=data, **extra)
    body.update({'ok': True, 'success': True})
    return _response(body, 200)


def _error(message, status=400, **extra):
    body = error_body(message, **extra)
    body.update({'ok': False, 'success': False, 'error': message})
    return _response(body, status)


def _read_payload():
    raw = request.httprequest.get_data(as_text=True) or ''
    if raw.strip():
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    try:
        form = dict(request.httprequest.form or {})
        if form:
            return form
    except Exception:
        pass
    try:
        return dict(request.params or {})
    except Exception:
        return {}


def _trim(value, limit=256):
    return str(value or '').strip()[:limit]


def _public_base_url():
    try:
        return (request.env['ir.config_parameter'].sudo().get_param('web.base.url') or '').rstrip('/')
    except Exception:
        return ''


def _branch_timezone(controller):
    try:
        if controller.branch_id and controller.branch_id.timezone:
            return controller.branch_id.timezone
    except Exception:
        pass
    try:
        if controller.parent_id and controller.parent_id.branch_id and controller.parent_id.branch_id.timezone:
            return controller.parent_id.branch_id.timezone
    except Exception:
        pass
    return request.env['ir.config_parameter'].sudo().get_param(
        'nsp_zeroconfig.default_timezone', 'Asia/Ho_Chi_Minh'
    )


def _canonical_bootstrap(controller_code, timestamp, nonce):
    return '%s|%s|%s' % (controller_code, timestamp, nonce)


def _verify_bootstrap_signature(controller_code, timestamp, nonce, signature):
    params = request.env['ir.config_parameter'].sudo()
    secret = (params.get_param('nsp_zeroconfig.discovery_secret') or '').strip()
    if not secret:
        return False, 'Discovery Secret Key is not configured on the Edge Server.'
    try:
        timestamp_int = int(str(timestamp).strip())
    except Exception:
        return False, 'timestamp must be a Unix timestamp in seconds.'
    max_skew = 180
    try:
        configured = int(params.get_param('nsp_zeroconfig.bootstrap_max_clock_skew_sec', '180') or '180')
        max_skew = max(30, min(600, configured))
    except Exception:
        pass
    if abs(int(time.time()) - timestamp_int) > max_skew:
        return False, 'Bootstrap timestamp is outside the allowed clock window.'
    if not nonce or len(nonce) < 16:
        return False, 'nonce must contain at least 16 characters.'
    canonical = _canonical_bootstrap(controller_code, timestamp_int, nonce)
    expected = hmac.new(secret.encode('utf-8'), canonical.encode('utf-8'), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected.lower(), (signature or '').strip().lower()):
        return False, 'Invalid Controller bootstrap signature.'
    return True, ''


def _ensure_controller_application(controller):
    application = controller.core_api_application_id.sudo().exists()
    Application = request.env['core.api.application'].sudo()
    if not application:
        service_code = (controller.controller_id or '').strip()
        application = Application.with_context(active_test=False).search([
            ('service_code', '=', service_code),
        ], limit=1)
        if application and application.state != 'active':
            raise ValueError(_('The Core API Application for Controller %s is inactive.') % service_code)
        if not application:
            application = Application.create({
                'name': '%s / NSP Controller' % (controller.controller_name or service_code),
                'application_kind': 'controller',
                'service_code': service_code,
                'state': 'active',
                'notes': _('Automatically assigned when Controller Code %s first bootstrapped.') % service_code,
            })
        elif application.application_kind != 'controller':
            application.write({'application_kind': 'controller'})
        controller.write({'core_api_application_id': application.id})
    if application.credentials_pending:
        application._clear_pending_secret()
        application.write({'credentials_pending': False})
    if application.state != 'active':
        raise ValueError(_('The assigned Core API Application is inactive.'))
    # Idempotently ensure all runtime routes exist. This does not create a Controller record.
    controller._generate_gatekeeper_routes(application)
    return application


def _bootstrap_payload(application, controller, token_result):
    service_code = (application.service_code or '').strip('/')
    timezone = _branch_timezone(controller)
    access_rec = token_result.get('access_token_rec')
    refresh_rec = token_result.get('refresh_token_rec')
    controller_config = {
        'id': controller.id,
        'client_id': application.client_id,
        'service_code': service_code,
        'server_code': service_code,
        'controller_code': controller.controller_id,
        'controller_id': controller.controller_id,
        'controller_name': controller.controller_name,
        'timezone': timezone,
        'branch_timezone': timezone,
        'parent_controller_code': controller.parent_id.controller_id if controller.parent_id else False,
    }
    return {
        'bootstrap_method': 'controller_code_hmac_sha256',
        'server_base_url': _public_base_url(),
        'auth_type': 'core_api_bearer',
        'token_endpoint': '/auth/token',
        'gateway_base': '/%s/v1' % service_code,
        'service_code': service_code,
        'server_code': service_code,
        'controller_code': controller.controller_id,
        'application': {
            'id': application.id,
            'name': application.name,
            'client_id': application.client_id,
            'service_code': service_code,
            'server_code': service_code,
            'gateway_base': '/%s/v1' % service_code,
        },
        'controller': controller_config,
        'controller_config': controller_config,
        'api_token': {
            'token': token_result.get('access_token'),
            'expires_in': application.token_ttl_hours * 3600 if application.token_ttl_hours else False,
            'expires_at': access_rec.expiration_date if access_rec else False,
        },
        'refresh_token': {
            'token': token_result.get('refresh_token'),
            'expires_in': application.refresh_token_ttl_hours * 3600 if application.refresh_token_ttl_hours else False,
            'expires_at': refresh_rec.expiration_date if refresh_rec else False,
        },
        'routes': [{
            'code': endpoint.code,
            'route_suffix': endpoint.route_suffix,
            'route_pattern': endpoint.route_pattern,
            'method': endpoint.method,
        } for endpoint in application.endpoint_ids.filtered(lambda endpoint: endpoint.route_active)],
    }


class NspZeroconfigController(http.Controller):

    @http.route(
        '/nsp/zeroconfig/controller/bootstrap',
        type='http', auth='none', methods=['POST'], csrf=False, cors='*', save_session=False,
    )
    def controller_bootstrap(self, **kwargs):
        """Authenticate an existing Controller Code and issue runtime bearer tokens.

        The Controller record must already exist in NSP Gatekeeper. There is no
        request/approval/polling workflow. The request is authenticated by an
        HMAC-SHA256 signature created from Controller Code, Unix timestamp and nonce.
        """
        try:
            request.update_env(user=SUPERUSER_ID)
        except Exception:
            pass

        data = _read_payload()
        controller_code = _trim(data.get('controller_code') or data.get('controller_id'), 128)
        timestamp = data.get('timestamp')
        nonce = _trim(data.get('nonce'), 160)
        signature = _trim(data.get('signature'), 128)
        controller_url = _trim(data.get('controller_url') or data.get('url'), 512)
        ip_address = get_client_ip()
        user_agent = request.httprequest.headers.get('User-Agent')

        if not controller_code:
            return _error('controller_code is required.', 400)
        verified, verify_error = _verify_bootstrap_signature(
            controller_code, timestamp, nonce, signature,
        )
        if not verified:
            request.env['core.api.log'].sudo().log_event(
                event_type='auth', route='/nsp/zeroconfig/controller/bootstrap', method='POST',
                ip_address=ip_address, status_code=401, success=False,
                error_message=verify_error, user_agent=user_agent,
            )
            return _error(verify_error, 401)

        Controller = request.env['nsp.controller'].sudo().with_context(active_test=False)
        controller = Controller.search([
            ('controller_id', '=', controller_code),
            ('node_type', '=', 'controller'),
        ], limit=1)
        if not controller:
            return _error('Controller Code was not found on this Edge Server.', 404, controller_code=controller_code)
        if not controller.active or controller.status in ('block', 'revoked'):
            return _error('Controller is inactive, blocked or revoked.', 403, controller_code=controller_code)

        try:
            application = _ensure_controller_application(controller)
        except Exception as exc:
            _logger.exception('Cannot prepare Core API Application for Controller %s', controller_code)
            return _error(str(exc), 409, controller_code=controller_code)

        values = {
            'status': 'online',
            'connected': True,
            'timestamp': fields.Datetime.now(),
            'last_error': False,
        }
        if controller_url:
            values['url'] = controller_url
        controller.write(values)

        shared_application = bool(request.env['nsp.controller'].sudo().search_count([
            ('core_api_application_id', '=', application.id),
            ('id', '!=', controller.id),
        ]))
        token_result = request.env['core.api.token'].sudo().issue_for_application(
            application, revoke_existing=not shared_application,
        )
        request.env['core.api.log'].sudo().log_event(
            event_type='auth', route='/nsp/zeroconfig/controller/bootstrap', method='POST',
            ip_address=ip_address, status_code=200, success=True,
            application=application, user_agent=user_agent,
        )
        return _success(
            'Controller Code authenticated. Runtime configuration and tokens issued.',
            data=_bootstrap_payload(application, controller, token_result),
        )

    @http.route(
        ['/api/nsp_zeroconfig/v1/status', '/api/t4_coreapi/v1/zeroconfig/status'],
        type='http', auth='user', methods=['GET'], csrf=False,
    )
    def status(self, **kwargs):
        return _success('NSP Zeroconfig status loaded.', data={
            **discovery_status(),
            'enabled': True,
            'module': 'nsp_zeroconfig',
            'purpose': 'Secure NSP Local Server discovery and direct Controller Code bootstrap',
            'controller_must_exist': True,
            'requires_manual_approval': False,
            'bootstrap_endpoint': '/nsp/zeroconfig/controller/bootstrap',
            'bootstrap_auth': 'HMAC-SHA256(Discovery Secret, Controller Code|timestamp|nonce)',
            'auth_endpoint': '/auth/token',
        })
