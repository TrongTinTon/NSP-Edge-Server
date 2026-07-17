# -*- coding: utf-8 -*-
import logging

_logger = logging.getLogger(__name__)


def _safe_link_it_group_to_core_api(env):
    """Link NSP IT Parking Admin to Core API Manager only when both XML IDs exist.

    This replaces the old XML record update that could break module loading on
    databases where t4_coreapi had not yet been upgraded and
    t4_coreapi.group_core_api_manager did not exist. The link is optional and
    must never block registry/data loading.
    """
    it_group = env.ref('nsp_core.group_nsp_it_parking', raise_if_not_found=False)
    core_group = env.ref('t4_coreapi.group_core_api_manager', raise_if_not_found=False)
    if not it_group or not core_group:
        _logger.info(
            'Skip NSP IT/Core API implied group link: it_group=%s core_group=%s',
            bool(it_group), bool(core_group),
        )
        return
    if core_group not in it_group.implied_ids:
        it_group.write({'implied_ids': [(4, core_group.id)]})
        _logger.info('Linked NSP IT Parking Admin group to Core API Manager group.')


def post_init_hook(env):
    try:
        _safe_link_it_group_to_core_api(env.sudo())
    except Exception:
        _logger.exception('Unable to link NSP IT Parking Admin with Core API Manager.')
