# Cloud Master Data Projection — 19.0.10.69.0

Edge treats Users, Friendships, Vehicles and Vehicle Borrows as Cloud-owned runtime projections.

- Adds pull application for the Friendship snapshot.
- Reconciles Friendship deletions from the full Cloud snapshot.
- Blocks non-system local writes to Cloud Master Data on Edge.
- Vehicle Borrow continues to be synchronized from Cloud and is used by Check-out authorization.
- Parking Logs remain Edge runtime/audit records and may be pushed to Cloud.
