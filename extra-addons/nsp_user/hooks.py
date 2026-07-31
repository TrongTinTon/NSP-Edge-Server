# -*- coding: utf-8 -*-
import logging

_logger = logging.getLogger(__name__)


def post_init_hook(env):
    """Backfill the mandatory 1:1 profile for existing internal Odoo Users."""
    try:
        users = env['res.users'].sudo().search([('share', '=', False)])
        users._ensure_nsp_user_profile()
    except Exception:
        _logger.exception('Unable to create mandatory NSP User profiles.')
        raise
