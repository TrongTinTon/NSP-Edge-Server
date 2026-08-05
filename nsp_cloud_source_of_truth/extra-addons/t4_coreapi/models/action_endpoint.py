# -*- coding: utf-8 -*-
import inspect
import re
from odoo import models, fields, _
from odoo.exceptions import UserError


class ActionEndpointManager(models.Model):
    _name = 'action.endpoint.manager'
    _description = 'Action Endpoint Manager'

    name = fields.Char(string='Name', default='Endpoint')

    model_id = fields.Many2one(
        'ir.model',
        string='Model',
        required=True,
        domain=[('transient', '=', False)],
        ondelete='cascade',
    )

    core_api_action_ids = fields.One2many(
        'ir.actions.core_api',
        'endpoint_manager_id',
        string='Core API Actions',
    )

    application_id = fields.Many2one(
        'core.api.application',
        string='Default Application',
        help='Optional default application used by the route generation helper.',
    )

    version_id = fields.Many2one(
        'core.api.version',
        string='Default API Version',
        default=lambda self: self.env['core.api.version'].get_default_version(),
        help='Default API version used by the route generation wizard.',
    )

    generated_endpoint_ids = fields.One2many(
        'core.api.endpoint',
        'endpoint_manager_id',
        string='Generated Gateway Routes',
        readonly=True,
    )

    def _endpoint_meta(self, method_name, func, action_name=False):
        endpoint_code = (getattr(func, '_endpoint_code', None) or method_name or '').strip()
        route_suffix = getattr(func, '_endpoint_route_suffix', None) or re.sub(
            r'[^a-z0-9]+', '-', (action_name or method_name).lower()
        ).strip('-')
        route_suffix = (route_suffix or '').strip().strip('/')
        methods = (getattr(func, '_endpoint_methods', None) or 'POST').upper().replace(' ', '')
        return endpoint_code, route_suffix, methods

    def _endpoint_action_vals(self, method_name, func):
        action_name = getattr(func, '_endpoint_name')
        endpoint_code, route_suffix, methods = self._endpoint_meta(method_name, func, action_name=action_name)
        return {
            'name': action_name,
            'model_id': self.model_id.id,
            'code': f"model.{method_name}()",
            'endpoint_manager_id': self.id,
            'endpoint_code': endpoint_code,
            'route_suffix': route_suffix,
            'http_methods': methods,
        }

    def _endpoint_route_vals(self, action, method_name, func, application, version):
        self.ensure_one()
        action_name = getattr(func, '_endpoint_name')
        endpoint_code, route_suffix, methods = self._endpoint_meta(method_name, func, action_name=action_name)
        return {
            'name': action.name,
            'code': endpoint_code,
            'version_id': version.id,
            'route_suffix': route_suffix,
            'http_methods': methods,
            'action_id': action.id,
            'application_id': application.id,
            'endpoint_manager_id': self.id,
        }

    def _get_endpoint_methods(self):
        self.ensure_one()
        target_model_name = self.model_id.model
        target_class = type(self.env[target_model_name])
        return [
            (method_name, func)
            for method_name, func in inspect.getmembers(target_class, predicate=callable)
            if hasattr(func, '_is_endpoint')
        ]

    def _generate_core_api_action(self):
        self.ensure_one()
        CAaction = self.env['ir.actions.core_api'].sudo()
        for method_name, func in self._get_endpoint_methods():
            action_name = getattr(func, '_endpoint_name')
            vals = self._endpoint_action_vals(method_name, func)
            existing_action = CAaction.search([
                ('endpoint_manager_id', '=', self.id),
                ('endpoint_code', '=', vals['endpoint_code']),
            ], limit=1)
            if not existing_action:
                existing_action = CAaction.search([
                    ('endpoint_manager_id', '=', self.id),
                    ('name', '=', action_name),
                ], limit=1)
            if existing_action:
                existing_action.write(vals)
            else:
                CAaction.create(vals)

    def _generate_core_api_routes_for_applications(self, applications, version=False):
        self.ensure_one()
        applications = applications.exists()
        if not applications:
            raise UserError(_('Select at least one Core API Application.'))
        version = version or self.version_id or self.env['core.api.version'].get_default_version()
        if not version:
            raise UserError(_('Select an API Version before generating routes.'))

        Endpoint = self.env['core.api.endpoint'].sudo()
        CAaction = self.env['ir.actions.core_api'].sudo()
        self._generate_core_api_action()

        created = updated = 0
        for application in applications:
            if application.state != 'active':
                raise UserError(_('Application %s is not active.') % application.display_name)
            for method_name, func in self._get_endpoint_methods():
                action_name = getattr(func, '_endpoint_name')
                endpoint_code, route_suffix, methods = self._endpoint_meta(method_name, func, action_name=action_name)
                action = CAaction.search([
                    ('endpoint_manager_id', '=', self.id),
                    ('endpoint_code', '=', endpoint_code),
                ], limit=1)
                if not action:
                    action = CAaction.search([
                        ('endpoint_manager_id', '=', self.id),
                        ('name', '=', action_name),
                    ], limit=1)
                if not action:
                    continue
                vals = self._endpoint_route_vals(action, method_name, func, application, version)
                existing = Endpoint.search([
                    ('application_id', '=', application.id),
                    ('version_id', '=', version.id),
                    ('route_suffix', '=', vals['route_suffix']),
                ], limit=1)
                if existing:
                    existing.write(vals)
                    updated += 1
                else:
                    Endpoint.create(vals)
                    created += 1
        return {'created': created, 'updated': updated, 'applications': len(applications)}

    def _generate_core_api_routes(self, applications=False, version=False):
        self.ensure_one()
        applications = applications or self.application_id
        if not applications:
            return False
        return self._generate_core_api_routes_for_applications(applications, version=version)

    def action_generate_core_api_routes(self):
        self.ensure_one()
        wizard = self.env['action.endpoint.generate.routes.wizard'].create({
            'endpoint_manager_id': self.id,
            'version_id': self.version_id.id if self.version_id else False,
        })
        return {
            'type': 'ir.actions.act_window',
            'name': _('Generate API Actions & Routes'),
            'res_model': 'action.endpoint.generate.routes.wizard',
            'res_id': wizard.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def action_generate_core_api_action(self):
        self.ensure_one()
        self._generate_core_api_action()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Success'),
                'message': _('API Actions have been synchronized for model %s.') % self.model_id.model,
                'type': 'success',
                'sticky': False,
                'next': {'type': 'ir.actions.client', 'tag': 'reload'},
            }
        }
