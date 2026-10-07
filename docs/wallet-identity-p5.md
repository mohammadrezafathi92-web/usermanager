# P5 normal-generation identity rebind

Telegram changes/link/unlink, customer owner transfers, reseller reparenting
and admin deletion use `wallet_identity.change` / `synchronize_hierarchy`.
The User fields, account binding, completed operation and audit row are
committed by the existing caller in one transaction. Helpers never commit,
roll back caller work, perform remote calls or change customer balances.
Account lineage and historical owner snapshots stay immutable.

Source identity with remaining debt refuses with 409, including changes
within the same tenant. Closed debt remains attached to its old identity.
Identities are row-locked in ascending ID order, then the account and User
are revalidated. SQLite obtains its writer lock without discarding pending
caller edits. MariaDB snapshot/deadlock conflicts return retryable 503;
only a fresh whole-request transaction may retry, never one statement.

Optional UUID tokens deduplicate the complete operation; a different
request with the same token refuses. Existing endpoints generate tokens
internally and exact repeated, unchanged requests produce no extra audit.

Hierarchy synchronization runs before its existing commit. A failure must
roll back the hierarchy edits as well as every account rebind. No schema
changes, historical backfill, wallet lots, cutover or mode activation.
Schema-unready installations and pre-P5 users without accounts retain
legacy edits. P9 backfill remains required before enforcement.

This writer is deliberately limited to normal mode. P8 resource fencing
and the full financial lock protocol must precede enforced mode; this
batch does not claim a fenced financial implementation. Hierarchy changes
scan only live accounts owned by the affected subtree after flushing
hierarchy edits. Unrelated tenants are neither scanned nor repaired.
Authorization remains in existing endpoints, not in the helper.

Unchanged identity fields posted by forms do not acquire a wallet writer
lock. If a profile edit changes both identity and status, its DB-only
identity unit commits before the existing legacy node reconciliation.
Remote reconciliation is not made atomic by this batch; durable P6 remains
required. No SQLite wallet writer lock is held across that node call.
