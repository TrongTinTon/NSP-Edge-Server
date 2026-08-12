# -*- coding: utf-8 -*-
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Repair generated Core API Gateway routes after endpoint/action upgrades.

    ``ir.actions.core_api`` records are module data, while ``core.api.endpoint``
    routes are generated per Application. Updating the module action does not
    automatically refresh an already-generated route's ``action_id``. A stale
    route can therefore continue to exist at /v1/... with no Server Action and
    reject valid Controller requests before business code is reached.
    """
    env = api.Environment(cr, SUPERUSER_ID, {})
    manager = env.ref(
        'nsp_business_gatekeeper.action_endpoint_manager_nsp_business_gatekeeper',
        raise_if_not_found=False,
    )
    if not manager:
        _logger.warning('NSP Core API route repair skipped: Endpoint Manager is missing')
        return

    # Synchronize action metadata from the @endpoint decorators first. This
    # reuses existing actions by stable endpoint_code and does not create routes.
    manager._generate_core_api_action()

    Action = env['ir.actions.core_api'].sudo()
    Endpoint = env['core.api.endpoint'].sudo()
    actions = Action.search([
        ('endpoint_manager_id', '=', manager.id),
        ('endpoint_code', '!=', False),
    ])
    by_code = {str(a.endpoint_code or '').strip(): a for a in actions if a.endpoint_code}
    by_path = {
        str(a.route_suffix or '').strip().strip('/'): a
        for a in actions if a.route_suffix
    }
    if not by_code and not by_path:
        _logger.warning('NSP Core API route repair skipped: no endpoint actions found')
        return

    # Include routes whose manager pointer was lost but whose stable code/path
    # still belongs to this module. Do not touch unrelated Applications/routes.
    domains = []
    if by_code:
        domains.append(('code', 'in', list(by_code)))
    if by_path:
        domains.append(('route_suffix', 'in', list(by_path)))
    if len(domains) == 2:
        domain = ['|', domains[0], domains[1]]
    else:
        domain = domains

    routes = Endpoint.search(domain)
    repaired = 0
    for route in routes:
        code = str(route.code or '').strip()
        path = str(route.route_suffix or '').strip().strip('/')
        action = by_code.get(code) or by_path.get(path)
        if not action:
            continue
        vals = {}
        if route.action_id != action:
            vals['action_id'] = action.id
        if route.endpoint_manager_id != manager:
            vals['endpoint_manager_id'] = manager.id
        if route.code != action.endpoint_code:
            vals['code'] = action.endpoint_code
        if route.name != action.name:
            vals['name'] = action.name
        if (route.http_methods or '') != (action.http_methods or ''):
            vals['http_methods'] = action.http_methods or 'POST'
        if vals:
            route.write(vals)
            repaired += 1

    parking_action = by_code.get('nsp_controller_parking_detection_push')
    parking_routes = Endpoint.search([
        ('route_suffix', '=', 'parking/detections/push'),
    ])
    if parking_action:
        for route in parking_routes:
            if route.action_id != parking_action or route.endpoint_manager_id != manager:
                route.write({
                    'action_id': parking_action.id,
                    'endpoint_manager_id': manager.id,
                    'code': parking_action.endpoint_code,
                    'name': parking_action.name,
                    'http_methods': parking_action.http_methods or 'POST',
                })
                repaired += 1

    _logger.info(
        'NSP Core API Gateway route repair completed: %s route(s) repaired; parking routes=%s',
        repaired,
        len(parking_routes),
    )
