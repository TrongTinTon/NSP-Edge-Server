# -*- coding: utf-8 -*-
"""Adopt a pre-existing Friendship Core API Action before XML data is loaded.

T4 Core API may already have generated ``Friendships Snapshot`` from the
``@endpoint`` declaration. If that action has no module XML ID, loading
``data/cloud_sync_api_endpoints.xml`` would try to create a second action with the
same ``(endpoint_manager_id, name)`` and hit the T4 Core API unique constraint.
"""

import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)

XML_MODULE = "nsp_master_gatekeeper"
XML_NAME = "api_master_friendships"
ACTION_MODEL = "ir.actions.core_api"
ACTION_NAME = "Friendships Snapshot"
ENDPOINT_CODE = "nsp_edge_friendships_snapshot"
ROUTE_SUFFIX = "edge/friendships/snapshot"
MANAGER_XMLID = (
    "nsp_master_gatekeeper.action_endpoint_manager_nsp_master_gatekeeper_sync"
)


def _normalized_route(value):
    return str(value or "").strip().strip("/")


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    manager = env.ref(MANAGER_XMLID, raise_if_not_found=False)
    if not manager:
        _logger.warning(
            "Friendships Core API Action adoption skipped: Endpoint Manager missing"
        )
        return

    Action = env[ACTION_MODEL].sudo()
    ModelData = env["ir.model.data"].sudo()

    xmlid_row = ModelData.search(
        [("module", "=", XML_MODULE), ("name", "=", XML_NAME)], limit=1
    )
    xmlid_action = Action.browse(xmlid_row.res_id).exists() if xmlid_row else Action.browse()

    # Prefer the exact constrained identity first. T4 enforces uniqueness on
    # (endpoint_manager_id, name), therefore at most one exact-name action can exist.
    exact_name = Action.search(
        [
            ("endpoint_manager_id", "=", manager.id),
            ("name", "=", ACTION_NAME),
        ],
        limit=1,
    )
    exact_code = Action.search(
        [
            ("endpoint_manager_id", "=", manager.id),
            ("endpoint_code", "=", ENDPOINT_CODE),
        ],
        limit=1,
    )
    route_candidates = Action.search(
        [
            ("endpoint_manager_id", "=", manager.id),
            ("route_suffix", "!=", False),
        ]
    )
    exact_route = route_candidates.filtered(
        lambda rec: _normalized_route(rec.route_suffix) == ROUTE_SUFFIX
    )[:1]

    canonical = exact_name or exact_code or exact_route or xmlid_action
    if not canonical:
        # No pre-generated action exists. XML data should create it normally.
        return

    # Bind/rebind the stable module XML ID to the already-existing action before
    # XML data load. The XML loader will UPDATE this record instead of CREATE.
    if xmlid_row:
        values = {}
        if xmlid_row.model != ACTION_MODEL:
            values["model"] = ACTION_MODEL
        if xmlid_row.res_id != canonical.id:
            values["res_id"] = canonical.id
        if values:
            xmlid_row.write(values)
    else:
        ModelData.create(
            {
                "module": XML_MODULE,
                "name": XML_NAME,
                "model": ACTION_MODEL,
                "res_id": canonical.id,
                "noupdate": False,
            }
        )

    _logger.info(
        "Adopted existing Friendships Core API Action before XML load: action_id=%s",
        canonical.id,
    )
