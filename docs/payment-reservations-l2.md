# Lifecycle L2: structured pre-cutover payment reservations

DB-only helpers implement section 8's `legacy_balance` customer and
`admin_balance` reseller generations on the existing reservation table.

- Reserve deducts conditionally once per `(operation_id, payer_kind)`; retry
  with different payer/amount is rejected. Reseller overdraft rules are retained.
- Customer capture binds the actual sale ledger (payer and amount), without
  another deduction. Reseller capture writes its only `admin_credit_spend` row.
- Release is allowed only while compensating and after every recorded step
  is removed. An attempted remote step requires verified absence; unknown or
  abandoned removal is not automatically refunded by these helpers.
- Capture/release use state/version CAS. Retry is a no-op; a captured sale
  cannot be released as if it were an unused hold.
- Release writes no cash-income or refund ledger. The internal wallet cause
  `reservation_release` is not exposed as an add-balance API credit/source.
- Reserved payers cannot be deleted; customer identity rebind is also refused.
  Hold checks use live table availability even if provisioning readiness fails.

No live caller invokes these helpers yet. All operation modes remain legacy,
the node gate remains off, and no worker or cutover is activated. `wallet_lot`
is refused rather than approximated. New tables/columns are not introduced.

The future T1/T_final/T_comp caller must own the complete short transaction:
BEGIN IMMEDIATE on SQLite; canonical runtime/lease/operation/payer locks on
MariaDB; operation state changes flushed before helper entry. Any exception,
including CAS/UNIQUE failure after a balance change, requires rollback of the
whole transaction. Never wrap network I/O in this transaction. Payer eligibility
(trial, superadmin, volume billing) and scoped authorization belong to T1; exempt
reseller rows are refused by the helper as a second guard.

Tests use isolated databases and a no-network guard, with real MariaDB mandatory
in CI: reserve/rollback/retry, ledger binding, release verification, held-payer
deletion/rebind refusal, exact reseller capture accounting, release without a
refund ledger, overdraft and two real simultaneous requests for the same hold.
The single-customer-wallet-writer AST gate now explicitly verifies the two new
reseller-only balance writers cannot bypass `wallet_service` for User.balance.
