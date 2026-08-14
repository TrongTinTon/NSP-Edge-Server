# Friendship route upgrade ParseError fix — 19.0.9.1.6

## Failure

During upgrade, `sync_route_definitions.xml` could fail on `api_friendships` if the
new Python catalogue had already self-created `edge/friendships/snapshot` before
module XML data was loaded.

## Root cause

The self-healing route existed in `ir.actions.core_api`, but it had no canonical
`nsp_sync.api_friendships` external ID. XML loading attempted to create the same
route again and hit the Core API uniqueness constraint.

## Fix

- pre-migration adopts an existing `nsp.sync.job` Friendship route into
  `nsp_sync.api_friendships` before XML loading;
- runtime self-healing immediately creates/repairs the external-ID binding when
  it resolves the Friendship descriptor.

No transport direction or API contract changes are introduced.
