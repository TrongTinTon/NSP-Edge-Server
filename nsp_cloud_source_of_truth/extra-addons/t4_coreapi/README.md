# T4 Core API

Generic external API gateway for Odoo 19.

## Authentication contract

Clients authenticate with only:

```json
{
  "client_id": "app_...",
  "client_secret": "..."
}
```

The response contains one bearer access token and its expiry. When a token expires,
the client authenticates again with the same credentials.

Many clients may share the same Client ID and Client Secret. Every successful
authentication creates a new independent access token and does not revoke tokens
already used by other clients.

## Route authorization

API calls use:

```text
/<version>/<route_path>
Authorization: Bearer <access_token>
```

The token identifies the Core API Application. The gateway accepts a request only
when the requested Route Path is active and belongs to that Application. Controller
Code, Edge Server Code, and other payload fields are business data and do not select
or expand Core API permissions.

## Scope

- Applications and shared Client ID / Client Secret credentials
- independent bearer access tokens
- route-path authorization
- API domains and versions
- IP allowlists and rate limits
- request audit logs
- endpoint actions and route generation

Regenerating a Client Secret affects future authentication only. Existing access
tokens remain valid until expiration or explicit revocation. Use **Revoke All Active
Tokens** when shared credentials are compromised.
