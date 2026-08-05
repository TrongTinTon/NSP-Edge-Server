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
        if ctx.get('core_api_subject_model') != 'res.users' or not ctx.get('core_api_subject_id'):
            raise AccessError(_('Mobile Token has no valid Odoo User binding.'))

        odoo_user = self.env['res.users'].sudo().browse(
            int(ctx['core_api_subject_id'])
        ).exists()
        if not odoo_user or not odoo_user.active or self.env.uid != odoo_user.id:
            raise AccessError(_('Odoo User is inactive or no longer authorized.'))

        mapped_user = self.env['nsp.user'].sudo().search([
            ('odoo_user_id', '=', odoo_user.id),
            ('active', '=', True),
        ])
        if len(mapped_user) != 1:
            raise AccessError(_(
                'The authenticated Odoo User must have exactly one active NSP User profile.'
            ))
        user = self.env['nsp.user'].search([
            ('id', '=', mapped_user.id),
            ('active', '=', True),
        ])
        if len(user) != 1:
            raise AccessError(_(
                'The authenticated Odoo User cannot access its NSP User profile.'
            ))

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
        return user, odoo_user, device, session

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
        }
        if include_contact:
            data.update({
                'email': user.email or None,
                'phone': user.phone or None,
            })
        return data

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
            'parking_area': tx.parking_area_id.name if tx.parking_area_id else tx.parking_area_code or None,
            'lane': tx.lane_id.name if tx.lane_id else tx.lane_code or None,
            'license_plate': tx.license_plate or (tx.vehicle_id.license_plate if tx.vehicle_id else None) or tx.vehicle_tid or None,
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

    @api.model
    def _cleanup_obsolete_routes(self):
        """Keep the persisted Mobile route catalogue identical to code metadata."""
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
        user, odoo_user, device, session = self._mobile_context()
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
                'user': {
                    **self._user_data(user, include_contact=True),
                    'odoo_user_id': odoo_user.id,
                    'login': odoo_user.login,
                },
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
        user, _odoo_user, device, session = self._mobile_context()
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
        _user, _odoo_user, device, _session = self._mobile_context()
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
        'NSP Mobile Vehicles',
        route_path='mobile/vehicles',
        methods='GET',
        code='nsp_mobile_vehicles',
    )
    def api_vehicles(self):
        user, _odoo_user, _device, _session = self._mobile_context()
        vehicles = self.env['nsp.vehicle'].search([
            ('owner_id', '=', user.id),
            ('active', '=', True),
        ], order='license_plate')
        latest = {}
        if vehicles:
            transactions = self.env['nsp.parking.transaction'].search([
                ('vehicle_id', 'in', vehicles.ids),
                ('status', '=', 'allowed'),
            ], order='vehicle_id, event_time desc, id desc')
            for transaction in transactions:
                latest.setdefault(transaction.vehicle_id.id, transaction)
        now = fields.Datetime.now()
        borrows = self.env['nsp.vehicle.borrow'].search([
            ('vehicle_id', 'in', vehicles.ids),
            ('state', '=', 'active'),
            ('returned_at', '=', False),
            ('valid_from', '<=', now),
            ('valid_to', '>=', now),
        ]) if vehicles else self.env['nsp.vehicle.borrow']
        borrow_by_vehicle = {rec.vehicle_id.id: rec for rec in borrows}
        return {
            'data': {
                'items': [
                    self._vehicle_data(
                        vehicle,
                        latest.get(vehicle.id),
                        borrow_by_vehicle.get(vehicle.id),
                    )
                    for vehicle in vehicles
                ]
            },
            'message': 'OK',
        }

    @endpoint(
        'NSP Mobile Vehicle Detail',
        route_path='mobile/vehicles/detail',
        methods='GET',
        code='nsp_mobile_vehicle_detail',
    )
    def api_vehicle_detail(self):
        user, _odoo_user, _device, _session = self._mobile_context()
        params = get_params(self)
        try:
            vehicle_id = int(params.get('vehicle_id') or 0)
        except (TypeError, ValueError):
            vehicle_id = 0
        vehicle = self.env['nsp.vehicle'].search([
            ('id', '=', vehicle_id),
            ('owner_id', '=', user.id),
            ('active', '=', True),
        ], limit=1)
        if not vehicle:
            raise AccessError(_('Vehicle not found or not owned by the current user.'))
        latest = self.env['nsp.parking.transaction'].search([
            ('vehicle_id', '=', vehicle.id),
            ('status', '=', 'allowed'),
        ], order='event_time desc, id desc', limit=1)
        borrow = self.env['nsp.vehicle.borrow'].find_valid_borrow(vehicle)
        return {'data': self._vehicle_data(vehicle, latest, borrow), 'message': 'OK'}

    @endpoint(
        'NSP Mobile Parking Transactions',
        route_path='mobile/parking/transactions',
        methods='GET',
        code='nsp_mobile_parking_transactions',
    )
    def api_parking_transactions(self):
        user, _odoo_user, _device, _session = self._mobile_context()
        params = get_params(self)
        limit, offset = self._pagination(params)
        owned = self.env['nsp.vehicle'].search([('owner_id', '=', user.id)])
        domain = [('vehicle_id', 'in', owned.ids)]
        if params.get('vehicle_id'):
            try:
                vehicle_id = int(params['vehicle_id'])
            except (TypeError, ValueError):
                raise ValidationError(_('Invalid vehicle_id.'))
            if vehicle_id not in owned.ids:
                raise AccessError(_('Vehicle not found or not owned by the current user.'))
            domain.append(('vehicle_id', '=', vehicle_id))
        Tx = self.env['nsp.parking.transaction']
        total = Tx.search_count(domain)
        records = Tx.search(
            domain,
            order='event_time desc, id desc',
            limit=limit,
            offset=offset,
        )
        return {
            'data': {
                'total': total,
                'items': [self._transaction_data(rec) for rec in records],
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
        user, _odoo_user, _device, _session = self._mobile_context()
        q = str(get_params(self).get('q') or '').strip()
        if len(q) < 2:
            return {'data': {'items': []}, 'message': 'OK'}
        candidates = self.env['nsp.user'].search([
            ('id', '!=', user.id),
            ('active', '=', True),
            '|', '|',
            ('name', 'ilike', q),
            ('email', 'ilike', q),
            ('phone', 'ilike', q),
        ], limit=20, order='name')
        return {
            'data': {'items': [self._user_data(rec) for rec in candidates]},
            'message': 'OK',
        }

    @endpoint(
        'NSP Mobile Friends',
        route_path='mobile/friends',
        methods='GET',
        code='nsp_mobile_friends',
    )
    def api_friends(self):
        user, _odoo_user, _device, _session = self._mobile_context()
        friendships = self.env['nsp.user.friendship'].search([
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
        user, _odoo_user, _device, _session = self._mobile_context()
        method = self._request_method()
        if method == 'GET':
            records = self.env['nsp.user.friendship'].search([
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
        friend = self.env['nsp.user'].browse(friend_id).exists()
        if not friend or not friend.active or friend == user:
            raise ValidationError(_('Invalid friend_id.'))
        Friendship = self.env['nsp.user.friendship']
        pair_key = Friendship._make_pair_key(user.id, friend.id)
        existing = Friendship.search([('pair_key', '=', pair_key)], limit=1)
        if existing:
            raise ValidationError(
                _('A friend request or friendship already exists with this user.')
            )
        friendship = Friendship.create({
            'requester_id': user.id,
            'addressee_id': friend.id,
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
        user, _odoo_user, _device, _session = self._mobile_context()
        body = get_body(self)
        self._validate_body_fields(body, {'friendship_id'})
        friendship = self.env['nsp.user.friendship'].search([
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
        user, _odoo_user, _device, _session = self._mobile_context()
        body = get_body(self)
        self._validate_body_fields(body, {'friendship_id'})
        friendship = self.env['nsp.user.friendship'].search([
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
        user, _odoo_user, _device, _session = self._mobile_context()
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
        vehicle = self.env['nsp.vehicle'].search([
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
        user, _odoo_user, _device, _session = self._mobile_context()
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
        user, _odoo_user, _device, _session = self._mobile_context()
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
        user, _odoo_user, _device, _session = self._mobile_context()
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
        user, _odoo_user, _device, _session = self._mobile_context()
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
        user, _odoo_user, _device, _session = self._mobile_context()
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
            'read_by': self.env.user.id,
        })
        return {'data': self._notification_data(rec), 'message': 'OK'}

    @endpoint(
        'NSP Mobile Notification Read All',
        route_path='mobile/notifications/read-all',
        methods='POST',
        code='nsp_mobile_notification_read_all',
    )
    def api_notification_read_all(self):
        user, _odoo_user, _device, _session = self._mobile_context()
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
                'read_by': self.env.user.id,
            })
        return {'data': {'updated': len(records)}, 'message': 'OK'}

    @endpoint(
        'NSP Mobile Password',
        route_path='mobile/auth/password',
        methods='PATCH',
        code='nsp_mobile_auth_password',
    )
    def api_auth_password(self):
        _user, odoo_user, _device, _session = self._mobile_context()
        body = get_body(self)
        self._validate_body_fields(body, {'current_password', 'new_password'})
        current_password = str(body.get('current_password') or '')
        new_password = str(body.get('new_password') or '')
        if not current_password:
            raise ValidationError(_('current_password is required.'))
        if len(new_password) < 8:
            raise ValidationError(_('New password must contain at least 8 characters.'))
        try:
            odoo_user.with_user(odoo_user).change_password(
                current_password,
                new_password,
            )
        except AccessDenied as exc:
            raise AccessError(_('Current password is incorrect.')) from exc
        return {
            'data': {'reauthenticate': True},
            'message': 'Password changed. Sign in again with the Odoo User password.',
        }

    @endpoint(
        'NSP Mobile Notification Events',
        route_path='mobile/notifications/events',
        methods='GET',
        code='nsp_mobile_notification_events',
    )
    def api_notification_events(self):
        user, _odoo_user, device, _session = self._mobile_context()
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
