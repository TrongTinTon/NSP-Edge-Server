# Part of T4 Core API. See LICENSE file for full copyright and licensing details.

from odoo import models

from odoo.addons.t4_coreapi.utils.core_api_utils import endpoint, set_response


class MyApiService(models.Model):
    _inherit = 'res.partner'

    @endpoint('ping')
    def api_ping(self):
        return set_response(data='Hello World')
