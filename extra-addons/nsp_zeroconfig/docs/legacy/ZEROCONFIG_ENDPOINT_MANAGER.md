# Zeroconfig Endpoint Manager Selection

Zeroconfig no longer creates NSP Gatekeeper gateway routes from a hardcoded endpoint manager.

## Configuration

Open:

```text
T4 Core API → Configuration → Zeroconfig
```

Set **Endpoint Manager** to an existing record from:

```text
T4 Core API → Configuration → Endpoints → Action Endpoints Management
```

During `/nsp/handshake`, Core API will:

1. Resolve or create the Core API Application by `client_id`.
2. Issue access/refresh tokens.
3. Create/update the NSP Controller record.
4. Create/update Gateway Routes only from the selected Endpoint Manager.

## No Endpoint Manager selected

If **Endpoint Manager** is empty, Core API will not create or update Gateway Routes during Zeroconfig handshake.

The handshake can still succeed and return the current `server_code`, token, and controller config, but no runtime APIs such as these will be provisioned automatically:

```text
/{server_code}/v1/heartbeat
/{server_code}/v1/devices/report
/{server_code}/v1/gate-config/sync
/{server_code}/v1/parking/logs/push
```

This prevents accidental route creation when the administrator has not explicitly selected which API catalog should be exposed.
