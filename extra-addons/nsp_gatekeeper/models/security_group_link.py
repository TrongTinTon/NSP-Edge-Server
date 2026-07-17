# -*- coding: utf-8 -*-
import logging

from odoo import api, models, SUPERUSER_ID

_logger = logging.getLogger(__name__)


class NSPSecurityGroupLink(models.AbstractModel):
    _name = 'nsp.security.group.link'
    _description = 'NSP Security Group Link Helper'

    def _register_hook(self):
        res = super()._register_hook()
        try:
            env = api.Environment(self.env.cr, SUPERUSER_ID, {})
            it_group = env.ref('nsp_core.group_nsp_it_parking', raise_if_not_found=False)
            core_group = env.ref('t4_coreapi.group_core_api_manager', raise_if_not_found=False)
            if it_group and core_group and core_group not in it_group.implied_ids:
                it_group.write({'implied_ids': [(4, core_group.id)]})
                _logger.info('Linked NSP IT Parking Admin group to Core API Manager group.')
        except Exception:
            _logger.exception('Unable to link NSP IT Parking Admin with Core API Manager during registry hook.')
        return res
