# Lifecycle L2: DB-only mutation cores, first batch

Extracted from existing user operations without activating durable mode:

- `create_user_record_core`: User/account creation, optional legacy loyalty.
- `apply_referral_code_core`: referral link, both credit and quota rewards,
  and evidence; caller owns the transaction.
- `redeem_discount_code_core`: conditional SQL capacity/expiry/enabled/prior-use
  recheck, redemption and evidence. A stale validation cannot spend another slot.
- `renew_user_core`, `renew_purchase_core`: both reserved and immediate-reset
  branches, evidence, and optional reconciliation targets; never call a node.

Existing public service functions retain their signatures and commit boundaries.
Legacy renewal wrappers still perform reconciliation before their commit. The
durable caller must reconcile only after committing its own finalization.
Legacy loyalty counting timing and self-referral policy are unchanged.

The discount endpoint now records its shadow approval effect through the
wrapper's before-commit hook: counter, redemption and effect are atomic. An
injected commit failure rolls all three back, and a retry consumes one slot.

`test_provisioning_mutation_cores.py` runs on local SQLite and mandatory real
MariaDB in CI, using only isolated databases and a no-network guard. It covers
rollback, single-commit legacy wrappers, both renewal branches, discount/effect
crash and retry, stale validation, and two real concurrent sessions competing
for one discount slot. SQLite busy transactions are retried from rollback in
the contention test; this is not a new generic production retry wrapper.

## Remaining integration

This is not the complete L2/P6 rollout. Durable finalization still needs the
Purchase constructor, reseller reserve/capture/release, frozen-manifest reward
inputs and ordered resource locks, plus the complete closed call-graph gate.
Accounting already adds entries without committing; legacy-pool absorption
already flushes without committing. No worker, operation mode, reservation,
remote runner action, financial cutover or receipt-void execution is activated
by this batch. The frozen design documents are unchanged.
