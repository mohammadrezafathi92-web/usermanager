# P5: account construction and tombstones

Every application User constructor now goes through
`wallet_accounts.create_user_with_wallet`. If the Receipt Void schema is
ready, a fresh account/lineage is created in the same transaction as the
User. Telegram identities are shared only within the owner's hierarchy
root; Telegram-less accounts have private identities. The factory neither
commits nor contacts remote nodes. No balance, source, lot, or debt is created.

`delete_user_cascade` detaches the wallet account in its existing final DB
transaction. The account and identity survive; a reused User ID cannot
reuse its lineage. Creation/deletion are refused outside `normal` before
remote deletion starts. Pre-P5 users with no account remain deletable in
normal mode. No automatic account backfill or wallet cutover runs.

Legacy installations with an unavailable Receipt Void schema retain old
behavior without attempting partial account writes. P9 must verify the
schema and backfill before any activation.

This is **not** the full identity-rebind batch: existing Telegram/owner
changes retain legacy behavior in normal mode. Until audited rebind is
implemented, their accounts may have stale identity bindings. The prepared
enforced wallet policy now detects that mismatch and refuses rather than
using the wrong identity. Rebind, hierarchy-root transfers, debt-aware new
account creation, DB-only deletion finalizer, and backfill remain activation
prerequisites. Do not activate enforced mode with this batch alone.

Tests cover transactional rollback/commit failure, tenant isolation,
private identity, tombstone retention, explicitly reused User ID, and
normal-only lifecycle. SQLite runs with foreign keys enabled. Real MariaDB
is mandatory in CI through a freshly created `_mariadb_scratch` database.
An AST check rejects any application User constructor outside the factory.

Identity insertion never updates an existing identity: SQLite uses a
targeted conflict-do-nothing and MySQL/MariaDB uses insert-ignore on this
immutable, bounded-value table. An exact current locking read validates
the identity and its Telegram value; a newly inserted row's owner snapshot
must match too. Missing/mismatched data refuses the operation. No 1020,
deadlock or other database exception is caught and treated as success.
MariaDB 11.6.2+ defaults to snapshot isolation, where a locking read of a
row outside the snapshot can roll back the entire transaction with 1020.
See [MariaDB's snapshot-isolation contract](https://mariadb.com/docs/server/server-usage/storage-engines/innodb/innodb-system-variables).
Transient MariaDB creation conflicts return 503
`wallet_creation_retry_required`; the factory does not commit, roll back,
or retry just part of its caller's unit of work. The caller/request must
roll back and replay the whole transaction. CI forces a stale snapshot and
checks that a fresh whole-request retry preserves both creations with one
identity. The panel refunds its already-committed reseller debit when
construction fails. This batch does not add automatic creation retries to
legacy callers. The earlier unsafe statement-only retry was removed.
