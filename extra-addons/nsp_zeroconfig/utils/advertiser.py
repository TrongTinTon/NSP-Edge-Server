# -*- coding: utf-8 -*-
import hashlib
import hmac
import ipaddress
import os
import secrets
import socket

import psutil
from zeroconf import IPVersion, ServiceInfo, Zeroconf


class NspServiceAdvertiser:
    """Advertise one signed NSP Edge Server over LAN IPv6 only."""

    SERVICE_TYPE = '_nsp._tcp.local.'
    _VIRTUAL_INTERFACE_MARKERS = (
        'docker', 'veth', 'virbr', 'vmnet', 'vbox', 'br-', 'tun', 'tap',
        'tailscale', 'zerotier', 'wireguard', 'wg',
    )

    def __init__(
        self,
        service_name='NSP Edge Server',
        service_type='_nsp._tcp.local.',
        port=8069,
        discovery_secret=None,
        advertised_ip=None,
        scheme='http',
        database_name=None,
        edge_server_code=None,
    ):
        if not (discovery_secret or '').strip():
            raise ValueError('Discovery Secret Key is required.')
        self.service_name = (service_name or 'NSP Edge Server').strip()
        self.signed_name = self.service_name
        self.service_type = self._validate_service_type(service_type)
        self.port = int(port)
        if not 1 <= self.port <= 65535:
            raise ValueError('Odoo HTTP Port must be between 1 and 65535.')
        self.discovery_secret = discovery_secret.strip()
        self.scheme = self._validate_scheme(scheme)
        self.database_name = (database_name or '').strip()
        self.edge_server_code = (edge_server_code or '').strip()

        selected = self.resolve_lan_ipv6(advertised_ip)
        self.ip = selected['ip']
        self.ip_address = selected['address']
        self.interface_name = selected['interface_name']
        self.interface_index = selected['interface_index']
        self.address_source = selected.get('source') or 'system'

        self.hostname = socket.gethostname()
        self.service_id = hashlib.sha256(
            f'{self.hostname}|{self.ip}|{self.port}|{self.database_name}|{self.edge_server_code}'.encode('utf-8')
        ).hexdigest()[:16]
        self.nonce = secrets.token_hex(16)
        self.signature_version = '2'
        self.signature = self._sign()

        # python-zeroconf accepts IPv6 interface indexes. Restricting the
        # advertiser to the selected LAN interface ensures that link-local
        # multicast and replies use the correct scope.
        self.zeroconf = Zeroconf(
            interfaces=[self.interface_index],
            ip_version=IPVersion.V6Only,
        )
        self.info = ServiceInfo(
            type_=self.service_type,
            name=f'{self.service_name}.{self.service_type}',
            addresses=[self.ip_address.packed],
            port=self.port,
            properties=self._properties(),
            server=f'{self.hostname}.local.',
            interface_index=self.interface_index if self.ip_address.is_link_local else None,
        )
        self.registered_name = False

    @staticmethod
    def _validate_service_type(value):
        service_type = (value or '_nsp._tcp.local.').strip().lower()
        if not service_type.endswith('.'):
            service_type += '.'
        labels = service_type.rstrip('.').split('.')
        if len(labels) != 3 or not labels[0].startswith('_') or labels[1] != '_tcp' or labels[2] != 'local':
            raise ValueError('Service Type must use DNS-SD TCP format, for example _nsp._tcp.local.')
        if len(labels[0]) < 2 or any(ch not in 'abcdefghijklmnopqrstuvwxyz0123456789_-' for ch in labels[0]):
            raise ValueError('Service Type contains invalid characters.')
        return service_type

    @staticmethod
    def _validate_scheme(value):
        scheme = (value or 'http').strip().lower()
        if scheme not in ('http', 'https'):
            raise ValueError('Advertised scheme must be http or https.')
        return scheme

    @staticmethod
    def _strip_scope(value):
        return (value or '').strip().split('%', 1)[0]

    @classmethod
    def _validate_ipv6(cls, value):
        if not value:
            return None
        raw = cls._strip_scope(str(value))
        try:
            parsed = ipaddress.ip_address(raw)
        except ValueError as exc:
            raise ValueError('Advertised IP must be a valid IPv6 address.') from exc
        if parsed.version != 6 or parsed.is_loopback or parsed.is_unspecified or parsed.is_multicast:
            raise ValueError('Advertised IP must be a usable non-loopback IPv6 address.')
        return parsed

    @classmethod
    def _candidate_score(cls, interface_name, address):
        """Prefer stable LAN IPv6 while retaining link-local fallback."""
        score = 0
        if address.is_private and not address.is_link_local:
            score += 300  # ULA: fc00::/7 is preferred on a private LAN.
        elif address.is_global:
            score += 250
        elif address.is_link_local:
            score += 100
        else:
            score += 50

        name = (interface_name or '').lower()
        if any(marker in name for marker in cls._VIRTUAL_INTERFACE_MARKERS):
            score -= 200
        if name.startswith(('eth', 'en', 'bond', 'lan')):
            score += 30
        return score

    @staticmethod
    def _container_runtime():
        if os.path.exists('/.dockerenv'):
            return 'Docker'
        if os.path.exists('/run/.containerenv'):
            return 'Podman/container'
        try:
            with open('/proc/1/cgroup', 'r', encoding='utf-8', errors='ignore') as handle:
                value = handle.read().lower()
            for marker, label in (
                ('docker', 'Docker'),
                ('kubepods', 'Kubernetes'),
                ('containerd', 'containerd'),
                ('podman', 'Podman'),
                ('lxc', 'LXC'),
            ):
                if marker in value:
                    return label
        except OSError:
            pass
        return ''

    @classmethod
    def _append_candidate(cls, candidates, seen, interface_name, interface_index, raw_address, source):
        raw = cls._strip_scope(raw_address)
        try:
            address = ipaddress.ip_address(raw)
        except ValueError:
            return
        if address.version != 6 or address.is_loopback or address.is_unspecified or address.is_multicast:
            return
        key = (interface_index, str(address))
        if key in seen:
            return
        seen.add(key)
        candidates.append({
            'ip': str(address),
            'address': address,
            'interface_name': interface_name,
            'interface_index': interface_index,
            'score': cls._candidate_score(interface_name, address),
            'source': source,
        })

    @classmethod
    def _scan_psutil(cls, candidates, seen, observations):
        try:
            stats = psutil.net_if_stats() or {}
            interface_map = psutil.net_if_addrs() or {}
        except Exception as exc:
            observations.append('psutil error=%s' % exc)
            return

        for interface_name, addresses in interface_map.items():
            stat = stats.get(interface_name)
            if stat is not None and not stat.isup:
                observations.append('%s=down' % interface_name)
                continue
            try:
                interface_index = socket.if_nametoindex(interface_name)
            except (OSError, AttributeError):
                observations.append('%s=no-interface-index' % interface_name)
                continue
            if interface_index <= 0:
                continue

            visible = []
            for item in addresses:
                family = getattr(item, 'family', None)
                value = getattr(item, 'address', '') or ''
                if family == socket.AF_INET:
                    visible.append('IPv4:%s' % value)
                elif family == socket.AF_INET6:
                    visible.append('IPv6:%s' % value)
                    cls._append_candidate(
                        candidates, seen, interface_name, interface_index, value, 'psutil'
                    )
            observations.append('%s=%s' % (interface_name, ','.join(visible) if visible else 'no-IP-address'))

    @classmethod
    def _scan_linux_proc(cls, candidates, seen, observations):
        path = '/proc/net/if_inet6'
        if not os.path.exists(path):
            observations.append('/proc/net/if_inet6=missing')
            return
        try:
            with open(path, 'r', encoding='ascii', errors='ignore') as handle:
                lines = handle.readlines()
        except OSError as exc:
            observations.append('/proc/net/if_inet6 error=%s' % exc)
            return

        if not lines:
            observations.append('/proc/net/if_inet6=empty')
            return

        for line in lines:
            parts = line.split()
            if len(parts) != 6:
                continue
            hex_address, hex_index, _prefix, _scope, _flags, interface_name = parts
            try:
                address = str(ipaddress.IPv6Address(int(hex_address, 16)))
                interface_index = int(hex_index, 16)
            except (ValueError, TypeError):
                continue
            cls._append_candidate(
                candidates, seen, interface_name, interface_index, address, '/proc/net/if_inet6'
            )

    @classmethod
    def scan_lan_ipv6(cls):
        """Return visible IPv6 candidates and concise network diagnostics."""
        candidates = []
        seen = set()
        observations = []
        cls._scan_psutil(candidates, seen, observations)
        if os.name == 'posix':
            cls._scan_linux_proc(candidates, seen, observations)
        candidates.sort(key=lambda item: (-item['score'], item['interface_index'], item['ip']))
        return candidates, observations

    @classmethod
    def resolve_lan_ipv6(cls, configured_ip=None):
        configured = cls._validate_ipv6(configured_ip)
        candidates, observations = cls.scan_lan_ipv6()

        if configured:
            candidates = [item for item in candidates if item['address'] == configured]
            if not candidates:
                raise RuntimeError(
                    'The configured IPv6 address is not active in the Odoo process network namespace. '
                    'Visible network: %s' % ('; '.join(observations) or 'none')
                )

        if not candidates:
            runtime = cls._container_runtime()
            namespace_hint = (
                ' Odoo is running inside %s; enable IPv6 in that container network namespace or use host networking.'
                % runtime
            ) if runtime else ''
            raise RuntimeError(
                'No usable LAN IPv6 address is visible to the Odoo process.%s '
                'A real non-loopback IPv6 address (ULA, global, or link-local) must be assigned to the LAN interface. '
                'Visible network: %s'
                % (namespace_hint, '; '.join(observations) or 'none')
            )

        return candidates[0]

    def _signature_payload(self):
        return '|'.join([
            self.signed_name,
            self.ip,
            str(self.port),
            self.service_id,
            self.nonce,
            self.scheme,
            self.database_name,
            self.edge_server_code,
        ])

    def _sign(self):
        return hmac.new(
            self.discovery_secret.encode('utf-8'),
            self._signature_payload().encode('utf-8'),
            hashlib.sha256,
        ).hexdigest()

    def _properties(self):
        properties = {
            'version': '19.0',
            'module': 'nsp_zeroconfig',
            'auth': 'hmac-sha256',
            'signature_version': self.signature_version,
            'signed_name': self.signed_name,
            'service_id': self.service_id,
            'nonce': self.nonce,
            'signature': self.signature,
            'scheme': self.scheme,
            'address_family': 'ipv6',
            'advertised_ip': self.ip,
            'bootstrap': 'controller-code-hmac-sha256',
            'bootstrap_path': '/nsp/zeroconfig/controller/bootstrap',
            'auth_path': '/auth/token',
            'api_standard': 'core_api',
            'gateway_format': '/{service_code}/v1/{route}',
        }
        if self.database_name:
            properties['database'] = self.database_name
        if self.edge_server_code:
            properties['edge_server_code'] = self.edge_server_code
        return properties

    def register(self):
        self.zeroconf.register_service(self.info, allow_name_change=True)
        self.registered_name = self.info.name
        return self

    def close(self):
        try:
            if self.registered_name:
                self.zeroconf.unregister_service(self.info)
        finally:
            self.registered_name = False
            self.zeroconf.close()
