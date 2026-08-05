# Part of T4 Core API. See LICENSE file for full copyright and licensing details.

import functools
import logging
import time

from odoo.http import request

_logger = logging.getLogger(__name__)


def log_core_api(event_type='api'):
    """Decorator that audit-logs Core API controller calls with duration and status."""

    def decorator(func):
        @functools.wraps(func)
        def wrapper(self, *args, **kwargs):
            """Wrap the controller method, log success or failure, then re-raise errors."""
            t0 = time.time()
            route = request.httprequest.path
            method = request.httprequest.method
            ip = request.httprequest.environ.get('REMOTE_ADDR')
            ua = request.httprequest.headers.get('User-Agent')
            application = None
            token_rec = None
            application_id = request.env.context.get('core_api_application_id')
            if application_id:
                application = request.env['core.api.application'].sudo().browse(application_id)
            token_id = request.env.context.get('core_api_token_id')
            if token_id:
                token_rec = request.env['core.api.token'].sudo().browse(token_id)

            try:
                result = func(self, *args, **kwargs)
                duration = (time.time() - t0) * 1000
                status_code = getattr(result, 'status_code', 200)
                request.env['core.api.log'].sudo().log_event(
                    event_type=event_type,
                    route=route,
                    method=method,
                    ip_address=ip,
                    status_code=status_code,
                    success=True,
                    application=application,
                    token=token_rec,
                    duration_ms=duration,
                    user_agent=ua,
                )
                return result
            except Exception as e:
                duration = (time.time() - t0) * 1000
                status_code = getattr(e, 'code', 500)
                request.env['core.api.log'].sudo().log_event(
                    event_type=event_type,
                    route=route,
                    method=method,
                    ip_address=ip,
                    status_code=status_code,
                    success=False,
                    application=application,
                    token=token_rec,
                    duration_ms=duration,
                    error_message=str(e)[:500],
                    user_agent=ua,
                )
                raise
        return wrapper
    return decorator
