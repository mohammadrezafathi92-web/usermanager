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
