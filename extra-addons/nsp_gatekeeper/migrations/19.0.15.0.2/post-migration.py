# -*- coding: utf-8 -*-
"""Repair stale/duplicate Core API actions for Edge Server Status.

Some databases may keep an older generated ``ir.actions.core_api`` record whose
Python code references ``request``.  ``ir.actions.core_api`` is evaluated by
safe_eval and intentionally exposes ``model``/payload context, not the HTTP
``request`` object.  Relink every route to the canonical XML action and remove
obsolete duplicates.
"""

from odoo import SUPERUSER_ID, api


ACTION_XMLID = "nsp_gatekeeper.action_core_api_nsp_gatekeeper_edge_server_status"
MANAGER_XMLID = "nsp_gatekeeper.action_endpoint_manager_nsp_gatekeeper"
ENDPOINT_CODE = "nsp_gatekeeper_edge_server_status"
ROUTE_SUFFIX = "edge-server/status"
CANONICAL_CODE = "model.api_edge_server_status()"


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})

    manager = env.ref(MANAGER_XMLID, raise_if_not_found=False)
    action = env.ref(ACTION_XMLID, raise_if_not_found=False)
    if not manager or not action:
        return

    # Force the canonical action definition regardless of previous generated or
    # manually edited values in the database.
    action.sudo().write({
        "name": "NSP Gatekeeper Edge Server Status",
        "model_id": manager.model_id.id,
        "code": CANONICAL_CODE,
        "endpoint_manager_id": manager.id,
        "endpoint_code": ENDPOINT_CODE,
        "route_suffix": ROUTE_SUFFIX,
        "http_methods": "POST",
    })

    Action = env["ir.actions.core_api"].sudo()
    Endpoint = env["core.api.endpoint"].sudo()

    # Relink routes that point to a stale action or match the same public path.
    routes = Endpoint.search([
        "|",
        ("action_id", "=", action.id),
        "&",
        ("endpoint_manager_id", "=", manager.id),
        ("route_suffix", "=", ROUTE_SUFFIX),
    ])
    if routes:
        routes.write({
            "action_id": action.id,
            "code": ENDPOINT_CODE,
            "route_suffix": ROUTE_SUFFIX,
            "http_methods": "POST",
        })

    duplicates = Action.search([
        ("id", "!=", action.id),
        ("endpoint_manager_id", "=", manager.id),
        "|",
        ("endpoint_code", "=", ENDPOINT_CODE),
        ("route_suffix", "=", ROUTE_SUFFIX),
    ])
    if duplicates:
        duplicate_routes = Endpoint.search([("action_id", "in", duplicates.ids)])
        if duplicate_routes:
            duplicate_routes.write({
                "action_id": action.id,
                "code": ENDPOINT_CODE,
                "route_suffix": ROUTE_SUFFIX,
                "http_methods": "POST",
            })
        duplicates.unlink()
