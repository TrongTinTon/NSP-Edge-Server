# Core API Application

A Core API Application represents one shared credential and its allowed Route Paths.

Authentication requires only:

- Client ID
- Client Secret

Many clients may use the same credential. Each authentication creates an independent
access token and never revokes another client's token.

Authorization is determined by:

- the Application identified by the access token;
- the active Route Path assigned to that Application;
- the HTTP method allowed by the route;
- Application active state, IP allowlist, and rate limits.

Controller Code, Edge Server Code, and similar payload values are validated by the
business endpoint after Core API authorization. They do not choose an Application or
grant access to a Route Path.
