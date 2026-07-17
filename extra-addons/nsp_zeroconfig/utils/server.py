# -*- coding: utf-8 -*-
import hashlib
import logging
import os
import tempfile
import threading

from .advertiser import NspServiceAdvertiser

_logger = logging.getLogger(__name__)
_lock = threading.RLock()
_advertiser = None
_process_lock_handle = None

try:
    import fcntl
except ImportError:  # pragma: no cover - Odoo Edge Server is normally Linux
    fcntl = None


def _acquire_process_lock(lock_key):
    global _process_lock_handle
    if _process_lock_handle or not fcntl:
        return True
    digest = hashlib.sha256((lock_key or 'default').encode('utf-8')).hexdigest()[:20]
    path = os.path.join(tempfile.gettempdir(), 'nsp_zeroconfig_%s.lock' % digest)
    handle = open(path, 'a+', encoding='utf-8')
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False
    _process_lock_handle = handle
    return True


def _release_process_lock():
    global _process_lock_handle
    handle = _process_lock_handle
    _process_lock_handle = None
    if not handle:
        return
    try:
        if fcntl:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def discovery_status():
    with _lock:
        if not _advertiser:
            return {'running': False}
        return {
            'running': True,
            'service_name': _advertiser.service_name,
            'registered_name': _advertiser.registered_name,
            'service_type': _advertiser.service_type,
            'ip': _advertiser.ip,
            'port': _advertiser.port,
            'scheme': _advertiser.scheme,
            'database': _advertiser.database_name,
            'edge_server_code': _advertiser.edge_server_code,
            'service_id': _advertiser.service_id,
            'interface_name': _advertiser.interface_name,
            'interface_index': _advertiser.interface_index,
            'address_family': 'ipv6',
        }


def start_discovery(
    service_name='NSP Edge Server',
    service_type='_nsp._tcp.local.',
    port=8069,
    discovery_secret=None,
    advertised_ip=None,
    scheme='http',
    database_name=None,
    edge_server_code=None,
    lock_key=None,
):
    global _advertiser
    with _lock:
        if _advertiser:
            return {'code': 409, 'message': 'Zeroconfig discovery is already running.', **discovery_status()}
        if not _acquire_process_lock(lock_key or '%s:%s' % (database_name or 'default', port)):
            return {
                'code': 423,
                'message': 'Another Odoo worker is already advertising Zeroconfig for this Edge Server.',
                'running': False,
            }
        try:
            _advertiser = NspServiceAdvertiser(
                service_name=service_name,
                service_type=service_type,
                port=port,
                discovery_secret=discovery_secret,
                advertised_ip=advertised_ip,
                scheme=scheme,
                database_name=database_name,
                edge_server_code=edge_server_code,
            ).register()
            _logger.info(
                'NSP Zeroconfig advertised %s at %s://[%s]:%s interface=%s database=%s edge=%s',
                _advertiser.service_name,
                _advertiser.scheme,
                _advertiser.ip,
                _advertiser.port,
                _advertiser.interface_name,
                _advertiser.database_name,
                _advertiser.edge_server_code,
            )
            return {'code': 200, 'message': 'Zeroconfig discovery started.', **discovery_status()}
        except Exception as exc:
            _advertiser = None
            _release_process_lock()
            _logger.exception('Cannot start NSP Zeroconfig')
            return {'code': 500, 'message': str(exc), 'running': False}


def stop_discovery():
    global _advertiser
    with _lock:
        if not _advertiser:
            _release_process_lock()
            return {'code': 404, 'message': 'No active Zeroconfig discovery session.', 'running': False}
        current = _advertiser
        _advertiser = None
        try:
            current.close()
            _logger.info('NSP Zeroconfig stopped')
            return {'code': 200, 'message': 'Zeroconfig discovery stopped.', 'running': False}
        except Exception as exc:
            _logger.exception('Cannot stop NSP Zeroconfig')
            return {'code': 500, 'message': str(exc), 'running': False}
        finally:
            _release_process_lock()
