# Part of T4 Core API. See LICENSE file for full copyright and licensing details.

import ipaddress

from odoo.exceptions import AccessError
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


def check_rate_limit(env, domain_extra, limit, error_message):
    """Raise AccessError when recent log count exceeds the configured limit."""
    if not limit:
        return True
    count = env['core.api.log'].sudo().count_recent(domain_extra, minutes=1)
    if count >= limit:
        raise AccessError(error_message)
    return True


def check_application_api_rate_limit(application):
    """Enforce per-application API call rate limits."""
    application.ensure_one()
    check_rate_limit(
        application.env,
        [('application_id', '=', application.id), ('event_type', '=', 'api')],
        application.rate_limit_per_minute,
        f'API rate limit exceeded for application "{application.name}" ({application.rate_limit_per_minute}/min).',
    )


def check_application_auth_rate_limit(application):
    """Enforce per-application token request rate limits."""
    application.ensure_one()
    check_rate_limit(
        application.env,
        [('application_id', '=', application.id), ('event_type', '=', 'auth')],
        application.auth_rate_limit_per_minute,
        f'Auth rate limit exceeded for application "{application.name}" ({application.auth_rate_limit_per_minute}/min).',
    )


def check_ip_auth_rate_limit(env, ip_address, limit=30):
    """Enforce global per-IP throttling on the auth endpoint."""
    if not ip_address or not limit:
        return True
    check_rate_limit(
        env,
        [('ip_address', '=', ip_address), ('event_type', '=', 'auth'), ('route', '=like', '%/auth/token')],
        limit,
        f'Too many authentication attempts from IP {ip_address}. Try again later.',
    )
