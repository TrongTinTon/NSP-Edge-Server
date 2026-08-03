# NSP Odoo Audit Patch Notes

Applied safe notification fix:

- Added FCM HTTP v1 provider configuration and delivery adapter.
- Added APNS token-authenticated HTTP/2 provider configuration and delivery adapter.
- Added durable pending queue, retry scheduler, attempt tracking, provider message ID and manual retry.
- External provider failures do not roll back Parking Transactions.
- `nsp_notification` version is now `19.0.5.0.0`.

Python requirements:

- `PyJWT` (import name `jwt`)
- `httpx`
- `h2`

Known architectural blockers are documented in `NSP_STATIC_AUDIT_2026-08-01.md` and are not hidden by this patch.
