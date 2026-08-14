# -*- coding: utf-8 -*-
from datetime import timedelta
from odoo import api, fields, models, _
from odoo.exceptions import AccessDenied, AccessError, ValidationError
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
            raise AccessError(_('Mobile Token has no valid NSP User binding.'))

        user = self.env['nsp.user'].sudo().browse(
            int(ctx['core_api_subject_id'])
        ).exists()
        
        if not user or not user.active:
            raise AccessError(_('NSP User is inactive or no longer authorized.'))

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
        if not session or not device:
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
    def _user_data(self, user, include_contact=False):
        data = {
            'id': user.id,
            'name': user.name,
            'rfid_tid': user.rfid_tid if hasattr(user, 'rfid_tid') and user.rfid_tid else '',
        }
        if include_contact:
            data.update({
                'email': user.email or None,
                'phone': user.phone or None,
            })
        return data


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
    def _parking_event_badge(self, event_type):
        badges = {
            'check_in': {'key': 'check_in', 'label': 'Check-in', 'tone': 'success'},
            'check_out': {'key': 'check_out', 'label': 'Check-out', 'tone': 'info'},
        }
        return badges.get(event_type)

    @api.model
    def _notification_data(self, rec):
        event_type = rec.parking_event_type or None
        return {
            'id': rec.id,
            'title': rec.name,
            'message': rec.message,
            'category': rec.category,
            'severity': rec.severity,
            'state': rec.state,
            'event_time': fields.Datetime.to_string(rec.event_time) if rec.event_time else None,
            'parking_log_uid': rec.transaction_uid or None,
            'parking_event_type': event_type,
            'parking_event_badge': self._parking_event_badge(event_type),
        }

    @api.model
    def _cleanup_obsolete_routes(self):
        manager = self.env.ref(
            'nsp_mobile.action_endpoint_manager_nsp_mobile',
            raise_if_not_found=False,
        )
        application = self.env.ref(
            'nsp_mobile.core_api_application_nsp_mobile',
            raise_if_not_found=False,
        )
        if not manager or not application:
            return True

        valid_codes = {
            manager._endpoint_meta(method_name, func)[0]
            for method_name, func in manager._get_endpoint_methods()
        }
        obsolete_endpoints = self.env['core.api.endpoint'].sudo().search([
            ('endpoint_manager_id', '=', manager.id),
            ('application_id', '=', application.id),
            ('code', 'not in', list(valid_codes)),
        ])
        if obsolete_endpoints:
            obsolete_endpoints.unlink()

        obsolete_actions = self.env['ir.actions.core_api'].sudo().search([
            ('endpoint_manager_id', '=', manager.id),
            ('endpoint_code', 'not in', list(valid_codes)),
        ])
        if obsolete_actions:
            obsolete_actions.unlink()
        return True

    @api.model
    def _request_method(self):
        return str(self.env.context.get('core_api_method') or 'GET').strip().upper()

    @api.model
    def _validate_body_fields(self, body, allowed):
        unsupported = sorted(set(body) - set(allowed))
        if unsupported:
            raise ValidationError(_('Unsupported field(s): %s.') % ', '.join(unsupported))

    @api.model
    def _device_data(self, device):
        return {
            'device_uid': device.device_uid,
            'platform': device.platform,
            'device_name': device.device_name or None,
            'app_version': device.app_version or None,
            'push_provider': device.push_provider,
            'push_enabled': bool(device.push_enabled),
            'last_seen_at': (
                fields.Datetime.to_string(device.last_seen_at)
                if device.last_seen_at else None
            ),
            'last_sync_at': (
                fields.Datetime.to_string(device.last_sync_at)
                if device.last_sync_at else None
            ),
        }

    @endpoint(
        'NSP Mobile Profile',
        route_path='mobile/profile',
        methods='GET,PATCH',
        code='nsp_mobile_profile',
    )
    def api_profile(self):
        user, device, session = self._mobile_context()
        method = self._request_method()

        if method == 'PATCH':
            body = get_body(self)
            allowed = {'name', 'email', 'phone'}
            self._validate_body_fields(body, allowed)
            vals = {
                key: str(body.get(key) or '').strip() or False
                for key in allowed
                if key in body
            }
            if vals:
                user.write(vals)

        return {
            'data': {
                'user': self._user_data(user, include_contact=True),
                'device': self._device_data(device),
                'session_uid': session.session_uid,
            },
            'message': 'OK',
        }

    @endpoint(
        'NSP Mobile Current Device',
        route_path='mobile/device',
        methods='GET,PATCH,DELETE',
        code='nsp_mobile_device',
    )
    def api_device(self):
        user, device, session = self._mobile_context()
        method = self._request_method()

        if method == 'PATCH':
            body = get_body(self)
            allowed = {
                'device_uid', 'platform', 'device_name', 'app_version',
                'push_provider', 'push_token', 'push_enabled',
            }
            self._validate_body_fields(body, allowed)
            requested_uid = str(body.get('device_uid') or device.device_uid).strip()
            if requested_uid != device.device_uid:
                raise AccessError(_('A Mobile Token can only update its bound device.'))
            merged = {
                'device_uid': device.device_uid,
                'platform': body.get('platform', device.platform),
                'device_name': body.get('device_name', device.device_name),
                'app_version': body.get('app_version', device.app_version),
                'push_provider': body.get('push_provider', device.push_provider),
                'push_token': body.get('push_token', device.push_token),
                'push_enabled': body.get('push_enabled', device.push_enabled),
            }
            device = self.env['nsp.mobile.device'].sudo().register_or_update(
                user.sudo(), merged
            )
        elif method == 'DELETE':
            session.revoke()
            device.sudo().write({
                'active': False,
                'push_enabled': False,
                'push_token': False,
            })
            return {'data': {'unregistered': True}, 'message': 'OK'}

        return {'data': self._device_data(device), 'message': 'OK'}

    @endpoint(
        'NSP Mobile Device Heartbeat',
        route_path='mobile/device/heartbeat',
        methods='POST',
        code='nsp_mobile_device_heartbeat',
    )
    def api_device_heartbeat(self):
        user, device, session = self._mobile_context()
        body = get_body(self)
        self._validate_body_fields(body, set())
        device.touch(sync=True)
        return {
            'data': {
                'device_uid': device.device_uid,
                'last_sync_at': fields.Datetime.to_string(device.last_sync_at),
            },
            'message': 'OK',
        }


    @endpoint(
        'NSP Mobile Friend Search',
        route_path='mobile/friends/search',
        methods='GET',
        code='nsp_mobile_friend_search',
    )
    def api_friend_search(self):
        user, device, session = self._mobile_context()
        q = str(get_params(self).get('q') or '').strip()
        
        if len(q) < 2:
            return {'data': {'items': []}, 'message': 'OK'}
            
        candidates = self.env['nsp.user'].sudo().search([
            ('id', '!=', user.id),
            ('active', '=', True),
            '|', '|',
            ('name', 'ilike', q),
            ('email', 'ilike', q),
            ('phone', 'ilike', q),
        ], limit=20, order='name')
        
        candidate_ids = candidates.ids
        friendships = self.env['nsp.user.friendship'].sudo().search([
            '|',
            '&', ('requester_id', '=', user.id), ('addressee_id', 'in', candidate_ids),
            '&', ('requester_id', 'in', candidate_ids), ('addressee_id', '=', user.id),
        ])
        status_map = {}
        for f in friendships:
            other_id = f.addressee_id.id if f.requester_id.id == user.id else f.requester_id.id
            status_map[other_id] = f.state

        items = []
        for rec in candidates:
            data = self._user_data(rec, include_contact=True)
            # Gắn thêm trạng thái: 'none' (chưa kết bạn), 'pending' (chờ duyệt), 'accepted' (đã là bạn)
            data['friendship_status'] = status_map.get(rec.id, 'none')
            items.append(data)
        
        return {
            'data': {'items': items},
            'message': 'OK',
        }

    @endpoint(
        'NSP Mobile Friends',
        route_path='mobile/friends',
        methods='GET',
        code='nsp_mobile_friends',
    )
    def api_friends(self):
        user, device, session = self._mobile_context()
        friendships = self.env['nsp.user.friendship'].sudo().search([
            ('state', '=', 'accepted'),
            '|',
            ('requester_id', '=', user.id),
            ('addressee_id', '=', user.id),
        ], order='id desc')
        return {
            'data': {
                'items': [self._friendship_data(rec, user) for rec in friendships]
            },
            'message': 'OK',
        }

    @endpoint(
        'NSP Mobile Friend Requests',
        route_path='mobile/friend-requests',
        methods='GET,POST',
        code='nsp_mobile_friend_requests',
    )
    def api_friend_requests(self):
        user, device, session = self._mobile_context()
        method = self._request_method()
        if method == 'GET':
            records = self.env['nsp.user.friendship'].sudo().search([
                ('state', '=', 'pending'),
                '|',
                ('requester_id', '=', user.id),
                ('addressee_id', '=', user.id),
            ], order='id desc')
            return {
                'data': {
                    'items': [self._friendship_data(rec, user) for rec in records]
                },
                'message': 'OK',
            }

        body = get_body(self)
        self._validate_body_fields(body, {'friend_id'})
        friend_id = self._body_int(body, 'friend_id')
        friend = self.env['nsp.user'].sudo().browse(friend_id).exists()
        if not friend or not friend.active or friend == user:
            raise ValidationError(_('Invalid friend_id.'))
        Friendship = self.env['nsp.user.friendship'].sudo()
        pair_key = Friendship._make_pair_key(user.id, friend.id)
        existing = Friendship.search([
            '|',
            '&', ('requester_id', '=', user.id), ('addressee_id', '=', friend.id),
            '&', ('requester_id', '=', friend.id), ('addressee_id', '=', user.id),
        ], limit=1)
        if existing:
            raise ValidationError(
                _('A friend request or friendship already exists with this user.')
            )
        friendship = Friendship.sudo().create({
            'requester_id': user.id,
            'addressee_id': friend.id,
        })

        self.env['nsp.notification'].sudo().create({
            'recipient_user_id': friend.id,
            'name': 'Lời mời kết bạn',
            'message': f'{user.name} đã gửi cho bạn một lời mời kết bạn.',
            'category': 'system', 
            'severity': 'info',
            'state': 'unread',
        })
        
        return {
            'status_code': 201,
            'data': self._friendship_data(friendship, user),
            'message': 'Created',
        }

    @endpoint(
        'NSP Mobile Friend Request Accept',
        route_path='mobile/friend-requests/accept',
        methods='POST',
        code='nsp_mobile_friend_request_accept',
    )
    def api_friend_request_accept(self):
        user, device, session = self._mobile_context()
        body = get_body(self)
        self._validate_body_fields(body, {'friendship_id'})
        friendship = self.env['nsp.user.friendship'].sudo().search([
            ('id', '=', self._body_int(body, 'friendship_id')),
            ('addressee_id', '=', user.id),
            ('state', '=', 'pending'),
        ], limit=1)
        if not friendship:
            raise AccessError(_('Pending friend request not found.'))
        friendship.action_accept()
        return {'data': self._friendship_data(friendship, user), 'message': 'OK'}

    @endpoint(
        'NSP Mobile Friend Request Cancel',
        route_path='mobile/friend-requests/cancel',
        methods='POST',
        code='nsp_mobile_friend_request_cancel',
    )
    def api_friend_request_cancel(self):
        user, device, session = self._mobile_context()
        body = get_body(self)
        self._validate_body_fields(body, {'friendship_id'})
        friendship = self.env['nsp.user.friendship'].sudo().search([
            ('id', '=', self._body_int(body, 'friendship_id')),
            '|',
            ('requester_id', '=', user.id),
            ('addressee_id', '=', user.id),
        ], limit=1)
        if not friendship:
            raise AccessError(_('Friend request or friendship not found.'))
        friendship.action_cancel()
        return {'data': {'cancelled': True}, 'message': 'OK'}

    @endpoint(
        'NSP Mobile Vehicle Borrows',
        route_path='mobile/vehicle-borrows',
        methods='GET,POST',
        code='nsp_mobile_vehicle_borrows',
    )
    def api_vehicle_borrows(self):
        user, device, session = self._mobile_context()
        method = self._request_method()
        if method == 'GET':
            records = self.env['nsp.vehicle.borrow'].search([
                '|',
                ('vehicle_id.owner_id', '=', user.id),
                ('borrower_id', '=', user.id),
            ], order='valid_from desc, id desc', limit=200)
            return {
                'data': {'items': [self._borrow_data(rec) for rec in records]},
                'message': 'OK',
            }

        body = get_body(self)
        self._validate_body_fields(
            body,
            {'vehicle_id', 'borrower_id', 'valid_from', 'valid_to'},
        )
        vehicle_id = self._body_int(body, 'vehicle_id')
        borrower_id = self._body_int(body, 'borrower_id')
        vehicle = self.env['nsp.vehicle'].sudo().search([
            ('id', '=', vehicle_id),
            ('owner_id', '=', user.id),
            ('active', '=', True),
        ], limit=1)
        if not vehicle:
            raise AccessError(_('Vehicle not found or not owned by the current user.'))
        borrower = self.env['nsp.user'].browse(borrower_id).exists()
        if not borrower or not borrower.active:
            raise ValidationError(_('Borrower not found.'))
        valid_from = self._parse_datetime(
            body.get('valid_from'),
            'valid_from',
            default=fields.Datetime.now(),
        )
        valid_to = self._parse_datetime(
            body.get('valid_to'),
            'valid_to',
            default=valid_from + timedelta(days=1),
        )
        borrow = self.env['nsp.vehicle.borrow'].create({
            'vehicle_id': vehicle.id,
            'borrower_id': borrower.id,
            'valid_from': valid_from,
            'valid_to': valid_to,
        })
        return {
            'status_code': 201,
            'data': self._borrow_data(borrow),
            'message': 'Created',
        }

    @endpoint(
        'NSP Mobile Vehicle Borrow End',
        route_path='mobile/vehicle-borrows/end',
        methods='POST',
        code='nsp_mobile_vehicle_borrow_end',
    )
    def api_vehicle_borrow_end(self):
        user, device, session = self._mobile_context()
        body = get_body(self)
        self._validate_body_fields(body, {'borrow_id'})
        borrow = self.env['nsp.vehicle.borrow'].search([
            ('id', '=', self._body_int(body, 'borrow_id')),
            ('vehicle_id.owner_id', '=', user.id),
            ('state', '=', 'active'),
        ], limit=1)
        if not borrow:
            raise AccessError(_('Active vehicle borrow not found.'))
        borrow.action_return_vehicle()
        return {'data': self._borrow_data(borrow), 'message': 'OK'}

    @endpoint(
        'NSP Mobile Vehicle Borrow Cancel',
        route_path='mobile/vehicle-borrows/cancel',
        methods='POST',
        code='nsp_mobile_vehicle_borrow_cancel',
    )
    def api_vehicle_borrow_cancel(self):
        user, device, session = self._mobile_context()
        body = get_body(self)
        self._validate_body_fields(body, {'borrow_id'})
        borrow = self.env['nsp.vehicle.borrow'].search([
            ('id', '=', self._body_int(body, 'borrow_id')),
            ('vehicle_id.owner_id', '=', user.id),
        ], limit=1)
        if not borrow:
            raise AccessError(_('Vehicle borrow not found.'))
        borrow.action_cancel()
        return {'data': self._borrow_data(borrow), 'message': 'OK'}

    @endpoint(
        'NSP Mobile Notifications',
        route_path='mobile/notifications',
        methods='GET',
        code='nsp_mobile_notifications',
    )
    def api_notifications(self):
        user, device, session = self._mobile_context()
        params = get_params(self)
        limit, offset = self._pagination(params)
        domain = [('recipient_user_id', '=', user.id), ('active', '=', True)]
        state = str(params.get('state') or '').strip()
        if state in ('unread', 'read'):
            domain.append(('state', '=', state))
        Notification = self.env['nsp.notification']
        total = Notification.search_count(domain)
        records = Notification.search(
            domain,
            order='event_time desc, id desc',
            limit=limit,
            offset=offset,
        )
        return {
            'data': {
                'total': total,
                'items': [self._notification_data(rec) for rec in records],
            },
            'message': 'OK',
        }

    @endpoint(
        'NSP Mobile Notification Unread Count',
        route_path='mobile/notifications/unread-count',
        methods='GET',
        code='nsp_mobile_notification_unread_count',
    )
    def api_notification_unread_count(self):
        user, device, session = self._mobile_context()
        count = self.env['nsp.notification'].search_count([
            ('recipient_user_id', '=', user.id),
            ('state', '=', 'unread'),
            ('active', '=', True),
        ])
        return {'data': {'count': count}, 'message': 'OK'}

    @endpoint(
        'NSP Mobile Notification Read',
        route_path='mobile/notifications/read',
        methods='POST',
        code='nsp_mobile_notification_read',
    )
    def api_notification_read(self):
        user, device, session = self._mobile_context()
        body = get_body(self)
        self._validate_body_fields(body, {'notification_id'})
        rec = self.env['nsp.notification'].search([
            ('id', '=', self._body_int(body, 'notification_id')),
            ('recipient_user_id', '=', user.id),
            ('active', '=', True),
        ], limit=1)
        if not rec:
            raise AccessError(_('Notification not found.'))
        rec.write({
            'state': 'read',
            'read_at': fields.Datetime.now(),
            'read_by': self.env.user.id if self.env.user else False,
        })
        return {'data': self._notification_data(rec), 'message': 'OK'}

    @endpoint(
        'NSP Mobile Notification Read All',
        route_path='mobile/notifications/read-all',
        methods='POST',
        code='nsp_mobile_notification_read_all',
    )
    def api_notification_read_all(self):
        user, device, session = self._mobile_context()
        body = get_body(self)
        self._validate_body_fields(body, set())
        records = self.env['nsp.notification'].search([
            ('recipient_user_id', '=', user.id),
            ('state', '=', 'unread'),
            ('active', '=', True),
        ])
        if records:
            records.write({
                'state': 'read',
                'read_at': fields.Datetime.now(),
                'read_by': self.env.user.id if self.env.user else False,
            })
        return {'data': {'updated': len(records)}, 'message': 'OK'}

    @endpoint(
        'NSP Mobile Password',
        route_path='mobile/auth/password',
        methods='PATCH',
        code='nsp_mobile_auth_password',
    )
    def api_auth_password(self):
        user, _device, _session = self._mobile_context()
        body = get_body(self)
        self._validate_body_fields(body, {'current_password', 'new_password'})
        current_password = str(body.get('current_password') or '')
        new_password = str(body.get('new_password') or '')
        if not current_password:
            raise ValidationError(_('current_password is required.'))
        if len(new_password) < 8:
            raise ValidationError(_('New password must contain at least 8 characters.'))

        odoo_user = user.odoo_user_id.sudo().exists()
        if not odoo_user or not odoo_user.active:
            raise AccessError(_('The linked Odoo User is inactive or unavailable.'))
        try:
            odoo_user.with_user(odoo_user).change_password(
                current_password,
                new_password,
            )
        except AccessDenied as exc:
            raise AccessError(_('Current password is incorrect.')) from exc
        return {
            'data': {'reauthenticate': True},
            'message': 'Password changed. Sign in again with the new password.',
        }

    @endpoint(
        'NSP Mobile Notification Events',
        route_path='mobile/notifications/events',
        methods='GET',
        code='nsp_mobile_notification_events',
    )
    def api_notification_events(self):
        user, device, session = self._mobile_context()
        params = get_params(self)
        try:
            after_id = max(0, int(params.get('after_id') or 0))
            limit = min(max(1, int(params.get('limit') or 50)), 100)
        except (TypeError, ValueError):
            raise ValidationError(_('Invalid notification cursor.'))
        Delivery = self.env['nsp.notification.delivery'].sudo()
        deliveries = Delivery.search([
            ('recipient_user_id', '=', user.id),
            ('device_uid', '=', device.device_uid),
            ('channel', '=', 'realtime'),
            ('notification_id', '>', after_id),
            ('state', 'in', ['pending', 'sent']),
        ], order='notification_id asc, id asc', limit=limit)
        items = [self._notification_data(rec.notification_id) for rec in deliveries]
        if deliveries:
            deliveries.mark_delivered()
            device.touch(sync=True)
        cursor = max([item['id'] for item in items], default=after_id)
        return {'data': {'cursor': cursor, 'items': items}, 'message': 'OK'}


