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


def auth_success_response(token_result, application, status_code=200):
    """Return one rotating access/refresh token pair."""
    data = {
        'access_token': token_result['access_token'],
        'refresh_token': token_result['refresh_token'],
        'token_type': 'Bearer',
    }
    if application.token_ttl_hours:
        data['expires_in'] = application.token_ttl_hours * 3600
    if application.refresh_token_ttl_days:
        data['refresh_expires_in'] = application.refresh_token_ttl_days * 86400
    return api_success_response('OK', status_code=status_code, data=data)

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
