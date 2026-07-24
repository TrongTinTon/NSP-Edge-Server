# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, ValidationError
from odoo.addons.t4_coreapi.utils.core_api_utils import endpoint, get_body, get_params


class NspMobileApiService(models.Model):
    _name = 'nsp.mobile.api.service'
    _description = 'NSP Mobile API Service'

    @api.model
    def _mobile_context(self):
        ctx = self.env.context
        if ctx.get('core_api_token_kind') != 'mobile':
            raise AccessError(_('Mobile Token is required.'))
        if ctx.get('core_api_subject_model') != 'nsp.user' or not ctx.get('core_api_subject_id'):
            raise AccessError(_('Mobile Token has no valid user binding.'))
        user = self.env['nsp.user'].sudo().browse(int(ctx['core_api_subject_id'])).exists()
        session = self.env['nsp.mobile.session'].sudo().search([
            ('session_uid', '=', ctx.get('core_api_session_uid')),
            ('user_id', '=', user.id),
            ('state', '=', 'active'),
        ], limit=1)
        device = self.env['nsp.mobile.device'].sudo().search([
            ('device_uid', '=', ctx.get('core_api_device_uid')),
            ('user_id', '=', user.id),
            ('active', '=', True),
        ], limit=1)
        if not user or not user.active or not user.mobile_enabled or not session or not device:
            raise AccessError(_('Mobile session is no longer active.'))
        session.touch()
        return user, device, session

    @api.model
    def _pagination(self, params, default=50, maximum=200):
        try:
            limit = min(max(1, int(params.get('limit') or default)), maximum)
            offset = max(0, int(params.get('offset') or 0))
        except (TypeError, ValueError):
            raise ValidationError(_('Invalid pagination parameters.'))
        return limit, offset

    @api.model
    def _body_int(self, body, field_name, required=True):
        raw = body.get(field_name)
        if raw in (None, '', False):
            if required:
                raise ValidationError(_('%s is required.') % field_name)
            return 0
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise ValidationError(_('Invalid %s.') % field_name)
        if required and value <= 0:
            raise ValidationError(_('Invalid %s.') % field_name)
        return value

    @api.model
    def _parse_datetime(self, value, field_name, default=False):
        if not value:
            return default
        try:
            return fields.Datetime.to_datetime(value)
        except (TypeError, ValueError):
            raise ValidationError(_('Invalid %s timestamp.') % field_name)

    @api.model
    def _user_data(self, user):
        return {
            'id': user.id,
            'name': user.name,
            'email': user.email or None,
            'phone': user.phone or None,
        }

    @api.model
    def _vehicle_data(self, vehicle, latest_tx=None, active_borrow=None):
        return {
            'id': vehicle.id,
            'license_plate': vehicle.license_plate,
            'vehicle_type': vehicle.vehicle_type_id.name if vehicle.vehicle_type_id else None,
            'brand': vehicle.brand_id.name if vehicle.brand_id else None,
            'model': vehicle.model_id.name if vehicle.model_id else None,
            'color': vehicle.color_id.name if vehicle.color_id else None,
            'active': bool(vehicle.active),
            'parking_status': (
                'inside' if latest_tx and latest_tx.status == 'allowed' and latest_tx.event_type == 'check_in'
                else 'outside'
            ),
            'last_parking_event': self._transaction_data(latest_tx) if latest_tx else None,
            'active_borrow': self._borrow_data(active_borrow) if active_borrow else None,
        }

    @api.model
    def _transaction_data(self, tx):
        if not tx:
            return None
        return {
            'id': tx.id,
            'event_time': fields.Datetime.to_string(tx.event_time) if tx.event_time else None,
            'event_type': tx.event_type,
            'decision': tx.status,
            'parking_area': tx.parking_area_id.name if tx.parking_area_id else None,
            'lane': tx.lane_id.name if tx.lane_id else None,
            'license_plate': tx.vehicle_id.license_plate if tx.vehicle_id else tx.vehicle_tid or None,
            'user': tx.user_id.name if tx.user_id else None,
            'error_code': tx.error_code or None,
            'error_message': tx.error_message or None,
        }

    @api.model
    def _friendship_data(self, friendship, current_user):
        friend = friendship.addressee_id if friendship.requester_id == current_user else friendship.requester_id
        return {
            'id': friendship.id,
            'state': friendship.state,
            'direction': 'sent' if friendship.requester_id == current_user else 'received',
            'friend': self._user_data(friend),
            'accepted_at': fields.Datetime.to_string(friendship.accepted_at) if friendship.accepted_at else None,
        }

    @api.model
    def _borrow_data(self, borrow):
        if not borrow:
            return None
        return {
            'id': borrow.id,
            'vehicle_id': borrow.vehicle_id.id,
            'license_plate': borrow.vehicle_id.license_plate,
            'owner': self._user_data(borrow.owner_id),
            'borrower': self._user_data(borrow.borrower_id),
            'valid_from': fields.Datetime.to_string(borrow.valid_from) if borrow.valid_from else None,
            'valid_to': fields.Datetime.to_string(borrow.valid_to) if borrow.valid_to else None,
            'state': borrow.state,
            'active_now': bool(borrow.active_now),
        }

    @api.model
    def _notification_data(self, rec):
        return {
            'id': rec.id,
            'title': rec.name,
            'message': rec.message,
            'category': rec.category,
            'severity': rec.severity,
            'state': rec.state,
            'event_time': fields.Datetime.to_string(rec.event_time) if rec.event_time else None,
            'transaction_uid': rec.transaction_uid or None,
            'parking_event_type': rec.parking_event_type or None,
        }

    @endpoint('NSP Mobile Profile', route_path='mobile/me', methods='GET', code='nsp_mobile_me')
    def api_me(self):
        user, device, session = self._mobile_context()
        return {'data': {
            'user': self._user_data(user),
            'device': {
                'device_uid': device.device_uid, 'platform': device.platform,
                'device_name': device.device_name or None, 'app_version': device.app_version or None,
                'push_provider': device.push_provider, 'push_enabled': bool(device.push_enabled),
            },
            'session_uid': session.session_uid,
        }, 'message': 'OK'}

    @endpoint('NSP Mobile Profile Update', route_path='mobile/me/update', methods='PATCH', code='nsp_mobile_me_update')
    def api_me_update(self):
        user, _device, _session = self._mobile_context()
        body = get_body(self)
        allowed = {'name', 'email', 'phone'}
        unsupported = sorted(set(body) - allowed)
        if unsupported:
            raise ValidationError(_('Unsupported field(s): %s.') % ', '.join(unsupported))
        vals = {}
        for key in allowed:
            if key in body:
                vals[key] = str(body.get(key) or '').strip() or False
        if vals:
            user.sudo().write(vals)
        return {'data': {'user': self._user_data(user)}, 'message': 'OK'}

    @endpoint('NSP Mobile Devices Register', route_path='mobile/devices/register', methods='POST', code='nsp_mobile_device_register')
    def api_device_register(self):
        user, device, _session = self._mobile_context()
        body = get_body(self)
        requested_uid = str(body.get('device_uid') or device.device_uid).strip()
        if requested_uid != device.device_uid:
            raise AccessError(_('A Mobile Token can only update its bound device.'))
        updated = self.env['nsp.mobile.device'].sudo().register_or_update(user, dict(body, device_uid=device.device_uid))
        return {'data': {'device_uid': updated.device_uid, 'push_provider': updated.push_provider, 'push_enabled': bool(updated.push_enabled)}, 'message': 'OK'}

    @endpoint('NSP Mobile Device Heartbeat', route_path='mobile/devices/heartbeat', methods='POST', code='nsp_mobile_device_heartbeat')
    def api_device_heartbeat(self):
        _user, device, _session = self._mobile_context()
        device.touch(sync=True)
        return {'data': {}, 'message': 'OK'}

    @endpoint('NSP Mobile Device Unregister', route_path='mobile/devices/unregister', methods='POST', code='nsp_mobile_device_unregister')
    def api_device_unregister(self):
        _user, device, session = self._mobile_context()
        session.revoke()
        device.sudo().write({'active': False, 'push_enabled': False, 'push_token': False})
        return {'data': {}, 'message': 'OK'}

    @endpoint('NSP Mobile Vehicles', route_path='mobile/vehicles', methods='GET', code='nsp_mobile_vehicles')
    def api_vehicles(self):
        user, _device, _session = self._mobile_context()
        vehicles = self.env['nsp.vehicle'].sudo().search([('owner_id', '=', user.id), ('active', '=', True)], order='license_plate')
        latest = {}
        if vehicles:
            self.env.cr.execute('''
                SELECT DISTINCT ON (vehicle_id) id, vehicle_id
                  FROM nsp_parking_transaction
                 WHERE vehicle_id = ANY(%s) AND status = 'allowed'
                 ORDER BY vehicle_id, event_time DESC, id DESC
            ''', [vehicles.ids])
            tx_ids = [row[0] for row in self.env.cr.fetchall()]
            for tx in self.env['nsp.parking.transaction'].sudo().browse(tx_ids):
                latest[tx.vehicle_id.id] = tx
        now = fields.Datetime.now()
        borrows = self.env['nsp.vehicle.borrow'].sudo().search([
            ('vehicle_id', 'in', vehicles.ids), ('state', '=', 'active'), ('returned_at', '=', False),
            ('valid_from', '<=', now), ('valid_to', '>=', now),
        ]) if vehicles else self.env['nsp.vehicle.borrow']
        borrow_by_vehicle = {rec.vehicle_id.id: rec for rec in borrows}
        return {'data': {'items': [self._vehicle_data(v, latest.get(v.id), borrow_by_vehicle.get(v.id)) for v in vehicles]}, 'message': 'OK'}

    @endpoint('NSP Mobile Vehicle Detail', route_path='mobile/vehicle', methods='GET', code='nsp_mobile_vehicle')
    def api_vehicle(self):
        user, _device, _session = self._mobile_context()
        params = get_params(self)
        try:
            vehicle_id = int(params.get('vehicle_id') or 0)
        except (TypeError, ValueError):
            vehicle_id = 0
        vehicle = self.env['nsp.vehicle'].sudo().search([('id', '=', vehicle_id), ('owner_id', '=', user.id), ('active', '=', True)], limit=1)
        if not vehicle:
            raise AccessError(_('Vehicle not found or not owned by the current user.'))
        latest = self.env['nsp.parking.transaction'].sudo().search([('vehicle_id', '=', vehicle.id), ('status', '=', 'allowed')], order='event_time desc, id desc', limit=1)
        borrow = self.env['nsp.vehicle.borrow'].sudo().find_valid_borrow(vehicle)
        return {'data': self._vehicle_data(vehicle, latest, borrow), 'message': 'OK'}

    @endpoint('NSP Mobile Parking History', route_path='mobile/parking-history', methods='GET', code='nsp_mobile_parking_history')
    def api_parking_history(self):
        user, _device, _session = self._mobile_context()
        params = get_params(self)
        limit, offset = self._pagination(params)
        owned = self.env['nsp.vehicle'].sudo().search([('owner_id', '=', user.id)])
        domain = [('vehicle_id', 'in', owned.ids)]
        if params.get('vehicle_id'):
            try:
                vehicle_id = int(params['vehicle_id'])
            except (TypeError, ValueError):
                raise ValidationError(_('Invalid vehicle_id.'))
            if vehicle_id not in owned.ids:
                raise AccessError(_('Vehicle not found or not owned by the current user.'))
            domain.append(('vehicle_id', '=', vehicle_id))
        Tx = self.env['nsp.parking.transaction'].sudo()
        total = Tx.search_count(domain)
        records = Tx.search(domain, order='event_time desc, id desc', limit=limit, offset=offset)
        return {'data': {'total': total, 'items': [self._transaction_data(rec) for rec in records]}, 'message': 'OK'}

    @endpoint('NSP Mobile Friend Search', route_path='mobile/friends/search', methods='GET', code='nsp_mobile_friend_search')
    def api_friend_search(self):
        user, _device, _session = self._mobile_context()
        q = str(get_params(self).get('q') or '').strip()
        if len(q) < 2:
            return {'data': {'items': []}, 'message': 'OK'}
        candidates = self.env['nsp.user'].sudo().search([
            ('id', '!=', user.id), ('active', '=', True), ('mobile_enabled', '=', True),
            '|', '|', ('name', 'ilike', q), ('email', 'ilike', q), ('phone', 'ilike', q),
        ], limit=20, order='name')
        return {'data': {'items': [self._user_data(rec) for rec in candidates]}, 'message': 'OK'}

    @endpoint('NSP Mobile Friends', route_path='mobile/friends', methods='GET', code='nsp_mobile_friends')
    def api_friends(self):
        user, _device, _session = self._mobile_context()
        friendships = self.env['nsp.user.friendship'].sudo().search([
            ('state', '=', 'accepted'), '|', ('requester_id', '=', user.id), ('addressee_id', '=', user.id),
        ], order='id desc')
        return {'data': {'items': [self._friendship_data(rec, user) for rec in friendships]}, 'message': 'OK'}

    @endpoint('NSP Mobile Friend Requests', route_path='mobile/friends/requests', methods='GET', code='nsp_mobile_friend_requests')
    def api_friend_requests(self):
        user, _device, _session = self._mobile_context()
        records = self.env['nsp.user.friendship'].sudo().search([
            ('state', '=', 'pending'), '|', ('requester_id', '=', user.id), ('addressee_id', '=', user.id),
        ], order='id desc')
        return {'data': {'items': [self._friendship_data(rec, user) for rec in records]}, 'message': 'OK'}

    @endpoint('NSP Mobile Friend Request Create', route_path='mobile/friends/request', methods='POST', code='nsp_mobile_friend_request')
    def api_friend_request(self):
        user, _device, _session = self._mobile_context()
        body = get_body(self)
        friend_id = self._body_int(body, 'friend_id')
        friend = self.env['nsp.user'].sudo().browse(friend_id).exists()
        if not friend or not friend.active or friend == user:
            raise ValidationError(_('Invalid friend_id.'))
        Friendship = self.env['nsp.user.friendship'].sudo()
        pair_key = Friendship._make_pair_key(user.id, friend.id)
        existing = Friendship.search([('pair_key', '=', pair_key)], limit=1)
        if existing:
            raise ValidationError(_('A friend request or friendship already exists with this user.'))
        friendship = Friendship.create({'requester_id': user.id, 'addressee_id': friend.id})
        return {'status_code': 201, 'data': self._friendship_data(friendship, user), 'message': 'Created'}

    @endpoint('NSP Mobile Friend Request Accept', route_path='mobile/friends/accept', methods='POST', code='nsp_mobile_friend_accept')
    def api_friend_accept(self):
        user, _device, _session = self._mobile_context()
        body = get_body(self)
        friendship = self.env['nsp.user.friendship'].sudo().search([
            ('id', '=', self._body_int(body, 'friendship_id')), ('addressee_id', '=', user.id), ('state', '=', 'pending')
        ], limit=1)
        if not friendship:
            raise AccessError(_('Pending friend request not found.'))
        friendship.action_accept()
        return {'data': self._friendship_data(friendship, user), 'message': 'OK'}

    @endpoint('NSP Mobile Friend Cancel', route_path='mobile/friends/cancel', methods='POST', code='nsp_mobile_friend_cancel')
    def api_friend_cancel(self):
        user, _device, _session = self._mobile_context()
        body = get_body(self)
        friendship = self.env['nsp.user.friendship'].sudo().search([
            ('id', '=', self._body_int(body, 'friendship_id')), '|', ('requester_id', '=', user.id), ('addressee_id', '=', user.id)
        ], limit=1)
        if not friendship:
            raise AccessError(_('Friendship not found.'))
        friendship.action_cancel()
        return {'data': {}, 'message': 'OK'}

    @endpoint('NSP Mobile Borrows', route_path='mobile/borrows', methods='GET', code='nsp_mobile_borrows')
    def api_borrows(self):
        user, _device, _session = self._mobile_context()
        records = self.env['nsp.vehicle.borrow'].sudo().search([
            '|', ('vehicle_id.owner_id', '=', user.id), ('borrower_id', '=', user.id)
        ], order='valid_from desc, id desc', limit=200)
        return {'data': {'items': [self._borrow_data(rec) for rec in records]}, 'message': 'OK'}

    @endpoint('NSP Mobile Borrow Create', route_path='mobile/borrows/create', methods='POST', code='nsp_mobile_borrow_create')
    def api_borrow_create(self):
        user, _device, _session = self._mobile_context()
        body = get_body(self)
        vehicle_id = self._body_int(body, 'vehicle_id')
        borrower_id = self._body_int(body, 'borrower_id')
        vehicle = self.env['nsp.vehicle'].sudo().search([('id', '=', vehicle_id), ('owner_id', '=', user.id), ('active', '=', True)], limit=1)
        if not vehicle:
            raise AccessError(_('Vehicle not found or not owned by the current user.'))
        borrower = self.env['nsp.user'].sudo().browse(borrower_id).exists()
        if not borrower or not borrower.active:
            raise ValidationError(_('Borrower not found.'))
        valid_from = self._parse_datetime(body.get('valid_from'), 'valid_from', default=fields.Datetime.now())
        valid_to = self._parse_datetime(body.get('valid_to'), 'valid_to', default=valid_from + timedelta(days=1))
        borrow = self.env['nsp.vehicle.borrow'].sudo().create({
            'vehicle_id': vehicle.id, 'borrower_id': borrower_id,
            'valid_from': valid_from, 'valid_to': valid_to,
        })
        return {'status_code': 201, 'data': self._borrow_data(borrow), 'message': 'Created'}

    @endpoint('NSP Mobile Borrow End', route_path='mobile/borrows/end', methods='POST', code='nsp_mobile_borrow_end')
    def api_borrow_end(self):
        user, _device, _session = self._mobile_context()
        body = get_body(self)
        borrow = self.env['nsp.vehicle.borrow'].sudo().search([
            ('id', '=', self._body_int(body, 'borrow_id')), ('vehicle_id.owner_id', '=', user.id), ('state', '=', 'active')
        ], limit=1)
        if not borrow:
            raise AccessError(_('Active vehicle borrow not found.'))
        borrow.action_return_vehicle()
        return {'data': self._borrow_data(borrow), 'message': 'OK'}

    @endpoint('NSP Mobile Borrow Cancel', route_path='mobile/borrows/cancel', methods='POST', code='nsp_mobile_borrow_cancel')
    def api_borrow_cancel(self):
        user, _device, _session = self._mobile_context()
        body = get_body(self)
        borrow = self.env['nsp.vehicle.borrow'].sudo().search([
            ('id', '=', self._body_int(body, 'borrow_id')), ('vehicle_id.owner_id', '=', user.id)
        ], limit=1)
        if not borrow:
            raise AccessError(_('Vehicle borrow not found.'))
        borrow.action_cancel()
        return {'data': self._borrow_data(borrow), 'message': 'OK'}

    @endpoint('NSP Mobile Notifications', route_path='mobile/notifications', methods='GET', code='nsp_mobile_notifications')
    def api_notifications(self):
        user, _device, _session = self._mobile_context()
        params = get_params(self)
        limit, offset = self._pagination(params)
        domain = [('recipient_user_id', '=', user.id), ('active', '=', True)]
        state = str(params.get('state') or '').strip()
        if state in ('unread', 'read'):
            domain.append(('state', '=', state))
        Notification = self.env['nsp.notification'].sudo()
        total = Notification.search_count(domain)
        records = Notification.search(domain, order='event_time desc, id desc', limit=limit, offset=offset)
        return {'data': {'total': total, 'items': [self._notification_data(rec) for rec in records]}, 'message': 'OK'}

    @endpoint('NSP Mobile Notification Unread Count', route_path='mobile/notifications/unread-count', methods='GET', code='nsp_mobile_notification_unread_count')
    def api_notification_unread_count(self):
        user, _device, _session = self._mobile_context()
        count = self.env['nsp.notification'].sudo().search_count([
            ('recipient_user_id', '=', user.id), ('state', '=', 'unread'), ('active', '=', True)
        ])
        return {'data': {'count': count}, 'message': 'OK'}

    @endpoint('NSP Mobile Notification Read', route_path='mobile/notifications/read', methods='POST', code='nsp_mobile_notification_read')
    def api_notification_read(self):
        user, _device, _session = self._mobile_context()
        body = get_body(self)
        rec = self.env['nsp.notification'].sudo().search([
            ('id', '=', self._body_int(body, 'notification_id')), ('recipient_user_id', '=', user.id), ('active', '=', True)
        ], limit=1)
        if not rec:
            raise AccessError(_('Notification not found.'))
        rec.write({'state': 'read', 'read_at': fields.Datetime.now(), 'read_by': False})
        return {'data': self._notification_data(rec), 'message': 'OK'}

    @endpoint('NSP Mobile Notification Read All', route_path='mobile/notifications/read-all', methods='POST', code='nsp_mobile_notification_read_all')
    def api_notification_read_all(self):
        user, _device, _session = self._mobile_context()
        records = self.env['nsp.notification'].sudo().search([
            ('recipient_user_id', '=', user.id), ('state', '=', 'unread'), ('active', '=', True)
        ])
        if records:
            records.write({'state': 'read', 'read_at': fields.Datetime.now(), 'read_by': False})
        return {'data': {'updated': len(records)}, 'message': 'OK'}

    @endpoint('NSP Mobile Change Password', route_path='mobile/auth/change-password', methods='POST', code='nsp_mobile_change_password')
    def api_change_password(self):
        user, _device, _session = self._mobile_context()
        body = get_body(self)
        current_password = str(body.get('current_password') or '')
        new_password = str(body.get('new_password') or '')
        if not user.check_mobile_password(current_password):
            raise AccessError(_('Current password is incorrect.'))
        user._set_mobile_password(new_password)
        # Revoke every other session after a credential change; current session remains valid.
        current_session_uid = self.env.context.get('core_api_session_uid')
        other_sessions = self.env['nsp.mobile.session'].sudo().search([
            ('user_id', '=', user.id), ('state', '=', 'active'), ('session_uid', '!=', current_session_uid),
        ])
        if other_sessions:
            other_sessions.revoke()
        return {'data': {}, 'message': 'OK'}

    @endpoint('NSP Mobile Realtime Events', route_path='mobile/realtime/events', methods='GET', code='nsp_mobile_realtime_events')
    def api_realtime_events(self):
        user, device, _session = self._mobile_context()
        params = get_params(self)
        try:
            after_id = max(0, int(params.get('after_id') or 0))
            limit = min(max(1, int(params.get('limit') or 50)), 100)
        except (TypeError, ValueError):
            raise ValidationError(_('Invalid realtime cursor.'))
        Delivery = self.env['nsp.notification.delivery'].sudo()
        deliveries = Delivery.search([
            ('recipient_user_id', '=', user.id), ('device_uid', '=', device.device_uid),
            ('channel', '=', 'realtime'), ('notification_id', '>', after_id),
            ('state', 'in', ['pending', 'sent']),
        ], order='notification_id asc, id asc', limit=limit)
        items = [self._notification_data(rec.notification_id) for rec in deliveries]
        if deliveries:
            deliveries.mark_delivered()
            device.touch(sync=True)
        cursor = max([item['id'] for item in items], default=after_id)
        return {'data': {'cursor': cursor, 'items': items}, 'message': 'OK'}
