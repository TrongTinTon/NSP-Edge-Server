# -*- coding: utf-8 -*-
from odoo import api, models


class NspNotificationMobile(models.Model):
    _inherit = 'nsp.notification'

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        Device = self.env['nsp.mobile.device'].sudo()
        Delivery = self.env['nsp.notification.delivery'].sudo()
        for notification in records.filtered('recipient_user_id'):
            devices = Device.search([
                ('user_id', '=', notification.recipient_user_id.id), ('active', '=', True),
            ])
            for device in devices:
                Delivery.enqueue(notification, device.device_uid, channel='realtime', provider='realtime')
                if device.push_enabled and device.push_provider != 'none' and device.push_token:
                    Delivery.enqueue(
                        notification, device.device_uid, channel='push', provider=device.push_provider,
                    )
        return records
