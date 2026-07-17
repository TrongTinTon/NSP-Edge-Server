# T4 Core API — Full Technical Reference

Odoo 19 module that exposes a **versioned HTTP API gateway** for external applications.  
Clients authenticate with **Client ID + Secret** once, receive **access + refresh tokens**, then call routes that run **Server Actions**.

**Companion addon:** `t4_coreapi_boot` (separate folder, server-wide) — database selection for `/api/*`.

---

## Table of contents

1. [Architecture overview](#1-architecture-overview)
2. [End-to-end request lifecycle](#2-end-to-end-request-lifecycle)
3. [Authentication & token lifecycle](#3-authentication--token-lifecycle)
4. [Context variables (who sets what)](#4-context-variables-who-sets-what)
5. [Root & bootstrap files](#5-root--bootstrap-files)
6. [controllers/ — HTTP layer](#6-controllers--http-layer)
7. [models/ — business logic & data](#7-models--business-logic--data)
8. [utils/ — shared helpers](#8-utils--shared-helpers)
9. [wizard/ — UI popups](#9-wizard--ui-popups)
10. [security/ & data/ & views/](#10-security--data--views)
11. [t4_coreapi_boot companion](#11-t4_coreapi_boot-companion)
12. [How to extend](#12-how-to-extend)

---

## 1. Architecture overview

```
External Client
      │
      ▼
┌─────────────────────────────────────────────────────────────┐
│  t4_coreapi_boot (server start)                             │
│  http_patch.patch_api_db_selection()                        │
│  → reads ?db= on /api/*                  │
└─────────────────────────────────────────────────────────────┘
      │
      ▼
┌──────────────────┐     ┌──────────────────┐
│  auth.py         │     │  proxy.py        │
│  auth='none'     │     │  auth='core_api' │
│  /api/v1/auth/   │     │  /api/v1/...     │
└────────┬─────────┘     └────────┬─────────┘
         │                        │
         │                        ▼
         │              ┌──────────────────┐
         │              │  ir_http.py      │
         │              │  _auth_method_   │
         │              │  core_api()      │
         │              │  → sets context  │
         │              └────────┬─────────┘
         │                        │
         ▼                        ▼
┌─────────────────────────────────────────────────────────────┐
│  Models: application, token, version, domain, endpoint    │
└─────────────────────────────────────────────────────────────┘
         │                        │
         ▼                        ▼
┌──────────────────┐     ┌──────────────────┐
│  core.api.log    │     │ ir.actions.server│
│  (audit)         │     │ (your Python)    │
└──────────────────┘     └──────────────────┘
```

### Core models (relationship)

```
core.api.domain (hostname: ashaf.xyz)
    └── core.api.version (code: v1 → path /api/v1)
            └── core.api.endpoint (suffix: orders → /api/v1/orders)
                    └── ir.actions.server (handler code)
                            └── owned by core.api.application
```

Each **application** owns its **gateway routes**. A route's `code` is the permission key checked at runtime.

---

## 2. End-to-end request lifecycle

### A. Token request — `POST /api/v1/auth/token`

| Step | File | Function | What happens |
|------|------|----------|--------------|
| 1 | `t4_coreapi_boot/http_patch.py` | `_get_session_and_dbname` | If path starts with `/api/`, pick DB from `?db=` |
| 2 | `controllers/auth.py` | `issue_token` | Route matched; `auth='none'` — no Bearer required |
| 3 | `utils/security.py` | `check_ip_auth_rate_limit` | Max 30 auth attempts/min per IP (global) |
| 4 | `controllers/auth.py` | `_resolve_version` | `core.api.domain.get_from_request()` + `version.get_active_by_code('v1')` |
| 5 | `controllers/auth.py` | `_parse_request_data` | Parse JSON or form body |
| 6 | `controllers/auth.py` | `_issue_token_impl` | Branch on `grant_type` |
| 7a | `models/core_api_application.py` | `authenticate_client` | Verify `client_id` + hashed `client_secret` |
| 7b | `models/core_api_token.py` | `issue_for_application` | Create access + refresh pair; revoke old tokens |
| 8 | `controllers/auth.py` | `_token_response` | Build JSON with `expires_in`, `refresh_expires_in` |
| 9 | `controllers/auth.py` | `_log_auth` | Write `core.api.log` row (`event_type=auth`) |

### B. API request — `GET /api/v1/orders` + Bearer token

| Step | File | Function | What happens |
|------|------|----------|--------------|
| 1 | `t4_coreapi_boot` | (same) | DB selection |
| 2 | Odoo HTTP router | — | Sees `auth='core_api'` on route |
| 3 | `models/ir_http.py` | `_auth_method_core_api` | **Runs before controller** |
| 4 | `ir_http.py` | `_extract_bearer_token` | Parse `Authorization: Bearer ...` |
| 5 | `models/core_api_token.py` | `authenticate` | Verify access token (not refresh) |
| 6 | `models/core_api_application.py` | `check_ip_allowed`, `check_api_rate_limit` | Per-app security |
| 7 | `ir_http.py` | `request.update_context` | Set `core_api_application_id`, `core_api_token_id`, `core_api_client_id` |
| 8 | `controllers/proxy.py` | `gateway` | Controller method starts |
| 9 | `utils/logging.py` | `log_core_api` wrapper | Start timer for audit log |
| 10 | `models/core_api_version.py` | `resolve_from_api_subpath` | `v1/orders` → version v1, rest `orders` |
| 11 | `controllers/base.py` | `_get_application` | Read `core_api_application_id` from `request.env.context` |
| 12 | `utils/core_api_utils.py` | `get_context` | Build `{core_api: {params, body}}` dict |
| 13 | `models/core_api_endpoint.py` | `dispatch_request` | Find matching route for path + app |
| 14 | `models/core_api_endpoint.py` | `dispatch` | Verify route belongs to app; `check_api_access` |
| 15 | `models/core_api_endpoint.py` | `_run_server_action` | Run linked Server Action with `core_api_*` context |
| 16 | Server Action code | `set_response()` or `set_api_response()` | Set `request.core_api_response` |
| 17 | `core_api_endpoint.py` | `_run_server_action` | Serialize JSON, return HTTP response |
| 18 | `utils/logging.py` | wrapper `finally` | Log success/failure to `core.api.log` |

---

## 3. Authentication & token lifecycle

### Grant types (`controllers/auth.py`)

| grant_type | Input | Output | Secret on wire? |
|------------|-------|--------|-----------------|
| `client_credentials` | `client_id`, `client_secret` | New access + refresh pair | Yes (once per login) |
| `refresh_token` | `refresh_token` | New access + refresh pair (rotation) | No |

### Token pair (`models/core_api_token.py`)

- Both tokens share `token_pair_id` (UUID).
- Only **access** tokens work on `auth='core_api'` routes.
- **Refresh** only works on `/auth/token` with `grant_type=refresh_token`.
- On login: all active tokens for the app are revoked.
- On refresh: only the current pair is revoked, new pair issued.
- Tokens stored as: `token_index` (first 8 chars, for lookup) + `token_hash` (pbkdf2_sha512).
- Plaintext shown **only once** at issue time.

### Host domain resolution

URL path is always `/api/v1/...`. **Which** v1 is used depends on the HTTP `Host` header:

| Request Host | Resolved domain | Version lookup |
|--------------|-----------------|----------------|
| `localhost`, unknown | Default domain | `get_active_by_code('v1', default_domain)` |
| `ashaf.xyz` | Domain record with `hostname=ashaf.xyz` | `get_active_by_code('v1', ashaf_domain)` |

Same version code `v1` can exist on multiple host domains with different routes.

---

## 4. Context variables (who sets what)

### Layer 1 — Set by `ir_http._auth_method_core_api` (before any controller)

| Key | Type | Meaning |
|-----|------|---------|
| `core_api_application_id` | int | Authenticated `core.api.application` ID |
| `core_api_token_id` | int | Active access `core.api.token` ID |
| `core_api_client_id` | str | Client ID string |

Read via: `controllers/base.py` → `_get_application()`

### Layer 2 — Set by `get_context()` in proxy (merged via `with_context`)

| Key | Type | Meaning |
|-----|------|---------|
| `core_api.params` | dict | URL query / form kwargs |
| `core_api.body` | dict | Parsed JSON body |
| `core_api.is_json` | bool | Always `False` for HTTP gateway |

Read via: `get_params()`, `get_body()` in `utils/core_api_utils.py`

### Layer 3 — Set by `endpoint._server_action_context` (for Server Actions)

| Key | Meaning |
|-----|---------|
| `core_api_application_id` | Application ID (again) |
| `core_api_method` | GET, POST, … |
| `core_api_route` | Full pattern e.g. `/api/v1/orders` |
| `core_api_endpoint_id` | Route record ID |
| `core_api_endpoint_code` | Route permission code |
| `core_api_version_id` | Version record ID |
| `core_api_version_code` | e.g. `v1` |
| `core_api_body` | Parsed JSON from HTTP request |
| `core_api_params` | Query string dict |
| `active_model`, `active_id`, `active_ids` | Standard Odoo action context |

Read via: `application.get_api_context()` or directly from `env.context` in Server Action.

### Response channel

Server Action sets: `request.core_api_response = {...}` via:
- `env['core.api.application'].set_api_response(data)` — model method
- `set_response(data=..., message=..., status_code=...)` — utils helper

`core_api_endpoint._run_server_action` reads `request.core_api_response` and returns JSON HTTP response.

---

## 5. Root & bootstrap files

### `__init__.py`

```python
from . import controllers
from . import models
from . import wizard
from . import utils
from .hooks import post_init_hook
```

Loads all subpackages and registers the post-install hook with Odoo.

### `__manifest__.py`

| Key | Purpose |
|-----|---------|
| `version` | Module version (triggers upgrade migrations) |
| `depends` | `base`, `mail` |
| `data` | Load order: security → domain data → versions → views → wizard |
| `post_init_hook` | `post_init_hook` function name in `hooks.py` |

### `hooks.py`

| Function | When | What |
|----------|------|------|
| `post_init_hook(env)` | After module install/upgrade | Calls `env['core.api.endpoint']._migrate_legacy_route_fields()` to backfill `version_id` + `route_suffix` from old `route_pattern` values |

---

## 6. controllers/ — HTTP layer

### `controllers/__init__.py`

Imports: `auth`, `base`, `proxy`, `main`.

### `controllers/base.py` — `CoreApiController`

Base class for custom secured API controllers.

| Method | Parameters | Returns | Description |
|--------|------------|---------|-------------|
| `_get_application(self)` | — | `core.api.application` recordset | Reads `request.env.context['core_api_application_id']`. Returns empty recordset if missing. Uses `sudo()`. |
| `_get_device(self)` | — | same as above | **Alias** for backward compatibility. |

**Does NOT set context** — only reads what `ir_http.py` already set.

### `controllers/proxy.py` — `CoreApiProxyController`

Main API gateway. Inherits `CoreApiController`.

| Method / Decorator | Route | Auth | Description |
|--------------------|-------|------|-------------|
| `@log_core_api('api')` | — | — | Decorator from `utils/logging.py`; logs every call to `core.api.log` |
| `gateway(self, subpath, **kw)` | `GET/POST/PUT/PATCH/DELETE /api/<path:subpath>` | `core_api` | Main entry point for all protected API traffic |

**`gateway()` step-by-step:**

1. `Version.resolve_from_api_subpath(subpath)` — e.g. `v1/orders` → version + `orders`
2. Reject if rest is `auth/token` (auth has its own controller)
3. Build full path: `{version.path_prefix}/{rest}` e.g. `/api/v1/orders`
4. `self._get_application()` — from context (set by ir_http)
5. `get_context(kw, request.httprequest.get_data())` — params + body
6. `endpoint.dispatch_request(path, application)` with merged context

### `controllers/auth.py` — `CoreApiAuthController`

Token endpoint. No Bearer auth required (`auth='none'`).

| Method | Route | Description |
|--------|-------|-------------|
| `issue_token(self, version_code, **kw)` | `POST /api/<version_code>/auth/token` | Public token endpoint |

| Private method | Description |
|----------------|-------------|
| `_log_auth(application, route, ip, ua, status_code, success, duration_ms, error)` | Creates `core.api.log` auth row |
| `_parse_request_data(kw)` | JSON or form-urlencoded body → dict |
| `_token_response(token_result, application)` | Builds OAuth2-style JSON response body |
| `_resolve_version(version_code)` | Host header → domain → version record |
| `_issue_token_impl(version, data, kw)` | Core logic for both grant types; returns `make_response` JSON |

### `controllers/main.py`

Re-exports `CoreApiController` for other modules:

```python
from .base import CoreApiController
__all__ = ['CoreApiController']
```

Use when building **custom** `@http.route(..., auth='core_api')` controllers in other addons.

---

## 7. models/ — business logic & data

### `models/__init__.py` — import order

```python
from . import core_api_domain
from . import core_api_version
from . import core_api_endpoint      # MUST load before application (One2many field)
from . import core_api_application
from . import core_api_token
from . import core_api_log
from . import ir_http
from . import ir_actions_server
from . import action_endpoint
from . import my_api_service
```

### `models/ir_http.py` — `IrHttp` (inherit)

Registers custom auth method `core_api` used in `@http.route(..., auth='core_api')`.

| Method | Type | Description |
|--------|------|-------------|
| `_extract_bearer_token(cls)` | classmethod | Regex `Bearer <token>` from Authorization header |
| `_auth_method_core_api(cls)` | classmethod | **Gatekeeper**: validate token → IP → rate limit → set context → switch env to public user |
| `_auth_method_validate_core_api(cls)` | classmethod | Alias; calls `_auth_method_core_api` |

**`_auth_method_core_api` flow:**

1. Extract bearer token → 401 if missing
2. `core.api.token.authenticate(token)` → 401 if invalid/expired
3. `application.check_ip_allowed(ip)` → 403 if blocked
4. `application.check_api_rate_limit()` → 429 if exceeded
5. `request.update_env(user=public_user)`
6. `request.update_context(core_api_application_id=..., core_api_token_id=..., core_api_client_id=...)`
7. `request.session.can_save = False` (stateless API)

---

### `models/core_api_domain.py` — `core.api.domain`

Groups API versions under a **public hostname** (DNS name).

| Field | Description |
|-------|-------------|
| `name` | Display name |
| `hostname` | e.g. `ashaf.xyz` (empty on default domain) |
| `is_default` | Fallback when Host doesn't match any hostname |
| `base_url` | Computed: `https://{hostname}` or `web.base.url` |
| `version_ids` | One2many versions on this host |
| `version_count` | Computed count |

| Method | Description |
|--------|-------------|
| `_compute_base_url()` | Scheme from `web.base.url` + hostname |
| `_compute_version_count()` | `len(version_ids)` |
| `_check_hostname()` | Default must have no hostname; others must have valid hostname |
| `_check_single_default()` | Only one `is_default=True` allowed |
| `get_default()` | Returns XML ref `core_api_domain_default` or first default |
| `get_from_request(httprequest)` | Match `Host` header → domain record; else default |
| `action_view_versions()` | UI action: open versions filtered to this domain |

---

### `models/core_api_version.py` — `core.api.version`

API version segment in URL path (`v1`, `v2`).

| Field | Description |
|-------|-------------|
| `domain_id` | Host domain this version belongs to |
| `code` | URL segment: `/api/{code}/...` |
| `path_prefix` | Computed: `/api/v1` |
| `public_base_url` | Computed: `https://ashaf.xyz/api/v1` |
| `endpoint_count` | Count of linked gateway routes |

| Method | Description |
|--------|-------------|
| `_compute_path_prefix()` | `/api/{code}` |
| `_compute_public_base_url()` | `{domain.base_url}{path_prefix}` |
| `_compute_endpoint_count()` | `read_group` on endpoints |
| `_check_code()` | Non-empty, no slashes |
| `get_default_version()` | v1 XML ref or first active on default domain |
| `get_active_by_code(code, api_domain)` | Find active version for code on given host domain |
| `resolve_from_api_subpath(subpath, api_domain)` | Parse `v1/orders` → (version, `orders`) using request Host |
| `action_view_endpoints()` | UI: open routes for this version |

---

### `models/core_api_application.py` — `core.api.application`

External application registration (like an OAuth client).

#### Fields (grouped)

| Group | Fields |
|-------|--------|
| Identity | `name`, `client_id`, `client_secret` (hashed), `state`, `active` |
| Tokens | `token_ttl_hours`, `refresh_token_ttl_hours`, `token_ids`, `active_token_id` |
| Routes | `endpoint_ids` (One2many), `default_version_id` |
| Security | `rate_limit_per_minute`, `auth_rate_limit_per_minute`, `allowed_ips` |
| Audit | `log_ids`, `last_auth_at`, `last_auth_ip` |
| UI guide | `api_base_url`, `auth_endpoint_url`, `auth_curl_example`, `api_call_curl_example`, `api_database_name` |

#### Compute methods

| Method | Description |
|--------|-------------|
| `_compute_active()` | `active = (state == 'active')` |
| `_compute_token_count()` | Count `token_ids` |
| `_compute_log_count()` | Count `log_ids` |
| `_compute_api_integration_guide()` | Build cURL examples for form "Authentication Guide" tab |
| `_compute_active_token()` | Current valid **access** token for UI display |

#### CRUD & credentials

| Method | Description |
|--------|-------------|
| `create(vals_list)` | Auto-generate `client_id`, hash `client_secret`, store plaintext in session for wizard |
| `_generate_client_id()` | `app_{32 hex chars}` |
| `_store_pending_secret(plaintext)` | Save in `request.session['core_api_application_secrets']` |
| `_pop_pending_secret()` | Read + remove from session |
| `_clear_pending_secret()` | Remove from session |
| `_open_secret_wizard(plaintext)` | Open one-time credentials popup |

#### UI actions

| Method | Description |
|--------|-------------|
| `action_view_credentials()` | Show secret wizard (only if `credentials_pending`) |
| `action_regenerate_secret()` | New secret + wizard |
| `action_set_active()` / `action_set_inactive()` | Toggle state |
| `action_revoke_token()` | Revoke current access token |
| `action_view_tokens()` | Open token list |
| `action_view_logs()` | Open request logs |

#### Runtime API (called during HTTP requests)

| Method | Description |
|--------|-------------|
| `set_api_response(data)` | **Model method.** Sets `request.core_api_response = data`. Call from Server Action. |
| `get_api_context()` | Returns dict of `core_api_*` keys from `env.context` |
| `check_ip_allowed(ip)` | Raises `AccessError` if IP not in `allowed_ips` CIDR list |
| `check_api_rate_limit()` | Raises if API calls/min exceeded (uses `core.api.log`) |
| `check_auth_rate_limit()` | Raises if auth calls/min exceeded |
| `authenticate_client(client_id, secret, ip)` | Verify credentials; update `last_auth_at`; return app or empty |
| `check_api_access(endpoint_code, version_id)` | Raises if app has no route with that `code` on that version |
| `check_route_access(path, method)` | Match path against owned `route_pattern` list |

---

### `models/core_api_endpoint.py` — `core.api.endpoint`

One row = one public URL handled by one Server Action.

| Field | Description |
|-------|-------------|
| `code` | Permission key (unique per app + version) |
| `version_id` | Which API version (`/api/v1`, …) |
| `route_suffix` | Path after version: `orders` → `/api/v1/orders` |
| `route_pattern` | Computed full path |
| `http_methods` | Comma-separated allowed methods |
| `action_id` | `ir.actions.server` to execute |
| `application_id` | Owning application |

| Method | Description |
|--------|-------------|
| `_compute_route_pattern()` | `{version.path_prefix}/{route_suffix}` |
| `_check_application_id()` | Every route must have an application |
| `_check_route_suffix()` | Suffix cannot be empty |
| `create(vals_list)` | Auto-fill `application_id` / `version_id` from form context |
| `_normalize_route_suffix(suffix)` | Strip slashes |
| `_migrate_legacy_route_fields()` | Post-install: parse old `route_pattern` into version + suffix |
| `_parsed_methods()` | Split `http_methods` string → list |
| `allows_method(method)` | Check if HTTP method allowed |
| `find_for_request(path, method, application)` | Longest-prefix match among app's active routes |
| `_parse_request_body(httprequest)` | JSON body → dict |
| `_server_action_context(application, httprequest)` | Build full context dict for Server Action |
| `_run_server_action(application, httprequest)` | Execute action; read `request.core_api_response`; return JSON HTTP |
| `_error_response(message, status)` | JSON error helper (400) |
| `dispatch(application)` | Verify ownership + `check_api_access`; run action |
| `dispatch_request(path, application)` | **Entry from proxy**: find route → `dispatch()` |

> **Note:** `dispatch()` currently returns before the `try/except` block (dead code below line 291). Errors from Server Actions are not caught by the intended handler yet.

---

### `models/core_api_token.py` — `core.api.token`

| Constant | Value | Purpose |
|----------|-------|---------|
| `TOKEN_SIZE` | 32 bytes | Random token length |
| `INDEX_SIZE` | 8 chars | DB lookup prefix |
| `TOKEN_CRYPT_CONTEXT` | pbkdf2_sha512 | Hash storage |

| Method | Description |
|--------|-------------|
| `_generate_plaintext()` | `hex(urandom(32))` |
| `_create_token_record(app, type, expiration, pair_id, name)` | Create DB row; return `(plaintext, record)` |
| `_expiration_from_hours(hours)` | Now + hours, or `False` if 0 |
| `_revoke_active_tokens(domain)` | `active=False` on matching tokens |
| `issue_for_application(app, revoke_existing=True)` | Create access+refresh pair; optionally revoke all app tokens first |
| `_authenticate_token(plaintext, token_type)` | Lookup by index + verify hash; update `last_used_at` |
| `authenticate(plaintext)` | Access token only |
| `authenticate_refresh(plaintext)` | Refresh token only |
| `refresh_for_application(refresh_plaintext)` | Validate refresh → revoke pair → issue new pair |
| `action_revoke()` | UI: revoke token + its pair |
| `_gc_expired_tokens()` | Autovacuum: deactivate expired tokens |

---

### `models/core_api_log.py` — `core.api.log`

Audit log for auth and API calls. Also used for **rate limiting** (count rows in last minute).

| Method | Description |
|--------|-------------|
| `log_event(...)` | Create one log row |
| `count_recent(domain_extra, minutes=1)` | Count rows for rate limit checks |
| `_gc_old_logs()` | Autovacuum: delete logs older than 90 days |

---

### `models/action_endpoint.py` — `action.endpoint.manager`

UI tool to auto-generate Server Actions from `@endpoint` decorated methods.

| Field | Description |
|-------|-------------|
| `model_id` | Target `ir.model` to scan |
| `generated_action_ids` | Created `ir.actions.server` records |

| Method | Description |
|--------|-------------|
| `_generate_endpoints()` | `inspect.getmembers` on model class; find `_is_endpoint`; create/update Server Action with code `model.{method}()` |
| `action_generate_endpoints()` | UI button → `_generate_endpoints()` + success notification |

Generated action code calls `model.{method}()` — the method must call `set_response()` internally.

---

### `models/ir_actions_server.py` — inherit `ir.actions.server`

| Field | Description |
|-------|-------------|
| `endpoint_manager_id` | Link back to `action.endpoint.manager` that created this action |

---

### `models/my_api_service.py` — example

```python
class MyApiService(models.Model):
    _inherit = 'res.partner'

    @endpoint('ping')
    def api_ping(self):
        return set_response(data="Hello World")
```

Example of `@endpoint` on an inherited model. Generate via **Configuration → Endpoints**.

---

## 8. utils/ — shared helpers

### `utils/__init__.py`

Public exports used by controllers and other addons.

### `utils/core_api_utils.py`

| Function | Description |
|----------|-------------|
| `endpoint(name=None)` | Decorator: marks method `_is_endpoint=True`, wraps with `@api.model` |
| `route(route, name)` | Alternate decorator ( `_is_route` ) — reserved for future use |
| `get_context(kw, body, ctype='http')` | Returns `{core_api: {params, body, is_json}}` for `with_context` |
| `_extract_context(obj)` | Get context from self/env/request/dict |
| `get_params(obj=None)` | Read URL/form params from `core_api` context or `request.params` |
| `get_body(obj=None)` | Read JSON body from context or raw request |
| `set_response(data, message, status_code)` | Build standard JSON payload → `set_api_response()` |

**Standard `set_response` output shape:**

```json
{
  "status_code": 200,
  "status": "success",
  "message": "Thao tác thành công",
  "data": { ... }
}
```

### `utils/security.py`

| Function | Description |
|----------|-------------|
| `get_client_ip()` | `REMOTE_ADDR` from WSGI environ |
| `get_request_hostname(httprequest)` | `Host` header without port, lowercased |
| `check_ip_allowed(allowed_ips_text, ip)` | Match IP against newline-separated IPs/CIDRs |
| `check_rate_limit(env, domain, limit, msg)` | Count `core.api.log` rows in last minute; raise `AccessError` |
| `check_application_api_rate_limit(app)` | Per-app API limit |
| `check_application_auth_rate_limit(app)` | Per-app auth limit |
| `check_ip_auth_rate_limit(env, ip, limit=30)` | Global per-IP auth throttle |

### `utils/logging.py`

| Function | Description |
|----------|-------------|
| `log_core_api(event_type='api')` | Decorator factory. Wraps controller methods; logs duration + status to `core.api.log` on success or exception |

### `utils/exception.py`

| Class / Function | HTTP | Description |
|----------------|------|-------------|
| `CoreApiBadRequest` | 400 | Base client error |
| `CoreApiInvalidBody` | 400 | Body not JSON dict |
| `CoreApiMissingData` | 400 | Required fields missing |
| `CoreApiInvalidData` | 400 | Invalid field value |
| `CoreApiInvalidResponse` | 400 | `set_api_response` got non-dict |
| `ensure_dict(data)` | — | Raise if not dict |
| `require_fields(data, fields)` | — | Raise if any field missing/empty |

---

## 9. wizard/ — UI popups

### `wizard/core_api_application_secret_wizard.py`

`core.api.application.secret.wizard` — transient model.

| Field | Description |
|-------|-------------|
| `application_id` | Parent app |
| `client_id` | Shown once |
| `client_secret` | Plaintext shown once |

| Method | Description |
|--------|-------------|
| `action_confirm()` | Set `credentials_pending=False`, clear session secret, close popup |

---

## 10. security/ & data/ & views/

### `security/groups.xml`

| XML ID | Description |
|--------|-------------|
| `group_core_api_manager` | Full access to Core API menus; implied `base.group_user` |

### `security/ir.model.access.csv`

ACL for managers: full CRUD on application, endpoint, token, wizard; read-only on logs.

### `security/core_api_domain_access.xml` / `core_api_version_access.xml`

Model access for domain and version (defined via XML search to avoid load-order issues).

### `security/t4_coreapi_rules.xml`

Empty — access controlled via CSV only.

### `data/core_api_domain_data.xml`

Creates **Default** domain (`is_default=True`, no hostname).

### `data/core_api_version_data.xml`

Creates **v1** and **v2** on default domain.

### `data/core_api_domain_migrate.xml` / `core_api_version_migrate.xml`

Upgrade hooks: assign `domain_id` / backfill version fields on existing records.

### `views/*.xml`

| File | UI for |
|------|--------|
| `menu_views.xml` | Core API root menu + Configuration submenu |
| `core_api_application_views.xml` | App form: credentials, routes, auth guide |
| `core_api_endpoint_views.xml` | Standalone route list/form |
| `core_api_domain_views.xml` | Host domain management |
| `core_api_version_views.xml` | Version management |
| `core_api_token_views.xml` | Token list + revoke button |
| `core_api_log_views.xml` | Request log viewer |
| `core_api_action_endpoint_views.xml` | Endpoint manager + generate button |
| `wizard/..._views.xml` | Credentials popup |

---

## 11. t4_coreapi_boot companion

**Path:** `server/addons/t4_coreapi_boot/` (NOT inside `t4_coreapi`)

**Why separate:** `t4_coreapi` must NOT be in `server_wide_modules` (blocks uninstall). Boot module is tiny and server-wide only for DB selection.

### `http_patch.py`

| Function | Description |
|----------|-------------|
| `patch_api_db_selection()` | Monkey-patch `http.Request._get_session_and_dbname` once at server start |

**Patched behavior:** If URL path starts with `/api/` and no DB yet selected:
1. Try `?db=` query param
2. Use `?db=` in the URL
3. Return that DB name so Odoo registry loads before routing

### `odoo.conf`

```ini
server_wide_modules = base,rpc,web,t4_coreapi_boot
```

---

## 12. How to extend

### Option A — Gateway route (recommended)

1. Create **Application** in UI
2. Add **Gateway Route**: version + suffix + Server Action
3. Server Action Python:

```python
body = env.context.get('core_api_body') or {}
# business logic...
env['core.api.application'].set_api_response({
    'status': 'success',
    'data': result,
})
```

### Option B — `@endpoint` code generation

1. Add method with `@endpoint('Name')` on a model
2. Call `set_response(...)` inside the method
3. **Configuration → Endpoints** → select model → Generate
4. Link generated Server Action to a gateway route

### Option C — Custom controller in another addon

```python
from odoo import http
from odoo.addons.t4_coreapi.controllers.main import CoreApiController

class MyController(CoreApiController):
    @http.route('/api/v1/custom', auth='core_api', csrf=False)
    def custom(self, **kw):
        app = self._get_application()  # from ir_http context
        ...
```

---

## Quick reference — which file answers which question?

| Question | Look in |
|----------|---------|
| Where is Bearer token validated? | `models/ir_http.py` |
| Where is `core_api_application_id` set? | `models/ir_http.py` → `request.update_context` |
| Where is it read in controllers? | `controllers/base.py` → `_get_application` |
| Where are tokens issued? | `controllers/auth.py` + `models/core_api_token.py` |
| Where is route matched? | `models/core_api_endpoint.py` → `find_for_request` |
| Where does Server Action run? | `models/core_api_endpoint.py` → `_run_server_action` |
| How to return JSON? | `set_response()` or `set_api_response()` |
| How to read request body in action? | `env.context['core_api_body']` or `get_body()` |
| Rate limits? | `utils/security.py` + `models/core_api_log.py` |
| Host-based v1 vs v2? | `models/core_api_domain.py` + `core_api_version.py` |
| DB selection for Postman? | `t4_coreapi_boot/http_patch.py` |

---

**Module version:** see `__manifest__.py` (`19.0.6.0.1`)
