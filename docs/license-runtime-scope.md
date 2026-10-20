# Runtime licensing and automatic recovery

This change implements the already-selected scope; it does not change any
installation's licence, master exemption, scope, offline grace, or production
settings. A valid licence stops nothing. A vendor outage alone does not cut
paid users unless the existing configured silence policy actually expires.

## Boundaries

- Panel: ordinary existing JWTs are checked, not just new logins. The exact
  licence recovery routes remain available to authenticated superadmins.
  Vendor recovery JWTs must carry a signed boolean marker and still resolve
  to a superadmin; no passwordless path is introduced.
- Bot: reject new mutations at the in-process bridge, HTTP API and Mini App
  boundaries. Existing accepted-operation finalization remains available.
- VPN: RADIUS rejects new admissions; accounting remains available. A retryable
  poll pass stops existing PPP sessions and disables WG, Xray and SoftEther
  through the existing ownership gate/runner. No customers, quotas, purchases,
  receipts or financial records are deleted.

## Automatic restoration after renewal

Each connection is marked before stop. A confirmed stop is not repeatedly sent;
an unknown result remains pending and retries. Renewal automatically restores
only marked, currently eligible connections, ordinarily on the next poll pass.
PPP becomes eligible for a fresh login; it does not resurrect an old session.
The marker is cleared only after a verified remote result and fresh checks.

Fresh checks include the connection switch, customer/service status, current
quota and expiry, session limit, node availability, renewal-void hold and
pending reseller cancellation and unfinished durable operations. A purchase-only account block is not a VPN
block. Quota processing preserves the marked connection switch, while still
updating service status/usage, so expiry during a licence cut cannot strand a
subsequent legitimate renewal. Administrator disabling still wins.

An internal fingerprint binds the suspension to the original customer,
credential, purchase and node endpoint, not mutable usage/expiry. A replacement
or changed target is not enabled automatically. Unknown enable results cause
a compensating stop and retain the marker for retry; never report success.

## Local concurrency fence and operational limits

Before application startup work, every application engine transaction acquires
a shared `license-runtime.lock` under `gate_locks.lock_base_dir()`. It retains
the lock through commit/rollback until pool check-in. Each individual licence
cut or restore holds the exclusive lock across fresh reads, detached runner
I/O and its final commit. No SQL transaction is held across licence node I/O.
The read-only runner child uses its existing restricted independent engine.

This serializes changes from HTTP, bot, poller, RADIUS and accepted-operation
database transactions against restoration. It is a same-host fence, not a
distributed lock. Full service control supports the project's standard single
backend process with its background threads, not HA or a multi-worker licence
cache. Stop/recreate the backend rather than mixing old/new processes. Direct
database edits or external writers bypass this guarantee. A separate remote
bot must use the authenticated HTTP boundary, not write the panel DB directly.

Exclusive work is per connection, not across the entire customer batch.
Licence actions require the enforced P6 child runner and use a three-second
hard deadline instead of the general 120-second default. An unknown node is
backed off for sixty seconds; each pass starts at most sixteen transitions
within a ten-second budget (the final in-flight action may overrun that budget).
Previously confirmed cuts are not included in subsequent cut batches. Invalid
services do not consume the remote-attempt budget. A large installation is
processed over multiple poll passes, not in one global stop-the-world batch.
Ordinary transaction lock waits time out after ten seconds, exclusive recovery
waits after one second, both with a retryable busy error.
Do not interpret busy or an unknown node outcome as licence restoration.

Service periods and queued renewals are not frozen or extended by a licence
cut. Accounting and normal expiry processing continue; the restoration check
uses their current committed result, not the service's pre-cut entitlement.

Cutting a live installation or changing its vendor-side scope is not part of
test execution. Tests use temporary databases and fake remote transports only.
Production scope changes and real customer disconnection require separate
owner approval and readiness review.
