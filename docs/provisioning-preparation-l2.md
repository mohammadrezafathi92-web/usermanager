# DB-only non-approval T1

Implementation follow-up to Lifecycle v7.1. The frozen design is unchanged.
This helper has **no live route, scheduler, worker, remote action registration,
ownership claim, mode setter or deployment hook**. Current type modes remain
legacy; current gate remains off. Receipt Void execution remains unavailable.

`provisioning_preparation.prepare` receives a strict `Preparation`, an externally
verified `HostIdentity`, installation UUID and current ownership epoch. The
caller begins a fenced business transaction, rolls back every exception, and
commits once. It must perform authorization, package/service selection and
pricing/source validation before constructing this internal request. This is
not an API for accepting arbitrary client-supplied snapshots or payer IDs.

The helper requires ready schemas, normal wallet generation, durable type,
active matching host/boot/epoch, enforced gate and HA disabled. It checks frozen
user/package identity, validates node/config/contracts, reserves one-time
claims, generates credentials for all nine backends, allocates WireGuard
addresses, and reserves customer/reseller payments in one transaction.
Positive wallet sales require a matching customer wallet hold; a wallet hold
cannot pay for another user or a different sale amount. Reseller cost may
differ from retail price, as supported by the existing reservation contract.

For a new numeric operation ID, canonical leases are acquired **inside this
short DB-only T1 after inserting/flushing the operation**, before any claim,
address or money mutation. This deliberately specializes the design's generic
"acquire before transaction" sequence, because the new operation's numeric
lease key does not exist beforehand. There is no waiting loop, remote I/O or
internal commit. A busy lease or failed hold rolls back even the newly inserted
operation/lease rows. Existing executor transactions still acquire leases
separately, then call `begin_business` and `revalidate`.

Non-renewal T1 exposes no final User/Purchase/Connection. Credentials and
remote identity exist only on staged steps. Same type/business key plus exact
canonical request hash returns the same operation without a second hold or
credential generation. Changed payload is rejected. A replay returns **no
lease tokens**: the executor must acquire current canonical leases before
doing more work. Returned tokens must be heartbeat-renewed while executing;
they are not a replacement for the per-node remote gate.

Renewals have no remote steps: preparation calls T_final inside the same
transaction. No committed intermediate `prepared` renewal is produced.
Reconciliation IDs returned on the operation are consumed only after commit.

T1 stores nonsecret node configuration fingerprints in the intent. T_final
rejects endpoint/config drift and a different node set before delivery. Older
isolated finalization fixtures may omit the map; newly prepared operations
always contain it. A future public executor must only use operations built by
this validated preparation path, not arbitrary legacy fixtures.

`provisioning_contracts` is only a read-side guard, not staging verification.
Adapter version includes adapter helper code and guard policy; configuration
hash excludes authentication secrets. A malformed matcher, unknown response
path, unsupported matcher transport, stale code/config or missing ready row
is refused. PPP has no remote contract. A future probe/verification writer
must supply genuine per-node observations; these tests' ready rows are fixtures,
not evidence of actual devices being verified.

## Verification

`test_provisioning_preparation.py` uses disposable SQLite and mandatory CI
MariaDB databases through `_mariadb_scratch`, with `_no_network` installed.
It covers all five operation types through T1/T_final, full rollback after
holds, no internal commit, stable retry, changed request, busy lease,
ownership/mode refusal, endpoint drift, every backend's staged credential,
and stale/malformed contract refusal. Remote outcomes are typed test fixtures;
no real node mutation occurs.

Remaining P6 work includes approval/effect atomic integration, verified
ownership/mode control, node probes, real runner DTO actions, recovery worker,
closed call-graph verification and activation prerequisites. This batch does
not declare P6 ready or enable Receipt Void.
