# Part of T4 Core API. See LICENSE file for full copyright and licensing details.

import json

from odoo.http import request

STATUS_SUCCESS = 'success'
STATUS_ERROR = 'error'


def make_json_response(body, status_code=200):
    """Return an HTTP JSON response with a standard Content-Type header."""
    return request.make_response(
        json.dumps(body, default=str),
        headers=[('Content-Type', 'application/json')],
        status=status_code,
    )


def success_body(message, data=None, **extra):
    """Build a standard success JSON body."""
    body = {
        'status': STATUS_SUCCESS,
        'message': message,
    }
    if data is not None:
        body['data'] = data
    body.update(extra)
    return body


def error_body(message, **extra):
    """Build a standard error JSON body."""
    body = {
        'status': STATUS_ERROR,
        'message': message,
    }
    body.update(extra)
    return body


def api_success_response(message, status_code=200, data=None, **extra):
    """Return HTTP 200 (or custom 2xx) with status + message (+ optional data)."""
    return make_json_response(success_body(message, data=data, **extra), status_code)


def api_error_response(message, status_code=400, **extra):
    """Return an error JSON response explaining why the request failed."""
    return make_json_response(error_body(message, **extra), status_code)


def auth_token_body(token_result, application):
    """Build nested api_token and refresh_token objects for auth responses."""
    api_token = {
        'token': token_result['access_token'],
    }
    if application.token_ttl_hours:
        api_token['expires_in'] = application.token_ttl_hours * 3600

    refresh_token = {
        'token': token_result['refresh_token'],
    }
    if application.refresh_token_ttl_hours:
        refresh_token['expires_in'] = application.refresh_token_ttl_hours * 3600

    client_instance_id = token_result.get('client_instance_id')
    if client_instance_id:
        api_token['client_instance_id'] = client_instance_id
        refresh_token['client_instance_id'] = client_instance_id
    return api_token, refresh_token


def auth_success_response(message, token_result, application, status_code=200):
    """Return the standard auth success payload.

    The gateway path still uses /<service_code>/<version>/<route>, but clients
    should not have to manually configure the remote service code. Include the
    authenticated application's server code in the token response so NSP Sync can
    resolve it from Client ID/Secret and cache it internally.
    """
    api_token, refresh_token = auth_token_body(token_result, application)
    service_code = (application.service_code or '').strip('/') if application else ''
    app_info = {
        'id': application.id if application else False,
        'name': application.name if application else False,
        'client_id': application.client_id if application else False,
        'service_code': service_code or False,
        'server_code': service_code or False,
        'gateway_base': f'/{service_code}/v1' if service_code else False,
    }
    return api_success_response(
        message,
        status_code=status_code,
        data={'application': app_info},
        application=app_info,
        service_code=service_code or False,
        server_code=service_code or False,
        client_id=application.client_id if application else False,
        api_token=api_token,
        refresh_token=refresh_token,
    )


def normalize_gateway_response(response_data, default_message='Request processed successfully.'):
    """Normalize a server-action response dict and extract the HTTP status code."""
    if response_data is None:
        return 200, success_body(default_message)

    if not isinstance(response_data, dict):
        from odoo.addons.t4_coreapi.utils.exception import CoreApiInvalidResponse
        raise CoreApiInvalidResponse('API response must be a dict. Use set_response() or set_api_response().')

    payload = dict(response_data)
    status_code = int(payload.pop('status_code', 200))

    if 'status' not in payload:
        payload['status'] = STATUS_SUCCESS if 200 <= status_code < 300 else STATUS_ERROR
    if 'message' not in payload:
        payload['message'] = (
            default_message if payload['status'] == STATUS_SUCCESS else 'Request failed.'
        )

    return status_code, payload
