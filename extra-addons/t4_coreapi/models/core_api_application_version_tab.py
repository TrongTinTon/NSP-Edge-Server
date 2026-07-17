# Part of T4 Core API. See LICENSE file for full copyright and licensing details.

from odoo import _, api, fields, models


class CoreApiApplicationVersionTab(models.Model):
    _name = 'core.api.application.version.tab'
    _description = 'Application API Version Route Tab'
    _order = 'version_id, id'

    application_id = fields.Many2one(
        'core.api.application',
        string='Application',
        required=True,
        ondelete='cascade',
        index=True,
    )
    version_id = fields.Many2one(
        'core.api.version',
        string='API Version',
        required=True,
        ondelete='cascade',
        index=True,
    )
    domain_id = fields.Many2one(
        related='application_id.domain_id',
        store=True,
        readonly=True,
    )
    endpoint_ids = fields.One2many(
        'core.api.endpoint',
        'version_tab_id',
        string='Routes',
    )
    endpoint_count = fields.Integer(compute='_compute_endpoint_count')

    _application_version_unique = models.Constraint(
        'unique(application_id, version_id)',
        'Each application may only have one route tab per API version.',
    )

    @api.depends('endpoint_ids')
    def _compute_endpoint_count(self):
        for tab in self:
            tab.endpoint_count = len(tab.endpoint_ids)

    @api.model
    def get_or_create(self, application_id, version_id):
        """Return the route tab for an application/version pair."""
        if not application_id or not version_id:
            return self.browse()
        tab = self.search([
            ('application_id', '=', application_id),
            ('version_id', '=', version_id),
        ], limit=1)
        if tab:
            return tab
        return self.create({
            'application_id': application_id,
            'version_id': version_id,
        })
