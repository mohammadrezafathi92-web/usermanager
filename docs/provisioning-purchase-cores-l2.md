# Purchase construction / legacy absorption cores — Lifecycle L2

`build_purchase_core` and `absorb_legacy_pool_core` separate the remaining
purchase-row mutations from remote provisioning and transaction ownership.
Legacy absorption preserves quota, usage, expiration, reservations and existing
connection ownership without committing. Purchase construction snapshots package
data and can apply count/loyalty in the caller's transaction.

The live `apply_package_as_purchase` wrapper defers count/loyalty until after its
existing remote provisioning loop, preserving existing lock scope and timing.
Its original rollback-on-all-services-failed and single successful commit remain.
No provisioning mode or payment policy changes. The historical absorption wrapper
never committed and retains that behavior.

Tests assert no hidden transaction or remote dispatch in both cores, rollback of
the purchase/reward/count/legacy reservation together, and exactly one commit and
one benefit application through the legacy wrapper. Real disposable MariaDB is
mandatory in CI through the existing mutation-core test.

This extraction does not claim the full T_final orchestration, frozen optional
manifest application, closed call-graph gate or worker integration is complete.
