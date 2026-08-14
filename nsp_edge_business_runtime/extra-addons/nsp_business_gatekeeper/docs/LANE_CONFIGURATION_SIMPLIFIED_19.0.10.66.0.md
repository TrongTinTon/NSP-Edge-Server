# Lane Configuration UI simplification — 19.0.10.66.0

Edge Lane Configuration now exposes only operational context at the top of the form:
Parking Layout, Branch and Active.

Removed from the visible form header:
- informational alert;
- Context section label;
- Configuration section label;
- Controller field;
- Configuration State badge;
- Configuration Issue.

The Device Configuration tree remains the place to inspect Controller → Reader topology.
The Antenna Sequence table no longer displays the technical sequence index. The backend
`sequence` value and `default_order="sequence asc, id asc"` remain unchanged, so runtime
ordering and matching semantics are unaffected.
