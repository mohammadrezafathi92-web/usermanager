# P5 DB-only deletion finalizers

`user_ops.finalize_user_deletion_after_deprovision` and
`finalize_purchase_deletion_after_deprovision` never contact a node, commit,
or roll back. Their caller must prove remote absence and own the transaction.
They are internal helpers, not new public deletion/financial endpoints.

The existing user-deletion wrapper checks the wallet before node calls,
deprovisions each connection once, then invokes the user finalizer and
commits. Failure before the commit leaves database deletion/tombstones
rollback-able; it cannot undo a remote removal. Durable recovery is P6.

User deletion keeps wallet lineage and history, detaches loyalty events and
discount audit references, and clears nullable referral links. Open wallet
holds refuse deletion before remote work, and are rechecked by the finalizer.
Purchase deletion removes only that purchase's connections/sessions while
retaining usage, limit-event and ledger history. The verified reseller-refund
settlement uses this helper inside its credit/refund transaction.

Both helpers remain normal-generation only. No wallet cutover, financial
reversal, loyalty reconciliation, or general Receipt Void button is enabled.
The old partial-failure purchase-delete endpoint is deliberately not routed
through this all-connections finalizer; its legacy behavior is unchanged.
