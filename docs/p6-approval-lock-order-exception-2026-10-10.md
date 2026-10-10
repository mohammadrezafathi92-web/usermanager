# P6 receipt approval lock-order exception (owner-approved, 2026-10-10)

Status: implementation contract only. This does not enable durable approval
operations, change a runtime mode, or authorize a production rollout.

The frozen Receipt Void design, section 6.5, calls the approval-row lock the
first statement of each approval transaction. Its section 9.2 independently
requires the wallet runtime locking read before any wallet-writer update. The
frozen Lifecycle/P6 design, sections 7 and 7.6, also requires runtime reads
before resource-lease revalidation and target-row locks. These requirements
cannot all be literal for one P6 transaction that both owns an approval and
writes the wallet. The owner approved this **limited ordering exception**:

1. Take the wallet runtime current locking read, then receipt-approval runtime
   and the operation-type mode locking reads. Reject blocked/incompatible
   modes before mutation.
2. Lock and validate the exact receipt approval row (version, state, manifest,
   execution-key binding and principal). This is the first approval-specific
   row lock, but not the first SQL statement.
3. Revalidate the already-acquired canonical resource leases in their fixed
   order. Initial lease acquisition may occur before this short business
   transaction as specified by Lifecycle 7.1; it is not permission to mutate
   a target before the in-transaction revalidation.
4. Lock target rows in the fixed (type, ID) order. Only then stage/reserve or
   finalize business effects, and commit approval state with those effects.

On MariaDB, runtime reads must be current locking reads (not repeatable-read
snapshot reads); the approval row uses `FOR UPDATE`. On SQLite, begin the
writer transaction with `BEGIN IMMEDIATE` before the reads. Every error
rolls back the whole transaction. Remote I/O remains outside it.

This exception applies only to P6 receipt-approved T1, T_final and T_comp,
and to recovery transactions that write the same resources. Existing
approval-only registration/takeover/retry behavior is unchanged unless its
implementation is shown to create an inverse lock-order cycle; that must be
tested before approval integration can be enabled. It does not waive version,
key-instance, manifest, effect, ownership, lease or mode checks, and it does
not change the frozen documents' other contracts.

Acceptance before enabling: concurrent takeover versus T1 must have one
winner; wallet cutover must wait for in-flight writers and fence later ones;
approval effect and operation creation/finalization must be atomic; retry and
revocation must fail closed; SQLite and real MariaDB must run these races in
CI. Until then P6 approval helpers remain unavailable and readiness remains
false.
