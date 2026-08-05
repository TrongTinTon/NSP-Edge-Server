# Part of T4 Core API. See LICENSE file for full copyright and licensing details.
import logging

from werkzeug.exceptions import NotFound

from odoo import http
from odoo.http import request

from odoo.addons.t4_coreapi.controllers.base import CoreApiController
from odoo.addons.t4_coreapi.utils import log_core_api
from odoo.addons.t4_coreapi.utils.routing import build_gateway_path

_logger = logging.getLogger(__name__)


class CoreApiProxyController(CoreApiController):
    """Bearer-token gateway. Application is derived only from the token."""

    @http.route(
        '/<string:version_code>/<path:route_suffix>',
        type='http',
        auth='core_api',
        methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE'],
        csrf=False,
        save_session=False,
    )
    @log_core_api('api')
    def gateway(self, version_code, route_suffix, **kw):
        """Handle /{version}/{route}; token scopes the Route Path to its Application."""
        version = request.env['core.api.version'].sudo().search([
            ('code', '=', (version_code or '').strip('/')),
            ('active', '=', True),
        ], limit=1)
        if not version:
            raise NotFound('Unknown or inactive API version.')

        application = self._get_application()
        path = build_gateway_path(version.code, route_suffix).rstrip('/')
        return request.env['core.api.endpoint'].dispatch_request(path, application)
