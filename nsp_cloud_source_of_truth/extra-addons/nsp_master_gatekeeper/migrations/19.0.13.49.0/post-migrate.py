# -*- coding: utf-8 -*-
"""Repair/provision Friendship Gateway Routes without regenerating API Actions."""

import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)

FRIENDSHIP_ROUTE = "edge/friendships/snapshot"
FRIENDSHIP_CODE = "nsp_edge_friendships_snapshot"
SEED_ROUTES = (
    "edge/users/snapshot",
    "edge/vehicle-borrows/snapshot",
)


def _normalized_route(value):
    return str(value or "").strip().strip("/")


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    manager = env.ref(
        "nsp_master_gatekeeper.action_endpoint_manager_nsp_master_gatekeeper_sync",
        raise_if_not_found=False,
    )
    friendship_action = env.ref(
        "nsp_master_gatekeeper.api_master_friendships",
        raise_if_not_found=False,
    )
    if not manager or not friendship_action:
        _logger.warning(
            "Friendships Gateway route provisioning skipped: manager/action missing"
        )
        return

    Endpoint = env["core.api.endpoint"].sudo()

    # Use only Application/API-Version pairs that already have an established
    # Cloud -> Edge master-data route. This extends the same authorization scope.
    seeds = Endpoint.search(
        [
            ("route_suffix", "in", list(SEED_ROUTES)),
            ("application_id", "!=", False),
            ("version_id", "!=", False),
        ]
    )
    pairs = {}
    for seed in seeds.sorted(key=lambda rec: rec.id):
        pairs.setdefault((seed.application_id.id, seed.version_id.id), seed)

    if not pairs:
        _logger.warning(
            "Friendships Gateway route provisioning found no Users/Borrows seed route"
        )
        return

    application_ids = list({pair[0] for pair in pairs})
    version_ids = list({pair[1] for pair in pairs})
    candidates = Endpoint.search(
        [
            ("application_id", "in", application_ids),
            ("version_id", "in", version_ids),
        ]
    )

    existing_by_pair = {}
    for route in candidates.sorted(key=lambda rec: rec.id):
        is_friendship = (
            str(route.code or "").strip() == FRIENDSHIP_CODE
            or _normalized_route(route.route_suffix) == FRIENDSHIP_ROUTE
        )
        if is_friendship:
            existing_by_pair.setdefault(
                (route.application_id.id, route.version_id.id), route
            )

    created = repaired = 0
    for application_id, version_id in pairs:
        values = {
            "name": friendship_action.name,
            "code": FRIENDSHIP_CODE,
            "version_id": version_id,
            "route_suffix": FRIENDSHIP_ROUTE,
            "http_methods": friendship_action.http_methods or "POST",
            "action_id": friendship_action.id,
            "application_id": application_id,
            "endpoint_manager_id": manager.id,
        }
        route = existing_by_pair.get((application_id, version_id))
        if route:
            delta = {}
            for field_name, expected in values.items():
                current = route[field_name]
                if route._fields[field_name].type == "many2one":
                    current = current.id
                if current != expected:
                    delta[field_name] = expected
            if delta:
                route.write(delta)
                repaired += 1
            continue

        Endpoint.create(values)
        created += 1

    _logger.info(
        "Friendships Gateway route provisioned: created=%s repaired=%s pairs=%s",
        created,
        repaired,
        len(pairs),
    )
