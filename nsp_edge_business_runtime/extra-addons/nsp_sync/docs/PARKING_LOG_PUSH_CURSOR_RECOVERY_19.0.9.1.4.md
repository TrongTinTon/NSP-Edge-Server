# Parking Log push cursor recovery — 19.0.9.1.4

- `edge/parking-logs` remains an outbound NSP Sync route.
- Upgrading resets its durable push cursor once so local immutable Parking Logs are replayed.
- Replay is safe because Cloud deduplicates by `log_uid`.
- `Generate API Actions Only` on `NSP Sync Remote Routes` now synchronizes the outbound action catalogue directly; no fake `@endpoint` methods are added to `nsp.sync.job`.
- Generic Core API endpoint managers keep the original `@endpoint` introspection behavior.
