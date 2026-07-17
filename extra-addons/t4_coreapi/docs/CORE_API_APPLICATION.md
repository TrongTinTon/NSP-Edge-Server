# Core API Application

A Core API Application represents one credential and its allowed API routes.
It does not carry an Application Type/Kind.

Authorization is determined by:

- Client ID / Client Secret
- Active state
- Allowed endpoints
- IP/rate limits
- Edge Server or Controller code validated by the business API

One Application may be shared by multiple node-specific Controller credentials.
A new authentication does not revoke other active token pairs.
