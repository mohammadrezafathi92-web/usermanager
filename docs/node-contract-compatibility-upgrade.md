# Node contract compatibility recovery — receipt 1140

## Confirmed deployment regression

The global provisioning adapter version hashes eight source files, including
`adapter_mikrotik_wg.py`, for every remote backend. Between deployed main
`42d0ec8` and `c22f1dc`, only that file changed in the hashed inventory. Its
changed definitions are `stop_capture_remove`, `stop_and_measure`, and
`remove_stopped`: cancellation/refund helpers. Existing provisioning probe,
create, read, identity, matcher and verified-absence definitions did not change.

The exact version transition is:

- previous: `742e58935c84e385565c1468b06c6bdd3218e984`;
- deployed: `8342748d18b4c80457d28d1ef038d7389fef3d39`.

Startup invalidated previously verified contracts on unchanged 3X-UI and
SoftEther backends as well as WireGuard. All parent/readiness/runner-child
guards correctly refused those invalidated contracts. Disabling those guards,
changing runtime modes, or blindly rewriting verification versions is not a fix.

## Bounded recovery contract

Recovery is a reviewed compatibility upgrade, **not a new live verification**.
It must leave the eight hashed files and every existing guard unchanged. It
only operates when the actual current version is exactly the deployed version
above, and only on contracts verified under the exact previous version whose
sole recorded invalidation reason is `adapter_version_changed`.

The supported recovery backends are `mikrotik_wg`, `threexui`, and `softether`.
The node's live backend/configuration, stored matcher schema, server
fingerprint, original verifier and timestamps must still be valid. Disabled
nodes and all unknown versions, different reasons, or mismatching facts remain
blocked. A future hashed source change disables this compatibility upgrade.

Lock the node and contract, compare-and-set the observed contract facts,
increment the row version, and record the complete non-secret previous contract
snapshot and old/new versions in a dedicated additive audit table in the same
transaction. Preserve original verification time, verifier, matcher and
fingerprints. Any audit/schema/CAS failure must not grant readiness. Recovery
must be idempotent and must not contact a node or create a test identity.

The audit table must be created and inspected explicitly: provisioning's
original ten-table metadata list is deliberately closed. Do not weaken its
schema verification to introduce the audit.

## Operational constraints

Only the primary server is in deployment scope. The actual receipt must not be
approved, rejected, retried, or used for a test. No financial entries or customer
records are repaired by this upgrade. After deployment, check contract readiness,
audit records, schema, runtime modes, health and the unchanged receipt status
read-only. The seller can retry approval only after the operator reports that
readiness is restored; do not promise that unrelated approval checks cannot
subsequently reject the request.

The production observation linked receipt 1140 to approval 254, which was
`failed`, had no `mutating_at`, and had no linked provisioning operation. This
does not by itself prove every possible financial table unchanged; do not
overstate that observation.

## Prevention: per-backend contract versions (follow-up)

The root cause was the single global adapter digest: any change to any of
the eight hashed files invalidated every node contract. Now:

- `provisioning_contract_rules.adapter_version(backend)` hashes only the shared
  contract code (`adapter_base.py`, `provisioning_contracts.py`,
  `provisioning_contract_rules.py`) plus that backend's own adapter. A
  WireGuard-only change no longer touches 3X-UI, SoftEther or panel contracts;
  a shared-code change still (correctly) moves them all.
- The WireGuard cancellation/refund steps moved to
  `adapter_mikrotik_wg_refund.py`, outside every hashed file, so refund changes
  invalidate no contract.
- `tests/test_provisioning_contract_version_scope.py` pins every backend's
  version. A pull request that changes contract code fails it and names the
  backends whose deployed contracts it would invalidate, before merge.
- Switching the scheme moves every stored version once. The upgrade module now
  carries only that reviewed transition (global c22 `8342748…` -> pinned
  per-backend versions, same three backends, same locks/CAS/audit and
  fail-closed rules). The earlier 42d -> c22 transition is retired.

Remaining, deliberately not automated: when an operator changes a node's
connection settings, its contract is `config_changed` and needs a fresh
verification. Automatically probing a real node on save needs an explicit
owner decision first.
