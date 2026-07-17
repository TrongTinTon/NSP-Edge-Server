# -*- coding: utf-8 -*-
import uuid
from odoo import api, fields, models


class NspSyncIdentityMixin(models.AbstractModel):
    _name = "nsp.sync.identity.mixin"
    _description = "NSP Sync Identity Mixin"

    sync_uid = fields.Char(
        string="Global Sync UID",
        required=True,
        copy=False,
        readonly=True,
        index=True,
        default=lambda self: str(uuid.uuid4()),
        help="Stable database-independent identifier used by NSP sync flows.",
    )


class NspActiveStateMixin(models.AbstractModel):
    _name = "nsp.active.state.mixin"
    _description = "NSP Active State Mixin"

    active = fields.Boolean(default=True, index=True)
