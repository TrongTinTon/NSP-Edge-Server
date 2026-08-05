# Part of T4 Core API. See LICENSE file for full copyright and licensing details.
from odoo import http
from odoo.http import request


class CoreApiController(http.Controller):
    """Base controller. Inherit when adding custom secured Core API endpoints."""

    def _get_application(self):
        """Return the authenticated application from request context."""
        application_id = request.env.context.get('core_api_application_id')
        if not application_id:
            return request.env['core.api.application']
        return request.env['core.api.application'].sudo().browse(application_id)
