# -*- coding: utf-8 -*-
from odoo import api, fields, models


class NspNotificationDelivery(models.Model):
    _name = 'nsp.notification.delivery'
    _description = 'NSP Notification Delivery'
    _order = 'create_date desc, id desc'

    notification_id = fields.Many2one('nsp.notification', required=True, index=True, ondelete='cascade')
    recipient_user_id = fields.Many2one('nsp.user', related='notification_id.recipient_user_id', store=True, index=True, readonly=True)
    device_uid = fields.Char(required=True, index=True, readonly=True)
    channel = fields.Selection([('realtime', 'Realtime'), ('push', 'Push')], required=True, index=True, readonly=True)
    provider = fields.Char(required=True, index=True, readonly=True)
    state = fields.Selection([
        ('pending', 'Pending'), ('sent', 'Sent'), ('delivered', 'Delivered'), ('failed', 'Failed')
    ], default='pending', required=True, index=True, readonly=True)
    attempt_count = fields.Integer(default=0, readonly=True)
    sent_at = fields.Datetime(readonly=True)
    delivered_at = fields.Datetime(readonly=True)
    last_error = fields.Text(readonly=True)

    _sql_constraints = [
        ('delivery_unique', 'unique(notification_id, device_uid, channel, provider)', 'Notification delivery already exists for this device/channel/provider.'),
    ]

    @api.model
    def enqueue(self, notification, device_uid, channel='realtime', provider='realtime'):
        notification.ensure_one()
        vals = {
            'notification_id': notification.id,
            'device_uid': str(device_uid or '').strip(),
            'channel': channel,
            'provider': str(provider or '').strip().lower() or 'none',
        }
        existing = self.search([
            ('notification_id', '=', notification.id),
            ('device_uid', '=', vals['device_uid']),
            ('channel', '=', channel),
            ('provider', '=', vals['provider']),
        ], limit=1)
        if existing:
            return existing
        delivery = self.create(vals)
        self.env['nsp.notification.delivery.service'].dispatch(delivery)
        return delivery

    def mark_delivered(self):
        pending = self.filtered(lambda rec: rec.state != 'delivered')
        if pending:
            pending.write({'state': 'delivered', 'delivered_at': fields.Datetime.now()})
        return True


class NspNotificationDeliveryService(models.AbstractModel):
    _name = 'nsp.notification.delivery.service'
    _description = 'NSP Notification Delivery Service'

    @api.model
    def dispatch(self, delivery):
        delivery.ensure_one()
        method = getattr(self, '_dispatch_%s' % (delivery.provider or '').replace('-', '_'), None)
        if not method:
            # Unknown providers are intentionally left pending so a future provider module can handle them.
            return False
        return method(delivery)

    @api.model
    def _dispatch_realtime(self, delivery):
        # Realtime delivery is acknowledged by Mobile through /mobile/realtime/events.
        delivery.write({
            'state': 'sent',
            'attempt_count': delivery.attempt_count + 1,
            'sent_at': fields.Datetime.now(),
            'last_error': False,
        })
        return True

    @api.model
    def _dispatch_none(self, delivery):
        return False
