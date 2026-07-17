# Client ID / Server Code Zeroconfig

Zeroconfig handshake now separates client identity from gateway routing.

- Client ID (`client_id`) identifies the controller across handshakes.
- Server Code (`core.api.application.service_code`) is the first Core API gateway URL segment.
- Server Code is unique and server-managed. It can be changed in Core API Application for management purposes.
- A controller re-handshakes by Client ID to learn the latest Server Code.
- During first bootstrap, `requested_server_code` is only a hint. If it is already used, Core API generates a unique Server Code.

Runtime API format remains:

```text
/{server_code}/v1/{route_suffix}
```

Handshake endpoint remains:

```text
POST /nsp/handshake
```
