# Non-approval T_final integration

`provisioning_finalization.finish` combines the existing DB mutation cores for
`create_user`, `purchase`, `add_connection`, `renew_user` and `renew_purchase`.
It currently has no live API/worker caller and does not activate provisioning.

The T1 controller must persist a versioned `FinalIntent` containing typed user,
package, sale and renewal snapshots. Finalization does not reread live package
prices or limits to construct the sale. It checks the current target identity,
tenant, wallet epoch, operation version, canonical lease ownership and complete
step states. It requires the operation lease and, for an existing customer or
existing Telegram identity, that customer's identity lease.

One caller-owned transaction creates User/wallet identity and account, Purchase,
Connection, benefits and sale Ledger rows; binds one-time claims; captures all
payment reservations; clears staged secrets through the existing transitions;
and completes the operation with result IDs. No core commits, rolls back or
contacts a node. The caller must roll back every exception and commit once.
Only reserved payment rows can be finalized. Wallet capture is bound to the
same sale ledger row. An already-completed operation is returned without
creating another resource, ledger row or benefit.

Renewal stays in one prepared-to-completed transaction with no remote call.
Its transient `_reconciliation_after_commit` contains the existing cores'
reconciliation requests; a future controller must consume these only after
commit. Existing quota polling remains the fallback after a crash there.

Approval-bearing operations still return
`provisioning_approval_integration_unavailable`: no bypass is added for them.
Atomic approval state/effects, full T1 preparation, verified host/node contracts,
the durable worker/runner, the closed call-graph gate and mode activation remain
separate pending work. Current loyalty behavior is unchanged; no P9a policy or
financial lot/debt cutover is activated. This is not Receipt Void execution.

The integration test injects failure at the final operation CAS for all five
types, proves rollback of resources, ledger, captures and staged-secret clearing,
then verifies exactly-once completion/replay and one outer commit. It also checks
frozen package projections after live package edits. SQLite uses a temporary
file; real MariaDB uses a newly created scratch database and is mandatory in CI.
No test contacts a VPN node.
