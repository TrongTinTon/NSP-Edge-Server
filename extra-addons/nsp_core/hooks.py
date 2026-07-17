# -*- coding: utf-8 -*-
import logging

_logger = logging.getLogger(__name__)


def _add_group(user, group):
    if not user or not group:
        return False
    if group not in user.groups_id:
        user.write({'groups_id': [(4, group.id)]})
        return True
    return False


def _bootstrap_nsp_it_admin_access(env):
    """Make NSP menus visible after fresh install.

    NSP menus and ACLs are intentionally protected by NSP role groups. On a
    fresh Odoo database, the Administrator may not yet belong to any NSP group,
    so the installed Gatekeeper menus can be invisible until someone manually
    grants the role. This hook grants the NSP IT Parking Admin role to system
    administrators safely during installation.
    """
    group_it = env.ref('nsp_core.group_nsp_it_parking', raise_if_not_found=False)
    if not group_it:
        _logger.warning('NSP bootstrap skipped: group_nsp_it_parking not found.')
        return

    assigned = []

    for xmlid in ('base.user_admin', 'base.user_root'):
        user = env.ref(xmlid, raise_if_not_found=False)
        if user and _add_group(user.sudo(), group_it):
            assigned.append(xmlid)

    group_system = env.ref('base.group_system', raise_if_not_found=False)
    if group_system:
        # Future Settings administrators should also have NSP IT access unless
        # an administrator later removes the implied relation explicitly.
        if group_it not in group_system.implied_ids:
            group_system.sudo().write({'implied_ids': [(4, group_it.id)]})

        # Existing Settings administrators in the DB should see NSP immediately
        # after install, without needing a manual group assignment.
        users = env['res.users'].sudo().search([('groups_id', 'in', group_system.id)])
        for user in users:
            if _add_group(user, group_it):
                assigned.append('res.users:%s' % user.id)

    try:
        env['ir.ui.menu'].sudo().clear_caches()
    except Exception:
        _logger.debug('Unable to clear ir.ui.menu cache after NSP bootstrap.', exc_info=True)

    if assigned:
        _logger.info('Bootstrapped NSP IT Parking Admin access for: %s', ', '.join(assigned))
    else:
        _logger.info('NSP IT Parking Admin access already present for administrator users.')


def post_init_hook(env):
    try:
        _bootstrap_nsp_it_admin_access(env.sudo())
    except Exception:
        _logger.exception('Unable to bootstrap NSP administrator menu access.')
