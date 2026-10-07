# DB-only one-time package claims — partial Lifecycle T1

`provisioning_claims.reserve` claims a one-time package for the existing User
and/or the Telegram identity within the operation tenant. Keys use the frozen
length-prefixed UTF-8 SHA-256 contract, with typed audit columns verified again.
Same-operation retry returns the same active rows; another operation is refused.
The UNIQUE key remains the final concurrent arbiter. All claims precede payment
reservation and remote work; the caller rolls back every exception, including
UNIQUE/deadlock, and retries the complete T1 transaction, never just an insert.

Existing purchases are checked conservatively within the same tenant. This is
not a substitute for the explicit activation backfill: legacy purchase writers
do not participate in this claim protocol, so mixed unmanaged legacy/durable
writers must not be allowed during cutover. No backfill or mode change runs here.

`bind_purchase` associates claims with the exact resulting User/Purchase in
the caller's T_final transaction. For a newly created User, frozen username,
tenant and Telegram identity must match. Wrong target/package or released or
corrupt claims refuse binding. Compensation's existing release protocol retains
old rows with `release_seq=id` and permits a subsequent active claim.

Tests cover canonical keys, invalid identity/scope, whole-transaction rollback,
same-operation retry, compensation and re-claim, purchase binding rollback,
sibling-account reuse, tenant isolation and a real two-session race. SQLite
runs locally; disposable real MariaDB is mandatory in CI. No function commits,
contacts a node, exposes credentials or integrates with a live purchase route.
The operation/controller, approval binding and activation backfill remain
prerequisites before enabling durable one-time package enforcement.
