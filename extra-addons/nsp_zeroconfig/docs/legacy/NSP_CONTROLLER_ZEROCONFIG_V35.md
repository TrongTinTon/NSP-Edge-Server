# NSP Controller Zeroconfig v35

## Design

Zeroconfig is reintroduced only for Controller discovery/bootstrap.

It does:
- Authenticate an existing Core API Application by client_id/client_secret.
- Find or create the related `nsp.controller` runtime identity.
- Link the controller to the Core API Application.
- Return Server Code, gateway base, access token and controller info.

It does not:
- Create Core API Applications.
- Create Sync Jobs or Sync Authentication.
- Add Application Type.
- Resolve NSP Sync as Controller.
- Check Controller for NSP Sync endpoints.

## Endpoints

- `POST /nsp/zeroconfig/controller?db=<dbname>`
- `POST /nsp/handshake?db=<dbname>` legacy alias
- `GET /api/t4_coreapi/v1/zeroconfig/status` backend status

## Sample request

```json
{
  "client_id": "<core-api-client-id>",
  "client_secret": "<core-api-client-secret>",
  "controller_name": "Gate Controller 01",
  "controller_url": "http://192.168.1.10:6000"
}
```

## Sample response

```json
{
  "ok": true,
  "data": {
    "controller": {
      "controller_id": "<server_code>",
      "server_code": "<server_code>",
      "branch_timezone": "Asia/Ho_Chi_Minh"
    },
    "application": {
      "client_id": "...",
      "server_code": "...",
      "gateway_base": "/<server_code>/v1"
    },
    "api_token": {"token": "..."},
    "refresh_token": {"token": "..."}
  }
}
```
