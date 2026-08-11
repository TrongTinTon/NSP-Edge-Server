# Part of T4 Core API. See LICENSE file for full copyright and licensing details.

import ipaddress

from odoo.http import request


def get_client_ip():
    """Return the remote IP address from the current HTTP request."""
    if not request:
        return None
    return request.httprequest.environ.get('REMOTE_ADDR')


def get_request_hostname(httprequest=None):
    """Return the normalized hostname from the HTTP request (without port)."""
    httprequest = httprequest or (request.httprequest if request else None)
    if not httprequest:
        return ''
    host = (httprequest.host or '').strip().lower()
    if host.startswith('['):
        return host
    return host.split(':')[0].strip('.')


def check_ip_allowed(allowed_ips_text, ip_address):
    """Return True when the IP matches the allowlist. Empty list allows any IP."""
    if not allowed_ips_text or not ip_address:
        return True
    lines = [ln.strip() for ln in allowed_ips_text.splitlines() if ln.strip()]
    if not lines:
        return True
    try:
        client = ipaddress.ip_address(ip_address)
    except ValueError:
        return False
    for entry in lines:
        try:
            if '/' in entry:
                if client in ipaddress.ip_network(entry, strict=False):
                    return True
            elif client == ipaddress.ip_address(entry):
                return True
        except ValueError:
            continue
    return False
