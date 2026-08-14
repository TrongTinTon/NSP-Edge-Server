# Cloud Master Data Pull — 19.0.9.1.6

Master Data synchronization is one-way: Cloud -> Edge.

- Adds `edge/friendships/snapshot` as a pull-only route.
- Users, Vehicles, Friendships and Vehicle Borrows are Edge runtime projections.
- No Edge -> Cloud route exists for these Master Data models.
- Existing default Users/Vehicles/Vehicle Borrows polling intervals are moved from five minutes to one minute.
- Parking Logs remain an Edge -> Cloud runtime/audit flow and are not Master Data synchronization.
