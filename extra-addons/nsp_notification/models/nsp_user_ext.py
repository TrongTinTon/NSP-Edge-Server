# -*- coding: utf-8 -*-
from odoo import fields, models


class NspUserNotificationExt(models.Model):
    _inherit = "nsp.user"

    notification_user_id = fields.Many2one(
        "res.users",
        string="Notification User",
        help="Optional Odoo user account that should receive realtime UI notifications for this NSP user.",
    )
    notification_enabled = fields.Boolean(string="Receive NSP Notifications", default=True)
