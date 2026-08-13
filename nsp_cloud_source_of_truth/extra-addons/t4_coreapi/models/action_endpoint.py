# -*- coding: utf-8 -*-
import inspect
import re

from odoo import _, fields, models
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

    def _get_endpoint_methods(self):
        self.ensure_one()
        target_class = type(self.env[self.model_id.model])
        return [
            (method_name, func)
            for method_name, func in inspect.getmembers(target_class, predicate=callable)
            if hasattr(func, '_is_endpoint')
        ]

    def _endpoint_specs(self):
        """Return one validated snapshot of the model's @endpoint declarations."""
        self.ensure_one()
        specs = []
        codes = {}
        routes = {}
        names = {}
        for method_name, func in self._get_endpoint_methods():
            action_name = getattr(func, '_endpoint_name')
            endpoint_code, route_suffix, methods = self._endpoint_meta(
                method_name, func, action_name=action_name,
            )
            if not endpoint_code:
                raise UserError(_('Endpoint method %s has no endpoint code.') % method_name)
            if not route_suffix:
                raise UserError(_('Endpoint method %s has no route path.') % method_name)
            previous = names.get(action_name)
            if previous and previous != method_name:
                raise UserError(_(
                    'Duplicate endpoint action name %(name)s on methods %(left)s and %(right)s.'
                ) % {'name': action_name, 'left': previous, 'right': method_name})
            names[action_name] = method_name
            previous = codes.get(endpoint_code)
            if previous and previous != method_name:
                raise UserError(_(
                    'Duplicate endpoint code %(code)s on methods %(left)s and %(right)s.'
                ) % {'code': endpoint_code, 'left': previous, 'right': method_name})
            previous = routes.get(route_suffix)
            if previous and previous != method_name:
                raise UserError(_(
                    'Duplicate endpoint route %(route)s on methods %(left)s and %(right)s.'
                ) % {'route': route_suffix, 'left': previous, 'right': method_name})
            codes[endpoint_code] = method_name
            routes[route_suffix] = method_name
            specs.append({
                'method_name': method_name,
                'name': action_name,
                'endpoint_code': endpoint_code,
                'route_suffix': route_suffix,
                'http_methods': methods,
            })
        return specs

    def _endpoint_action_vals(self, spec):
        return {
            'name': spec['name'],
            'model_id': self.model_id.id,
            'code': 'model.%s()' % spec['method_name'],
            'endpoint_manager_id': self.id,
            'endpoint_code': spec['endpoint_code'],
            'route_suffix': spec['route_suffix'],
            'http_methods': spec['http_methods'],
        }

    def _endpoint_route_vals(self, action, spec, application, version):
        return {
            'name': action.name,
            'code': spec['endpoint_code'],
            'version_id': version.id,
            'route_suffix': spec['route_suffix'],
            'http_methods': spec['http_methods'],
            'action_id': action.id,
            'application_id': application.id,
            'endpoint_manager_id': self.id,
        }

    @staticmethod
    def _field_changed(record, field_name, expected):
        current = record[field_name]
        if record._fields[field_name].type == 'many2one':
            current = current.id
        return current != expected

    def _generate_core_api_action(self, specs=None):
        """Idempotently synchronize API Actions from @endpoint declarations.

        Actions are generated independently from per-Application Gateway Routes.
        Existing actions are loaded once and matched by stable endpoint code first,
        then by action name for compatibility with older generated records.
        """
        self.ensure_one()
        specs = specs if specs is not None else self._endpoint_specs()
        Action = self.env['ir.actions.core_api'].sudo()
        existing = Action.search([('endpoint_manager_id', '=', self.id)])
        by_code = {
            str(action.endpoint_code or '').strip(): action
            for action in existing if action.endpoint_code
        }
        by_name = {action.name: action for action in existing if action.name}

        created = updated = 0
        create_vals = []
        claimed_ids = set()
        for spec in specs:
            vals = self._endpoint_action_vals(spec)
            action = by_code.get(spec['endpoint_code']) or by_name.get(spec['name'])
            if action and action.id not in claimed_ids:
                delta = {
                    key: value for key, value in vals.items()
                    if self._field_changed(action, key, value)
                }
                if delta:
                    action.write(delta)
                    updated += 1
                claimed_ids.add(action.id)
            else:
                create_vals.append(vals)

        new_actions = Action.create(create_vals) if create_vals else Action.browse()
        created = len(new_actions)
        actions = Action.search([('endpoint_manager_id', '=', self.id)])
        current_by_code = {
            str(action.endpoint_code or '').strip(): action
            for action in actions if action.endpoint_code
        }
        missing = [
            spec['route_suffix'] for spec in specs
            if spec['endpoint_code'] not in current_by_code
        ]
        if missing:
            raise UserError(
                _('Failed to generate Core API Actions for route(s): %s') % ', '.join(missing)
            )
        return {
            'created': created,
            'updated': updated,
            'actions_by_code': current_by_code,
            'specs': specs,
        }

    def _generate_core_api_routes_for_applications(self, applications, version=False):
        """Synchronize Actions, then create/repair per-Application Gateway Routes."""
        self.ensure_one()
        applications = applications.exists()
        if not applications:
            raise UserError(_('Select at least one Core API Application.'))
        version = version or self.version_id or self.env['core.api.version'].get_default_version()
        if not version:
            raise UserError(_('Select an API Version before generating routes.'))
        inactive = applications.filtered(lambda app: app.state != 'active')
        if inactive:
            raise UserError(
                _('Application(s) must be active: %s') % ', '.join(inactive.mapped('display_name'))
            )

        specs = self._endpoint_specs()
        if not specs:
            raise UserError(
                _('Model %s does not declare any @endpoint methods.') % self.model_id.model
            )
        action_result = self._generate_core_api_action(specs=specs)
        actions_by_code = action_result['actions_by_code']
        Endpoint = self.env['core.api.endpoint'].sudo()

        # One query for all selected Applications/Version. Match by endpoint code
        # first so a route-path rename updates the existing route rather than
        # creating a second route when the endpoint code stays stable.
        existing_routes = Endpoint.search([
            ('application_id', 'in', applications.ids),
            ('version_id', '=', version.id),
        ])
        by_code = {}
        by_route = {}
        for route in existing_routes.sorted(key=lambda rec: rec.id):
            app_id = route.application_id.id
            if route.code:
                by_code.setdefault((app_id, str(route.code).strip()), route)
            if route.route_suffix:
                by_route.setdefault((app_id, str(route.route_suffix).strip().strip('/')), route)

        created = updated = 0
        create_vals = []
        claimed_ids = set()
        for application in applications:
            for spec in specs:
                action = actions_by_code.get(spec['endpoint_code'])
                if not action:
                    raise UserError(
                        _('Missing generated Core API Action for route %s.') % spec['route_suffix']
                    )
                vals = self._endpoint_route_vals(action, spec, application, version)
                route = (
                    by_code.get((application.id, spec['endpoint_code']))
                    or by_route.get((application.id, spec['route_suffix']))
                )
                if route and route.id not in claimed_ids:
                    delta = {
                        key: value for key, value in vals.items()
                        if self._field_changed(route, key, value)
                    }
                    if delta:
                        route.write(delta)
                        updated += 1
                    claimed_ids.add(route.id)
                else:
                    create_vals.append(vals)

        if create_vals:
            created = len(Endpoint.create(create_vals))
        return {
            'created': created,
            'updated': updated,
            'applications': len(applications),
            'actions_created': action_result['created'],
            'actions_updated': action_result['updated'],
        }

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
        result = self._generate_core_api_action()
        if not result['specs']:
            raise UserError(
                _('Model %s does not declare any @endpoint methods.') % self.model_id.model
            )
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Core API Actions'),
                'message': _(
                    'API Actions synchronized for %(model)s: %(created)s created, %(updated)s updated.'
                ) % {
                    'model': self.model_id.model,
                    'created': result['created'],
                    'updated': result['updated'],
                },
                'type': 'success',
                'sticky': False,
                'next': {'type': 'ir.actions.client', 'tag': 'reload'},
            },
        }
