# Part of T4 Core API. Fresh-install clean API version model.

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from odoo.addons.t4_coreapi.utils.routing import parse_gateway_subpath


class CoreApiVersion(models.Model):
    _name = 'core.api.version'
    _description = 'API Version'
    _order = 'sequence, code'

    name = fields.Char(required=True)
    code = fields.Char(
        required=True,
        index=True,
        help='URL segment after the service code, e.g. v1 in /gk/v1/gate1.',
    )
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    description = fields.Text()
    endpoint_ids = fields.One2many('core.api.endpoint', 'version_id', string='Gateway Routes')
    endpoint_count = fields.Integer(compute='_compute_endpoint_count')

    _code_unique = models.Constraint(
        'unique(code)',
        'API version code must be unique.',
    )

    @api.depends('endpoint_ids')
    def _compute_endpoint_count(self):
        for rec in self:
            rec.endpoint_count = len(rec.endpoint_ids)

    @api.constrains('code')
    def _check_code(self):
        for rec in self:
            code = (rec.code or '').strip()
            if not code:
                raise ValidationError(_('API version code is required.'))
            if '/' in code or ' ' in code:
                raise ValidationError(_('API version code must not contain slashes or spaces.'))

    @api.model
    def get_default_version(self):
        version = self.env.ref('t4_coreapi.core_api_version_v1', raise_if_not_found=False)
        if version and version.active:
            return version
        return self.search([('active', '=', True)], order='sequence, code', limit=1)

    def action_view_endpoints(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Gateway Routes'),
            'res_model': 'core.api.endpoint',
            'view_mode': 'list,form',
            'domain': [('version_id', '=', self.id)],
            'context': {'default_version_id': self.id},
        }

    @api.model
    def get_active_by_code(self, code):
        if not code:
            return self.browse()
        return self.sudo().search([('code', '=', code), ('active', '=', True)], limit=1)

    @api.model
    def resolve_from_gateway_subpath(self, subpath):
        version_code, rest = parse_gateway_subpath(subpath)
        if not version_code:
            return self.browse(), ''
        version = self.get_active_by_code(version_code)
        return version, rest
