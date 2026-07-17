# Zeroconfig and NSP Gatekeeper Controllers

Zeroconfig is a discovery/bootstrap channel in T4 Core API. It does not create a separate Controller Application menu and it does not add Controller-specific pages to Core API Applications.

Runtime flow:

1. Controller discovers the Odoo server through `_nsp._tcp.local`.
2. Controller calls `POST /nsp/handshake`.
3. The payload should include `service_code`, `api_key`, `bootstrap_key`, and `controller_url`.
4. `client_id` is mandatory. `service_code` is server-managed and used only as the Core API gateway path segment.
5. T4 Core API resolves or creates a normal `core.api.application` using `service_code`.
6. NSP Gatekeeper creates or updates `nsp.controller` where `controller_id = service_code`.
7. T4 Core API issues a bearer token for that Core API Application.
8. The Controller uses that bearer token for NSP Gatekeeper APIs.

Management is separated:

- T4 Core API manages generic API Applications, credentials, tokens, logs, rate limits, and Zeroconfig discovery settings.
- NSP Gatekeeper manages Controllers, Branches, Gates, Devices, and Vehicle In/Out Logs.
- NSP Sync manages Push/Pull Protocol and Sync State, but reuses Core API Applications for remote authentication.

Core API standard endpoints:

- Token endpoint: `POST /auth/token`
- Gateway format: `/<service_code>/v1/<route>`
- NSP Sync routes:
  - `POST /<service_code>/v1/nsp-sync/changes`
  - `POST /<service_code>/v1/nsp-sync/apply`
  - `POST /<service_code>/v1/nsp-sync/status`
