# -*- coding: utf-8 -*-
from odoo import fields, models, _
from odoo.exceptions import UserError


class ActionEndpointGenerateRoutesWizard(models.TransientModel):
    _name = 'action.endpoint.generate.routes.wizard'
    _description = 'Generate Core API Routes for Applications'

    endpoint_manager_id = fields.Many2one(
        'action.endpoint.manager',
        string='Endpoint Manager',
        required=True,
        readonly=True,
    )
    version_id = fields.Many2one(
        'core.api.version',
        string='API Version',
        required=True,
        default=lambda self: self.env['core.api.version'].get_default_version(),
    )
    application_ids = fields.Many2many(
        'core.api.application',
        string='Applications',
        domain=[('state', '=', 'active')],
        required=True,
        help='Select one or more Applications that should receive this whole API endpoint set.',
    )

    def action_generate(self):
        self.ensure_one()
        if not self.application_ids:
            raise UserError(_('Select at least one Core API Application.'))
        result = self.endpoint_manager_id._generate_core_api_routes_for_applications(
            self.application_ids,
            version=self.version_id,
        )
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Core API Routes'),
                'message': _('Generated routes for %(applications)s application(s): %(created)s created, %(updated)s updated.') % result,
                'type': 'success',
                'sticky': False,
                'next': {'type': 'ir.actions.act_window_close'},
            },
        }
