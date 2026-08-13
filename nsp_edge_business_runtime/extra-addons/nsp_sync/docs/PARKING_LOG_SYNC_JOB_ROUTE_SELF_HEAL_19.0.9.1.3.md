# Parking Log Sync Job route self-heal — 19.0.9.1.3

- Core API action route is the runtime source of truth.
- Stored `nsp.sync.job.route_suffix` is only a projection and may be stale after route rename.
- Default-job repair resolves existing jobs from `sync_action_id.route_suffix` before creating replacements.
- Scheduler-owned `_ensure_edge_sync_jobs()` now lives in `nsp_sync`, not an optional business adapter.
- Run Now surfaces the actual failed Last Message instead of always showing a success toast.
- Post-migration converges stored action/route metadata from `ir.actions.core_api`.
