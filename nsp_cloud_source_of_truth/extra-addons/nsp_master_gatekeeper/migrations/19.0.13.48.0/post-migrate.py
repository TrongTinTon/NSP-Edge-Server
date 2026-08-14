# -*- coding: utf-8 -*-
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


FRIENDSHIP_ROUTE = "edge/friendships/snapshot"
SEED_ROUTES = (
    "edge/users/snapshot",
    "edge/vehicle-borrows/snapshot",
)


def _normalized_route(value):
    return str(value or "").strip().strip("/")


def migrate(cr, version):
    """Provision the new Friendship Gateway Route for existing Edge applications.

    T4 Core API separates module API Actions (``ir.actions.core_api``) from the
    per-Application public Gateway Routes (``core.api.endpoint``). Adding a new
    @endpoint/XML action therefore does not automatically grant an already-existing
    Edge Service Application access to that route.

    Reuse only Application/API-Version pairs that already own the Users Snapshot or
    Vehicle Borrows Snapshot route. Those pairs are the existing Cloud Master Data
    synchronization scope. No unrelated Application is granted the new route.
    """
    env = api.Environment(cr, SUPERUSER_ID, {})
    manager = env.ref(
        "nsp_master_gatekeeper.action_endpoint_manager_nsp_master_gatekeeper_sync",
        raise_if_not_found=False,
    )
    if not manager:
        _logger.warning("Friendships Gateway route provisioning skipped: endpoint manager missing")
        return

    Action = env["ir.actions.core_api"].sudo()
    Endpoint = env["core.api.endpoint"].sudo()

    friendship_action = env.ref(
        "nsp_master_gatekeeper.api_master_friendships",
        raise_if_not_found=False,
    )
    if not friendship_action:
        friendship_action = Action.search(
            [
                ("endpoint_manager_id", "=", manager.id),
                ("endpoint_code", "=", "nsp_edge_friendships_snapshot"),
            ],
            limit=1,
        )
    if not friendship_action:
        _logger.error("Friendships Gateway route provisioning failed: Core API Action missing")
        return

    # Find the exact Application/API-Version pairs already authorized for the
    # surrounding Cloud -> Edge Master Data routes.
    seeds = Endpoint.search(
        [
            ("route_suffix", "in", list(SEED_ROUTES)),
            ("application_id", "!=", False),
            ("version_id", "!=", False),
        ]
    )
    pairs = {}
    for seed in seeds.sorted(key=lambda rec: rec.id):
        key = (seed.application_id.id, seed.version_id.id)
        pairs.setdefault(key, seed)

    if not pairs:
        _logger.warning(
            "Friendships Gateway route provisioning found no existing Users/Borrows "
            "Gateway route to infer the Edge Service Application from"
        )
        return

    existing = Endpoint.search(
        [
            ("route_suffix", "=", FRIENDSHIP_ROUTE),
            ("application_id", "in", list({key[0] for key in pairs})),
            ("version_id", "in", list({key[1] for key in pairs})),
        ]
    )
    existing_by_pair = {
        (route.application_id.id, route.version_id.id): route
        for route in existing.sorted(key=lambda rec: rec.id)
    }

    created = repaired = 0
    for (application_id, version_id), _seed in pairs.items():
        values = {
            "name": friendship_action.name,
            "code": friendship_action.endpoint_code,
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
        "Friendships Gateway route provisioned for existing Edge applications: "
        "created=%s repaired=%s pairs=%s",
        created,
        repaired,
        len(pairs),
    )
