# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class CoreApiApplication(models.Model):
    _inherit = 'core.api.application'

    nsp_controller_ids = fields.One2many(
        'nsp.controller', 'core_api_application_id',
        string='NSP Controllers / Edge Servers', readonly=True,
    )
    nsp_controller_count = fields.Integer(compute='_compute_nsp_controller_count')

    @api.depends('nsp_controller_ids')
    def _compute_nsp_controller_count(self):
        for record in self:
            record.nsp_controller_count = len(record.nsp_controller_ids)

    def write(self, vals):
        # Application Server Code is the API route prefix and may be shared by
        # many NSP Controllers / Edge Servers. Changing it must not rewrite
        # controller_id, which is the operational node identity.
        return super().write(vals)

    def action_open_nsp_controllers(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('NSP Controllers / Edge Servers'),
            'res_model': 'nsp.controller',
            'view_mode': 'list,form',
            'domain': [('core_api_application_id', '=', self.id)],
            'context': {'default_core_api_application_id': self.id},
        }
