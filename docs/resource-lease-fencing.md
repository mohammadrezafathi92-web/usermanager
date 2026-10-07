# Resource lease fencing — Receipt Void 8 / Lifecycle 10.1

DB-only lease acquire/renew/release use the database clock, never the process
clock. Acquire increments a persistent fencing epoch only when unowned or
expired. Renewal requires the same owner/epoch and a still-live lease. An old
token cannot release a newer holder's lease. Owners are explicit operation
attempts; resource keys use the canonical identity/card-pool/node-pool/operation
names. TTL, key and token inputs are validated before insertion.

The caller commits acquisition separately. Each business transaction starts
through `begin_business` before its runtime/lease locking reads: SQLite takes
BEGIN IMMEDIATE; MariaDB locks the selected lease rows with FOR UPDATE.
`revalidate` checks every key's owner, fencing epoch and SQL-clock liveness in
sorted order. The transaction marker is tied to that SessionTransaction, not
reusable after commit/rollback. Remote calls must remain outside it. Caller
rolls back any exception and releases only after complete business completion
or rollback; no helper commits implicitly.

Tests cover rollback, expiry without takeover, renewals, stale epoch and owner,
stale releases, transaction-marker misuse and two actual competing sessions.
Disposable MariaDB is mandatory in CI. No live mutation is wired to these
helpers yet. They do not replace the non-expiring remote node gate and do not
make HA or remote exactly-once safe by themselves.
