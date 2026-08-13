# Parking Log Sync canonical route — 19.0.10.58.0

`edge/parking-logs` serialization resolves its route from the live `sync_action_id.route_suffix`, falling back to the stored job projection only when no action exists. This avoids false `Unsupported push route` failures when stored Sync Job metadata is stale after a route rename.

The Edge Sync Job repair hook remains backward-compatible with nsp_sync 19.0.9.1.1 and delegates to nsp_sync once 19.0.9.1.2 is installed.
