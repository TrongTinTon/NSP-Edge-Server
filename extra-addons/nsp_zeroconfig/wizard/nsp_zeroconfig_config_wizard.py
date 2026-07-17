# -*- coding: utf-8 -*-
import re
import secrets

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

from odoo.addons.nsp_zeroconfig.utils.server import stop_discovery


_SERVICE_TYPE_RE = re.compile(r'^_[A-Za-z0-9-]+\._tcp\.local\.$', re.IGNORECASE)


class NspZeroconfigConfigWizard(models.TransientModel):
    _name = 'nsp.zeroconfig.config.wizard'
    _description = 'NSP Zeroconfig Settings'

    service_type = fields.Char(
        string='Service Type',
        default='_nsp._tcp.local.',
        required=True,
        help='DNS-SD service type used by both Edge Server and Controller, for example _nsp._tcp.local.',
    )
    discovery_secret = fields.Char(
        string='Discovery Secret Key',
        required=True,
        help='Pre-shared key used only to sign and verify mDNS TXT records. It is not a Core API Client Secret.',
    )
    show_discovery_secret = fields.Boolean(string='Show Discovery Secret Key', default=False)
    service_port = fields.Integer(string='Odoo HTTP Port', default=8069, required=True)

    running = fields.Boolean(string='Advertising', readonly=True)
    advertised_ipv6 = fields.Char(string='Advertised LAN IPv6', readonly=True)
    advertised_port = fields.Integer(string='Advertised Port', readonly=True)
    runtime_message = fields.Char(string='Status Message', readonly=True)

    @api.model
    def default_get(self, fields_list):
        values = super().default_get(fields_list)
        parameters = self.env['ir.config_parameter'].sudo()
        service = self.env['nsp.zeroconfig.service']
        configured = service.configured_values()
        secret = (
            parameters.get_param('nsp_zeroconfig.discovery_secret')
            or parameters.get_param('t4_coreapi.zeroconfig.discovery_secret')
            or parameters.get_param('nsp_controller.discovery_secret')
        )
        preview = service.advertised_preview()
        values.update({
            'service_type': configured['service_type'],
            'discovery_secret': secret or secrets.token_urlsafe(32),
            'show_discovery_secret': False,
            'service_port': configured['port'],
            'running': bool(preview.get('running')),
            'advertised_ipv6': preview.get('ip') or parameters.get_param('nsp_zeroconfig.last_advertised_ipv6') or '',
            'advertised_port': preview.get('port') or configured['port'],
            'runtime_message': preview.get('message') or '',
        })
        return values

    def _validated_values(self):
        self.ensure_one()
        service_type = (self.service_type or '').strip().lower()
        if not service_type.endswith('.'):
            service_type += '.'
        if not _SERVICE_TYPE_RE.match(service_type):
            raise ValidationError(_('Service Type must use DNS-SD TCP format, for example _nsp._tcp.local.'))

        discovery_secret = (self.discovery_secret or '').strip()
        if not discovery_secret:
            raise ValidationError(_('Discovery Secret Key is required.'))
        if len(discovery_secret) < 16:
            raise ValidationError(_('Discovery Secret Key must contain at least 16 characters.'))

        port = int(self.service_port or 0)
        if not 1 <= port <= 65535:
            raise ValidationError(_('Odoo HTTP Port must be between 1 and 65535.'))
        return service_type, discovery_secret, port

    def _save_parameters(self):
        service_type, discovery_secret, port = self._validated_values()
        parameters = self.env['ir.config_parameter'].sudo()
        parameters.set_param('nsp_zeroconfig.service_type', service_type)
        parameters.set_param('nsp_zeroconfig.discovery_secret', discovery_secret)
        parameters.set_param('nsp_zeroconfig.service_port', port)
        parameters.set_param('nsp_zeroconfig.auto_start', 'true')
        return service_type, discovery_secret, port

    def _reopen(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('NSP Zeroconfig'),
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def action_start_discovery(self):
        self.ensure_one()
        self._save_parameters()
        result = self.env['nsp.zeroconfig.service'].start_configured_discovery(restart=True)
        if result.get('code') not in (200, 409, 423):
            self.write({
                'running': False,
                'runtime_message': result.get('message') or _('Failed to start Zeroconfig discovery.'),
            })
            raise UserError(result.get('message') or _('Failed to start Zeroconfig discovery.'))
        self.write({
            'service_type': result.get('service_type') or self.service_type,
            'running': bool(result.get('running')),
            'advertised_ipv6': result.get('ip') or self.advertised_ipv6,
            'advertised_port': result.get('port') or self.service_port,
            'runtime_message': result.get('message') or '',
        })
        return self._reopen()

    def action_stop_discovery(self):
        self.ensure_one()
        self.env['ir.config_parameter'].sudo().set_param('nsp_zeroconfig.auto_start', 'false')
        result = stop_discovery()
        if result.get('code') not in (200, 404):
            raise UserError(result.get('message') or _('Failed to stop Zeroconfig discovery.'))
        self.write({
            'running': False,
            'runtime_message': result.get('message') or '',
        })
        return self._reopen()
