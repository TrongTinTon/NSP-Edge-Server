# Part of T4 Core API. See LICENSE file for full copyright and licensing details.

"""Core API public URL helpers: /{version_code}/{route_suffix}."""

RESERVED_ROOT_SEGMENTS = frozenset({
    'web', 'static', 'longpolling', 'bus', 'websocket', 'mail', 'odoo',
    'api', 'json', 'xmlrpc', 'report', 'website', 'shop', 'payment', 'auth',
})

AUTH_TOKEN_PATH = '/auth/token'
AUTH_REFRESH_PATH = '/auth/refresh'


def is_auth_path(path):
    normalized = (path or '').split('?', 1)[0].strip('/')
    return normalized in {'auth/token', 'auth/refresh'}


def is_auth_token_path(path):
    normalized = (path or '').split('?', 1)[0].strip('/')
    return normalized == 'auth/token'


def is_gateway_path(path):
    """Return True when the path may target /{version}/{route}."""
    if is_auth_path(path):
        return False
    parts = (path or '').split('?', 1)[0].strip('/').split('/')
    if len(parts) < 2:
        return False
    return parts[0].lower() not in RESERVED_ROOT_SEGMENTS


def build_gateway_path(version_code, route_suffix=''):
    """Build a public gateway route, e.g. /v1/edge-server/status."""
    version = (version_code or '').strip('/')
    suffix = (route_suffix or '').strip('/')
    if not version:
        return f'/{suffix}' if suffix else '/'
    base = f'/{version}'
    return f'{base}/{suffix}' if suffix else base


def parse_gateway_subpath(subpath):
    subpath = (subpath or '').strip('/')
    if not subpath:
        return '', ''
    parts = subpath.split('/')
    return parts[0], '/'.join(parts[1:])
