# Part of T4 Core API. See LICENSE file for full copyright and licensing details.

"""Gateway URL helpers: /{service_code}/{version_code}/{route_suffix}."""

RESERVED_ROOT_SEGMENTS = frozenset({
    'web', 'static', 'longpolling', 'bus', 'websocket', 'mail', 'odoo',
    'api', 'json', 'xmlrpc', 'report', 'website', 'shop', 'payment', 'auth',
})

AUTH_TOKEN_PATH = '/auth/token'


def is_auth_token_path(path):
    """Return True when the path targets the public token endpoint."""
    normalized = (path or '').split('?', 1)[0].strip('/')
    return normalized == 'auth/token' or normalized.startswith('auth/token/')


def is_gateway_path(path):
    """Return True when the path may target the Core API gateway."""
    if is_auth_token_path(path):
        return False
    path = (path or '').split('?', 1)[0]
    parts = path.strip('/').split('/')
    if len(parts) < 2:
        return False
    return parts[0].lower() not in RESERVED_ROOT_SEGMENTS


def build_gateway_path(service_code, version_code, route_suffix=''):
    """Build a public gateway route, e.g. /gk/v1/gate1."""
    service = (service_code or '').strip('/')
    version = (version_code or '').strip('/')
    suffix = (route_suffix or '').strip('/')
    if not service or not version:
        return f'/{suffix}' if suffix else '/'
    base = f'/{service}/{version}'
    return f'{base}/{suffix}' if suffix else base


def parse_gateway_subpath(subpath):
    """Split ``v1/gate1`` into version code and remaining route suffix."""
    subpath = (subpath or '').strip('/')
    if not subpath:
        return '', ''
    parts = subpath.split('/')
    return parts[0], '/'.join(parts[1:])
