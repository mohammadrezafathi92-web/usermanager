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
connection and never repairs either identity. Setup/restore/rotate/fork-reset
and startup wiring are not implemented here. Existing off-mode live writers
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
mode lock and held exclusive locks for the exact current node set. **Enforced
release is explicitly refused:** the current frozen CHECK only permits an
enforced gate with active/draining owner, so released ownership would violate
it. No automatic gate downgrade is performed. A controlled constraint upgrade
needs approval before enforced handoff can be completed. No constraint or
existing schema is changed in this batch.
