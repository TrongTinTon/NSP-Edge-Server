# Cloud Master Data Pull — 19.0.13.47.0

Cloud remains the only source of truth for Users, Friendships, Vehicles and Vehicle Borrows.

- Adds the pull-only `edge/friendships/snapshot` endpoint.
- Existing Users, Vehicles and Vehicle Borrows snapshot endpoints remain Cloud -> Edge.
- No endpoint accepts Edge mutations for those Master Data models.
- Parking Log ingestion now rejects `borrow_uid` when the referenced Cloud Vehicle Borrow does not exist.
