# WireGuard allocation core — partial L2/T1

`provisioning_wireguard_pool.allocate` is DB-only and currently has **no live
caller**. No remote action, mode activation, schema change or server deployment.

The future T1 caller must validate runtime ownership, node contracts and caller
permissions, acquire canonical leases, call `resource_leases.begin_business`,
and roll back the entire transaction on every error. The helper requires the
same operation attempt's operation and node WG-pool lease, checks the current
wallet generation, and locks the current operation, step and node.

Allocation counts existing connections (including disabled ones) and unfinished
step claims. An abandoned remote peer retains its addresses even after its
operation is compensated; only a verified removed claim becomes reusable.
Adjacent IPv4 addresses, the step snapshot and any subnet widening are flushed
in the same transaction, with no internal commit. Expansion stops at /16. The
64-character schema bound is checked before writing either the node or step.
Retries retain their original addresses and reject a different count. Rollback
undoes both the step claim and subnet expansion; later compensation must never
narrow a committed subnet.

The existing `user_ops._wg_reserve_ips` legacy wrapper is unchanged. This core
does not claim to coordinate unconverted legacy writers; durable activation
still requires converting all relevant writers to the same reservation protocol.
Interface reconciliation, full T1/controller integration, real-node staging and
Receipt Void execution are still pending.

`test_provisioning_wireguard_pool.py` verifies rollback, expansion, staged and
abandoned claims, stale/missing leases, node drift, malformed addresses and
actual two-session allocation with loser retry. It uses a disposable SQLite
database locally and a disposable real MariaDB database in CI. Missing MariaDB
configuration fails in CI. Tests do not contact any VPN node.
