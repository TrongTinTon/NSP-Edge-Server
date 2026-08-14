# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from odoo.addons.t4_coreapi.utils.core_api_utils import endpoint, get_body, get_params


class NspMobileVehicleApi(models.Model):
    _inherit = 'nsp.mobile.api.service'

    @api.model
    def _vehicle_data(self, vehicle, latest_parking_log=None, active_borrow=None):
        return {
            'id': vehicle.id,
            'vehicle_code': vehicle.vehicle_code or None,
            'license_plate': vehicle.license_plate,
            'vehicle_type_id': vehicle.vehicle_type_id.id if vehicle.vehicle_type_id else None,
            'vehicle_type': vehicle.vehicle_type_id.name if vehicle.vehicle_type_id else None,
            'brand_id': vehicle.brand_id.id if vehicle.brand_id else None,
            'brand': vehicle.brand_id.name if vehicle.brand_id else None,
            'model_id': vehicle.model_id.id if vehicle.model_id else None,
            'model': vehicle.model_id.name if vehicle.model_id else None,
            'color_id': vehicle.color_id.id if vehicle.color_id else None,
            'color': vehicle.color_id.name if vehicle.color_id else None,
            'active': bool(vehicle.active),
            'parking_status': self._parking_status(latest_parking_log),
            'last_parking_event': self._parking_log_data(latest_parking_log),
            'active_borrow': self._borrow_data(active_borrow),
        }

    @api.model
    def _parking_status(self, parking_log):
        return (
            'inside'
            if parking_log
            and parking_log.decision == 'allowed'
            and parking_log.event_type == 'check_in'
            else 'outside'
        )

    @api.model
    def _parking_log_data(self, parking_log):
        if not parking_log:
            return None
        vehicle = parking_log.vehicle_id
        return {
            'id': parking_log.id,
            'log_uid': parking_log.log_uid,
            'event_time': (
                fields.Datetime.to_string(parking_log.event_time)
                if parking_log.event_time else None
            ),
            'event_type': parking_log.event_type,
            'decision': parking_log.decision,
            'reason_code': parking_log.reason_code or None,
            'parking_area_id': parking_log.parking_area_id.id if parking_log.parking_area_id else None,
            'parking_area': parking_log.parking_area_id.name if parking_log.parking_area_id else None,
            'lane_id': parking_log.lane_id.id if parking_log.lane_id else None,
            'lane': parking_log.lane_id.name if parking_log.lane_id else None,
            'layout_revision': parking_log.layout_revision or 0,
            'vehicle_id': vehicle.id if vehicle else None,
            'license_plate': (
                (vehicle.license_plate if vehicle else None)
                or parking_log.vehicle_tid
                or None
            ),
            'vehicle_tid': parking_log.vehicle_tid or None,
            'user_id': parking_log.user_id.id if parking_log.user_id else None,
            'user': parking_log.user_id.name if parking_log.user_id else None,
            'user_tid': parking_log.user_tid or None,
            'borrow_id': parking_log.borrow_id.id if parking_log.borrow_id else None,
        }

    @api.model
    def _owned_vehicles(self, user, active=None):
        domain = [('owner_id', '=', user.id)]
        if active is not None:
            domain.append(('active', '=', bool(active)))
        return self.env['nsp.vehicle'].sudo().search(domain, order='license_plate, id')

    @api.model
    def _latest_allowed_parking_logs(self, vehicles):
        if not vehicles:
            return {}
        logs = self.env['nsp.parking.log'].sudo().search([
            ('vehicle_id', 'in', vehicles.ids),
            ('decision', '=', 'allowed'),
        ], order='vehicle_id, event_time desc, id desc')
        latest_by_vehicle = {}
        for parking_log in logs:
            latest_by_vehicle.setdefault(parking_log.vehicle_id.id, parking_log)
        return latest_by_vehicle

    @api.model
    def _active_borrows_by_vehicle(self, vehicles):
        if not vehicles:
            return {}
        now = fields.Datetime.now()
        borrows = self.env['nsp.vehicle.borrow'].sudo().search([
            ('vehicle_id', 'in', vehicles.ids),
            ('state', '=', 'active'),
            ('returned_at', '=', False),
            ('valid_from', '<=', now),
            ('valid_to', '>=', now),
        ])
        return {borrow.vehicle_id.id: borrow for borrow in borrows}

    @api.model
    def _owned_vehicle_from_params(self, user, params, required=False):
        raw_vehicle_id = params.get('vehicle_id')
        if not raw_vehicle_id:
            if required:
                raise ValidationError(_('vehicle_id is required.'))
            return self.env['nsp.vehicle'].browse()
        try:
            vehicle_id = int(raw_vehicle_id)
        except (TypeError, ValueError):
            raise ValidationError(_('Invalid vehicle_id.')) from None
        vehicle = self.env['nsp.vehicle'].sudo().search([
            ('id', '=', vehicle_id),
            ('owner_id', '=', user.id),
        ], limit=1)
        if not vehicle:
            raise AccessError(_('Vehicle not found or not owned by the current user.'))
        return vehicle

    @api.model
    def _active_reference(self, model_name, record_id, label, extra_domain=None):
        if not record_id:
            return self.env[model_name].browse()
        try:
            record_id = int(record_id)
        except (TypeError, ValueError):
            raise ValidationError(_('Invalid %s.') % label) from None
        domain = [('id', '=', record_id), ('active', '=', True)]
        if extra_domain:
            domain.extend(extra_domain)
        record = self.env[model_name].sudo().search(domain, limit=1)
        if not record:
            raise ValidationError(_('%s is invalid or inactive.') % label)
        return record

    @api.model
    def _reference_data(self, records, include_brand=False):
        items = []
        for record in records:
            item = {
                'id': record.id,
                'code': record.code,
                'name': record.name,
            }
            if include_brand:
                item['brand_id'] = record.brand_id.id
            items.append(item)
        return items

    @endpoint(
        'NSP Mobile Vehicles',
        route_path='mobile/vehicles',
        methods='GET',
        code='nsp_mobile_vehicles',
    )
    def api_vehicles(self):
        user, _device, _session = self._mobile_context()
        vehicles = self._owned_vehicles(user, active=True)
        latest_logs = self._latest_allowed_parking_logs(vehicles)
        active_borrows = self._active_borrows_by_vehicle(vehicles)
        return {
            'data': {
                'items': [
                    self._vehicle_data(
                        vehicle,
                        latest_logs.get(vehicle.id),
                        active_borrows.get(vehicle.id),
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
        user, _device, _session = self._mobile_context()
        params = get_params(self)
        vehicle = self._owned_vehicle_from_params(user, params, required=True)
        if not vehicle.active:
            raise AccessError(_('Vehicle is inactive.'))
        latest_log = self.env['nsp.parking.log'].sudo().search([
            ('vehicle_id', '=', vehicle.id),
            ('decision', '=', 'allowed'),
        ], order='event_time desc, id desc', limit=1)
        borrow = self.env['nsp.vehicle.borrow'].sudo().find_valid_borrow(vehicle)
        return {
            'data': self._vehicle_data(vehicle, latest_log, borrow),
            'message': 'OK',
        }

    @endpoint(
        'NSP Mobile Parking Logs',
        route_path='mobile/parking/logs',
        methods='GET',
        code='nsp_mobile_parking_logs',
    )
    def api_parking_logs(self):
        user, _device, _session = self._mobile_context()
        params = get_params(self)
        limit, offset = self._pagination(params)
        owned_vehicles = self._owned_vehicles(user)
        domain = [('vehicle_id', 'in', owned_vehicles.ids)]

        vehicle = self._owned_vehicle_from_params(user, params)
        if vehicle:
            domain.append(('vehicle_id', '=', vehicle.id))

        ParkingLog = self.env['nsp.parking.log'].sudo()
        return {
            'data': {
                'total': ParkingLog.search_count(domain),
                'items': [
                    self._parking_log_data(parking_log)
                    for parking_log in ParkingLog.search(
                        domain,
                        order='event_time desc, id desc',
                        limit=limit,
                        offset=offset,
                    )
                ],
            },
            'message': 'OK',
        }

    @endpoint(
        'NSP Mobile Vehicle Config',
        route_path='mobile/vehicles/config',
        methods='GET',
        code='nsp_mobile_vehicle_config',
    )
    def api_vehicle_config(self):
        self._mobile_context()
        VehicleType = self.env['nsp.vehicle.type'].sudo()
        Brand = self.env['nsp.reference.brand'].sudo()
        VehicleModel = self.env['nsp.reference.model'].sudo()
        Color = self.env['nsp.vehicle.color'].sudo()
        return {
            'data': {
                'vehicle_types': self._reference_data(
                    VehicleType.search([('active', '=', True)], order='name, id')
                ),
                'brands': self._reference_data(
                    Brand.search([('active', '=', True)], order='name, id')
                ),
                'models': self._reference_data(
                    VehicleModel.search([('active', '=', True)], order='brand_id, name, id'),
                    include_brand=True,
                ),
                'colors': self._reference_data(
                    Color.search([('active', '=', True)], order='name, id')
                ),
            },
            'message': 'OK',
        }

    @endpoint(
        'NSP Mobile Vehicle Register',
        route_path='mobile/vehicles/register',
        methods='POST',
        code='nsp_mobile_vehicle_register',
    )
    def api_vehicle_register(self):
        user, _device, _session = self._mobile_context()
        body = get_body(self)
        self._validate_body_fields(body, {
            'license_plate', 'vehicle_type_id', 'brand_id', 'model_id', 'color_id', 'image',
        })

        Vehicle = self.env['nsp.vehicle'].sudo()
        plate = Vehicle._normalize_license_plate(body.get('license_plate'))
        if not plate:
            raise ValidationError(_('License plate is required.'))
        if Vehicle.search_count([('license_plate', '=', plate)]):
            raise ValidationError(_('License plate %s already exists.') % plate)

        vehicle_type = self._active_reference(
            'nsp.vehicle.type', body.get('vehicle_type_id'), 'vehicle_type_id'
        )
        brand = self._active_reference(
            'nsp.reference.brand', body.get('brand_id'), 'brand_id'
        )
        model = self._active_reference(
            'nsp.reference.model',
            body.get('model_id'),
            'model_id',
            [('brand_id', '=', brand.id)] if brand else None,
        )
        color = self._active_reference(
            'nsp.vehicle.color', body.get('color_id'), 'color_id'
        )

        values = {
            'license_plate': plate,
            'owner_id': user.id,
            'active': True,
        }
        if vehicle_type:
            values['vehicle_type_id'] = vehicle_type.id
        if brand:
            values['brand_id'] = brand.id
        if model:
            values['model_id'] = model.id
        if color:
            values['color_id'] = color.id
        if body.get('image'):
            values['image_1920'] = body['image']

        vehicle = Vehicle.create(values)
        return {
            'status_code': 201,
            'data': self._vehicle_data(vehicle),
            'message': _('Vehicle registered successfully.'),
        }
