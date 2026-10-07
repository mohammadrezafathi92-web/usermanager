# Atomic compensation core — partial Lifecycle L2

`provisioning_compensation.finish` implements the DB-only non-approval T_comp:
locking wallet runtime/epoch before operation, verifying all steps removed with
verified absence, returning each uncaptured reservation, releasing one-time
package claims, and CAS-closing the operation as compensated in one transaction.
It does not commit or contact nodes. The future executor must hold canonical
leases, begin a short writer transaction and roll back **any** exception.

Captured money, incomplete steps, stale versions, unknown failure codes and
approval-bearing operations are refused. Force-abandoned steps remain refused
until the authenticated force path is implemented; they never release money
automatically. A terminal retry does not refund twice. Claim release uses the
existing release-sequence schema, not deletion of audit history.

Tests inject a crash at the final operation write and prove the previous money
refund and claim release roll back too; retry refunds once and emits no refund
ledger. Additional tests cover an attempted-but-not-verified remote deletion,
stale operation versions and unsupported approval integration. Real disposable
MariaDB is mandatory in CI. No live caller, worker, mode or server activation is
introduced; this is not a claim that the full P6 or Receipt Void is finished.
