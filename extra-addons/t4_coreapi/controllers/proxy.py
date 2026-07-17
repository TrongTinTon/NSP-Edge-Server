# Part of T4 Core API. See LICENSE file for full copyright and licensing details.
import logging

from werkzeug.exceptions import NotFound

from odoo import _, http
from odoo.exceptions import AccessError
from odoo.http import request

from odoo.addons.t4_coreapi.controllers.base import CoreApiController
from odoo.addons.t4_coreapi.utils import log_core_api
from odoo.addons.t4_coreapi.utils.routing import build_gateway_path

_logger = logging.getLogger(__name__)


class CoreApiProxyController(CoreApiController):
    """HTTP gateway: validate token, then run the matching server action."""

    @http.route(
        '/<string:service_code>/<path:subpath>',
        type='http',
        auth='core_api',
        methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE'],
        csrf=False,
        save_session=False,
    )
    @log_core_api('api')
    def gateway(self, service_code, subpath, **kw):
        """Handle gateway routes: /{service_code}/{version}/{route_suffix}."""
        Version = request.env['core.api.version'].sudo()
        version, rest = Version.resolve_from_gateway_subpath(subpath)
        if not version:
            raise NotFound('Unknown or inactive API route.')

        if (service_code or '').strip().lower() == 'auth':
            raise NotFound('Use POST /auth/token for token requests.')

        application = self._get_application()
        app_service = (application.service_code or '').strip()
        if app_service != (service_code or '').strip():
            raise AccessError(_(
                'Application "%(app)s" is not registered for service code "%(code)s".',
                app=application.name,
                code=service_code,
            ))

        path = build_gateway_path(service_code, version.code, rest).rstrip('/')
        return request.env['core.api.endpoint'].dispatch_request(path, application)
