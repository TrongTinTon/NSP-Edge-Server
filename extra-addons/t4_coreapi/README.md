# T4 Core API

Generic external API gateway for Odoo 19.

## Scope

This module contains only reusable Core API infrastructure:

- Applications and Client ID / Client Secret credentials
- access and refresh token families
- multiple independent clients per Application
- optional `client_instance_id` for device/process identification
- bearer authentication
- service-code and endpoint authorization
- API domains and versions
- IP allowlists and rate limits
- request audit logs
- endpoint actions and route generation

It intentionally contains no product-specific Controller, Zeroconfig, pairing,
handshake, discovery, blacklist, or NSP business models.

## Multi-client behavior

One Application may be shared by multiple clients. Each successful
`client_credentials` request creates a new independent token family. Existing
families are preserved.

A client may send an optional stable identifier:

```json
{
  "grant_type": "client_credentials",
  "client_id": "app_...",
  "client_secret": "...",
  "client_instance_id": "EDGE-01-WORKER-A"
}
```

Refreshing a token rotates only the family owning that refresh token. Other
clients using the same Application remain active. Concurrent reuse of the same
refresh token is serialized with a PostgreSQL row lock; only the first rotation
succeeds.

## Routes

- `POST /auth/token`
- `/<service_code>/<version>/<route>` with `Authorization: Bearer <token>`

## Operational rule

Regenerating an Application secret changes future authentication credentials;
existing token families remain valid until expiration or explicit revocation.
Use **Revoke All Active Tokens** when the Application credential is compromised.


## Credential visibility

Authorized Core API managers may reopen **View Credentials** without a view-count limit.
Applications upgraded from a hash-only release must regenerate the secret once; newly generated
secrets remain available to authorized managers.

## Route configuration

Endpoint definitions accept only `route_path`, for example `vehicles/sync`. Core API automatically
builds the gateway path from the owning Application Server Code and API Version, for example
`/gatekeeper/v1/vehicles/sync`. A pasted full gateway path is normalized back to its Route Path.
