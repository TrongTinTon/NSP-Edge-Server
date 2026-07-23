# -*- coding: utf-8 -*-
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    """Remove standalone child-model views/actions replaced by inline User tabs."""
    env = api.Environment(cr, SUPERUSER_ID, {})
    for xmlid in (
        "nsp_user.action_nsp_user_card",
        "nsp_user.view_nsp_user_card_search",
        "nsp_user.view_nsp_user_card_list",
        "nsp_user.action_nsp_user_friendship",
        "nsp_user.view_nsp_user_friendship_list",
    ):
        record = env.ref(xmlid, raise_if_not_found=False)
        if record:
            record.unlink()
