# Private ownership verification and CAS cores

Follow-up implementation of Lifecycle 10.4. No live route, scheduler, startup
claim, installer change, heartbeat takeover or gate/type activation is added.
L0's inert singleton/type validation remains unchanged. Receipt Void is not
enabled by this batch.

`provisioning_lock_verification.verify` requires valid host identity and an
already-held exclusive installation gate-mode lock. It refuses non-Linux and
denylisted network/FUSE mounts. This filesystem check is the design's denylist,
not proof against every possible unsafe filesystem. It actually tests two
flock descriptors, a child that holds its own lock, SIGKILL plus confirmed
death, and reacquisition afterwards. On MariaDB it tests GET_LOCK exclusion
and release after real DBAPI disconnect (not pooled SQLAlchemy close()).
Only private random verification lock names/files are used; no nodes are
contacted and no business row is written. The proof is internal trusted
process data, not an authentication credential or an unforgeable signature.

`provisioning_ownership.claim` requires a fresh proof for the matching
installation/backend, a currently held exclusive mode lock, superadmin ID,
explicit expected row version and an outer writer transaction. The future
authenticated controller must verify the superadmin's password, read host
identity externally and retain that file lock through commit/rollback.
It also requires `{lock_dir}/installation.id` to match the runtime/proof UUID;
missing, malformed, symlinked or group/world-writable files are refused.
The read-only `provisioning_installation.require_match` uses a fresh database
connection and never repairs either identity. The private `initialize_off`
can publish a missing file only with mode EX, the exact known schema, gate
off, vacant ownership, all types legacy and no nonterminal operation. It
fsyncs a private temporary file and atomically links it without replacing
an existing file. A retry verifies the published file and fsyncs its
directory; error cleanup only removes this attempt's temporary file. Restore
recovery, rotate/fork-reset and startup wiring are not implemented. Existing off-mode live writers
are unchanged; this guard currently protects only the private ownership and
schema-upgrade cores.

Vacant/released ownership increments its epoch; re-verification by exactly
the same host AND boot retains its epoch/initial claim time. A different host,
different boot or draining owner is refused, however old its heartbeat.
State CAS and append-only ownership audit are in the same transaction with
no internal commit. Two real competing sessions accept exactly one version.

`reclaim` is only for the same host after a different boot, with the design's
explicit confirmation and nonempty reason. It increments the ownership epoch
and invalidates the old host/boot/epoch snapshot. The future controller also
needs password confirmation; this core cannot prove the previous machine is
off, distinguish reboot from a clone, or stop a request already in flight.
The existing audit `reason` field stores compact JSON with the human reason
and old/new boot IDs, without adding columns. If the complete evidence exceeds
500 characters it is refused instead of truncating the operator's reason.

Tests run SQLite CAS/rollback/reboot and two-session races locally. macOS uses
an explicit unit proof fixture for CAS and verifies real self-test refusal;
Linux real flock/death checks and real MariaDB advisory-lock checks are
mandatory in CI. Disposable DBs use `_mariadb_scratch`; Python sockets are
guarded with `_no_network`. This is not yet the complete ownership control
plane: handoff/drain/release, verified mode changes, live runner integration,
installer identity mounting and authenticated routes still need completion
before startup validation can accept non-inert runtime state.

Private drain/cancel cores also preserve the ownership epoch and record their
audit atomically. Release requires no nonterminal operations, held exclusive
mode lock and held exclusive locks for the exact current node set. Enforced
release is refused with the original constraint, and supported only after the
explicit, data-preserving v2 constraint upgrade described above. No automatic
gate downgrade or startup constraint upgrade is performed.

The private `provisioning_dispatch_binding.revalidate` core checks a fresh
committed operation/step/version and DB-clock lease after actual mode-SH and
node-EX locks have been acquired. A parent paused before spawning a child
cannot dispatch its old request after compensation, lease expiry or takeover.
Forward calls also require the original wallet epoch, a live forward deadline,
unchanged customer binding and unchanged node endpoint fingerprint. Disabled
nodes can still be cleaned up with a fresh compensation binding. A draining
owner can finish already committed work, but cannot admit new work through T1.
This query does not select management or customer secrets and closes its DB
connection before returning. It is not a transport authorization token and is
not yet wired to a live runner: action schemas, current node contracts, the
read-only child connection and adapter authorization remain separate requirements.

`provisioning_child_database.ChildDatabase` now supplies that private read-only
connection: existing SQLite files are opened with `mode=ro` (no file creation,
no `immutable` shortcut), query-only plus an authorizer; MariaDB connections
use session-default READ ONLY and verify the setting. See the official
[SQLite URI](https://www.sqlite.org/uri.html) and
[MariaDB transaction](https://mariadb.com/docs/server/reference/sql-statements/administrative-sql-statements/set-commands/set-transaction)
contracts. A closed exact SQL allowlist additionally refuses DML, DDL, settings
changes, arbitrary SELECTs, locking reads and multi-statements before execution.
It allows the fixed ownership/installation reads, current WireGuard subnet,
committed dispatch snapshot and the three advisory-lock statements only.
Connections use NullPool: close really disconnects and releases their MariaDB
locks instead of returning them to a pool. The facade exposes no ORM, commit
or raw cursor API. This is protection from coding mistakes, not a sandbox for
malicious Python accessing private internals. The runner still has no real
action registered or live database caller; no mode or server activation occurs.

The private `ChildGuard` combines this facade with actual held file gates and,
on MariaDB, a dedicated GET_LOCK connection. Its check ends each read snapshot
before returning to remote code. Losing the dedicated connection permanently
breaks that hold: no automatic reconnect, GET_LOCK retry, or resuming an old
action. The future transport guard must call it before every writer, including
nested adapter calls. This hold itself performs no remote call and grants no
transport token.

ChildGuard additionally reads the current `(node_id, backend)` contract via
one fixed, non-secret query. It requires ready state, the current adapter code
version, the current node endpoint fingerprint and valid typed not-exist
matchers using exactly the same rules as parent T1. It pins the contract's
version, server/config/code fingerprints and canonical payload for its hold:
changing any of them mid-action breaks the hold rather than applying a new
matcher silently. Returned contract data is detached, not mutable guard state.
The shared rule module is included in the adapter code fingerprint, so this
code update conservatively requires fresh attestation of any older contract.
No contract is marked ready automatically, and no probe runs on real nodes.
