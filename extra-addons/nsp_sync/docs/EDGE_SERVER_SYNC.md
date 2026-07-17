# NSP Sync on Edge Server

`nsp_sync` is installed only on the Edge Server. Cloud does not install this module.

The Edge Server owns Cloud authentication, pull/push jobs, opaque cursors, local sync records, retry, and backoff. It updates local queue state directly from each Cloud API batch response (`processed`, `duplicate`, or `rejected`). There is no callback API for sync status.

Every Edge Server request to Cloud includes `edge_server_code`. Cloud validates the code against the authenticated Core API Application and its node scope.

Topology Sync is not used. Branch, Gate, Measurement, Card, User, Vehicle, and related configuration are synchronized through dedicated APIs only.
