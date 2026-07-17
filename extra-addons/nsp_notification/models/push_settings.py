# -*- coding: utf-8 -*-
from odoo import fields, models


class NspNotificationSettings(models.TransientModel):
    _inherit = "res.config.settings"

    nsp_push_enabled = fields.Boolean(
        string="Enable NSP Native Push",
        config_parameter="nsp_notification.push_enabled",
        default=True,
    )
    nsp_push_send_immediate = fields.Boolean(
        string="Send Immediately On Notification Create",
        config_parameter="nsp_notification.push_send_immediate",
        default=False,
    )
    nsp_push_max_retry = fields.Integer(
        string="Max Retry",
        config_parameter="nsp_notification.push_max_retry",
        default=3,
    )
    nsp_push_retry_delay_sec = fields.Integer(
        string="Retry Delay Seconds",
        config_parameter="nsp_notification.push_retry_delay_sec",
        default=60,
    )
    nsp_push_delivery_retention_days = fields.Integer(
        string="Delivery Retention Days",
        config_parameter="nsp_notification.push_delivery_retention_days",
        default=30,
    )
