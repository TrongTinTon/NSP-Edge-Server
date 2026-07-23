from odoo import api
import json
import logging
from functools import wraps
from odoo.http import request
from odoo.addons.t4_coreapi.utils.response import STATUS_ERROR, STATUS_SUCCESS
from odoo.addons.t4_coreapi.utils.exception import ensure_dict, CoreApiInvalidBody

_logger = logging.getLogger(__name__)


def endpoint(name=None, route_path=None, methods='POST', code=None):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            result = func(*args, **kwargs)
            if not isinstance(result, dict):
                set_response(data=result)
            else:
                message = result.get('message')
                data = result.get('data')
                status_code = int(result.get('status_code') or 200)

                # Endpoint service methods may return a standard action payload:
                # {"status_code": 200, "message": "...", "data": {...}}.
                # Keep that status code instead of always returning HTTP 200.
                if not message and data is None:
                    set_response(data=result, status_code=status_code)
                else:
                    set_response(data=data, message=message, status_code=status_code)
            return result

        wrapper._is_endpoint = True
        wrapper._endpoint_name = name or func.__name__.replace('_', ' ').title()
        wrapper._endpoint_route_suffix = route_path
        wrapper._endpoint_methods = methods or 'POST'
        wrapper._endpoint_code = code or func.__name__
        return api.model(wrapper)

    return decorator


def _extract_context(obj=None):
    """Resolve an Odoo context dict from self, env, request, or a raw dict."""
    if hasattr(obj, 'context'):
        return obj.context

    if hasattr(obj, 'env') and hasattr(obj.env, 'context'):
        return obj.env.context

    if isinstance(obj, dict):
        return obj

    return {}


def get_params(obj=None):
    """Return URL or form parameters from the core_api context or request."""
    if not obj:
        return dict(request.httprequest.args) if request else {}

    ctx = _extract_context(obj)
    return ctx.get('core_api_params', {})
   


def get_body(obj=None):
    """Return the parsed JSON body from the core_api context or request."""
    if not obj:
        if not request:
            return {}

        raw_data = request.httprequest.data
        if not raw_data:
            return {}

        try:
            parsed_data = json.loads(raw_data)
        except Exception:
            raise CoreApiInvalidBody('Request body is not valid JSON.')

        return ensure_dict(parsed_data)

    ctx = _extract_context(obj)
    if ctx:
        body = ctx.get('core_api_body')

        if body is None:
            return {}
        return ensure_dict(body)

    return {}


def set_response(data=False, message='Request processed successfully.', status_code=200):
    """Build a standard JSON API response via set_api_response."""

    response_payload = {
        'status_code': status_code,
        'status': STATUS_SUCCESS if 200 <= status_code < 300 else STATUS_ERROR,
        'message': message if message else "something wrong!",
    }
    if data is not False and data is not None:
        response_payload['data'] = data

    request.env['core.api.application'].set_api_response(response_payload)
