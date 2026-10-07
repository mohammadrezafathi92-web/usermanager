# Fenced DB deletion completion — partial Lifecycle L2

`provisioning_deletion.finish` performs Connection/Purchase/User DB finalization
and operation completion in one caller-owned transaction. It never commits,
contacts a node, refunds money, or activates a mode. Its caller must use the
resource-lease business transaction protocol and revalidate host/runtime/node
contracts; both customer-identity and operation leases from the same attempt
are required. Current implementation only supports the normal wallet generation.

Deletion T1 must persist `snapshot()` in the operation intent before disabling
connections. It stores resource IDs and one-way remote-identity fingerprints,
not credentials. User deletion additionally snapshots all Purchase IDs, including
services with no connections. Changed identity/configuration, a new service,
extra/missing step, enabled connection, missing proof or wrong backend refuses
completion. User deletion also requires disabled status and purchase blocking.

`begin_remove` validates the persisted step against the actual remote identity
before allowing any attempt. Finalization requires removed steps with verified
absence for every non-PPP backend. PPP needs no remote outcome. Force/abandoned
outcomes remain refused until a separately authorized force controller exists.
UUID clearing after removal is supported; the T1 digest prevents a changed source
UUID from being silently deleted afterwards.

The single-Connection DB finalizer is extracted from the legacy wrapper, which
still performs remote deprovisioning once and commits once. Existing User and
Purchase finalizers preserve accounting/audit records and tombstone wallet
identity as before. Tests inject a crash after deletion and before operation CAS:
all deletes, audit detaches and wallet tombstones roll back. Success retries are
no-ops, sibling services and balances are unchanged, and ledger amounts survive.
Tests run on disposable SQLite and mandatory real MariaDB in CI; no real node
is mutated by tests.

This is not a live deletion orchestrator or Receipt Void button. No endpoint,
worker, force, required-mode switch or server deployment is introduced. Full T1,
host verification, runner actions, verified node probes and approval/financial
integration still precede activating durable deletion or Receipt Void execution.
