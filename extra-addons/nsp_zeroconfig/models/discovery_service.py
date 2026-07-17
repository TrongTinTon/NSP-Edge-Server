# -*- coding: utf-8 -*-
import logging
import socket

from odoo import api, models

from odoo.addons.nsp_zeroconfig.utils.advertiser import NspServiceAdvertiser
from odoo.addons.nsp_zeroconfig.utils.server import discovery_status, start_discovery, stop_discovery

_logger = logging.getLogger(__name__)


class NspZeroconfigService(models.AbstractModel):
    _name = 'nsp.zeroconfig.service'
    _description = 'NSP Zeroconfig Runtime Service'

    @api.model
    def _edge_server_code(self):
        """Return the single Local Server code without assuming a legacy schema."""
        if 'nsp.controller' in self.env.registry.models:
            controller_model = self.env['nsp.controller'].sudo()
            domain = []
            if 'active' in controller_model._fields:
                domain.append(('active', '=', True))
            if 'node_type' in controller_model._fields:
                domain.append(('node_type', '=', 'local_server'))
            local_servers = controller_model.search(domain, limit=2)
            if len(local_servers) == 1 and 'controller_id' in controller_model._fields:
                return (local_servers.controller_id or '').strip()

        if 'nsp.sync.auth' not in self.env.registry.models:
            return ''
        auth_model = self.env['nsp.sync.auth'].sudo()
        for field_name in ('local_server_id', 'edge_server_id'):
            if field_name not in auth_model._fields:
                continue
            auths = auth_model.search([
                ('active', '=', True),
                (field_name, '!=', False),
            ])
            servers = auths.mapped(field_name).exists()
            if len(servers) == 1 and 'controller_id' in servers._fields:
                return (servers.controller_id or '').strip()
        return ''

    @api.model
    def configured_values(self):
        params = self.env['ir.config_parameter'].sudo()
        edge_code = self._edge_server_code()
        service_type = (params.get_param('nsp_zeroconfig.service_type') or '_nsp._tcp.local.').strip().lower()
        if not service_type.endswith('.'):
            service_type += '.'
        try:
            port = int(params.get_param('nsp_zeroconfig.service_port') or 8069)
        except Exception:
            port = 8069
        secret = params.get_param('nsp_zeroconfig.discovery_secret') or ''
        enabled_raw = params.get_param('nsp_zeroconfig.auto_start')
        enabled = bool(secret) if enabled_raw is None else str(enabled_raw).lower() in ('1', 'true', 'yes', 'on')
        hostname = (socket.gethostname() or 'server').strip()
        service_name = 'NSP Edge %s' % (edge_code or hostname)
        return {
            'enabled': enabled,
            'service_name': service_name,
            'service_type': service_type,
            'port': port,
            'discovery_secret': secret,
            'database_name': self.env.cr.dbname,
            'edge_server_code': edge_code,
            'lock_key': '%s:%s:%s' % (self.env.cr.dbname, service_type, port),
        }

    @api.model
    def advertised_preview(self):
        values = self.configured_values()
        status = discovery_status()
        if status.get('running'):
            return {
                'running': True,
                'ip': status.get('ip') or '',
                'port': status.get('port') or values['port'],
                'interface_name': status.get('interface_name') or '',
                'message': '',
            }
        try:
            selected = NspServiceAdvertiser.resolve_lan_ipv6()
            return {
                'running': False,
                'ip': selected['ip'],
                'port': values['port'],
                'interface_name': selected['interface_name'],
                'message': '',
            }
        except Exception as exc:
            return {
                'running': False,
                'ip': '',
                'port': values['port'],
                'interface_name': '',
                'message': str(exc),
            }

    @api.model
    def start_configured_discovery(self, restart=False):
        values = self.configured_values()
        if restart and discovery_status().get('running'):
            stop_discovery()
        if not values['discovery_secret']:
            return {'code': 400, 'message': 'Discovery Secret Key is not configured.', 'running': False}
        result = start_discovery(
            service_name=values['service_name'],
            service_type=values['service_type'],
            port=values['port'],
            discovery_secret=values['discovery_secret'],
            database_name=values['database_name'],
            edge_server_code=values['edge_server_code'],
            lock_key=values['lock_key'],
        )
        if result.get('code') == 200:
            params = self.env['ir.config_parameter'].sudo()
            params.set_param('nsp_zeroconfig.last_advertised_ipv6', result.get('ip') or '')
            params.set_param('nsp_zeroconfig.last_advertised_port', result.get('port') or values['port'])
        return result

    @api.model
    def cron_ensure_discovery(self):
        values = self.configured_values()
        if not values['enabled']:
            return True
        status = discovery_status()
        if status.get('running'):
            if status.get('service_type') == values['service_type'] and status.get('port') == values['port']:
                return True
            stop_discovery()
        result = self.start_configured_discovery(restart=False)
        if result.get('code') not in (200, 409, 423):
            _logger.warning('NSP Zeroconfig IPv6 auto-start failed: %s', result.get('message'))
        return True
