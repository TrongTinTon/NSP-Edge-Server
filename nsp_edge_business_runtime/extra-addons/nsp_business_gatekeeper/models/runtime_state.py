# -*- coding: utf-8 -*-
from odoo import fields, models
class NspDeviceRuntimeState(models.Model):
    _inherit="nsp.device"
    active=fields.Boolean(default=True,index=True)
    cloud_removed=fields.Boolean(default=False,readonly=True,index=True,copy=False)
