# DB-only final Connection records — partial Lifecycle L2

`provisioning_records.build_connection_core` creates the final connection from
persisted staged credentials and immutable step identity, then activates the step
and clears staged credentials in the same caller-owned transaction. No remote
client, commit, random credential regeneration or worker is involved.

Owner and Purchase binding, active node, backend/protocol pairing, current Xray
panel mode, inbound tag and panel inbound ID / WireGuard interface, required credentials and batch length are
validated before creation. The transition layer verifies the copied credentials
before clearing their staged copy. Approval-bearing operations remain refused
until atomic approval integration exists.

Tests cover all nine backends (DB record shape only, not real devices), wrong
owner, disabled node, changed Xray inbound or WireGuard interface, missing credential, overlong batch, rollback preserving the
staged credential, successful retry and stale-version refusal without a duplicate
Connection. Real disposable MariaDB is mandatory in CI.

Caller still owns canonical runtime/resource leases, node-contract/fingerprint
revalidation, business policy, remaining final mutations/capture/effects and the
single T_final commit. This helper has no live caller and does not mark an
operation completed or enable durable provisioning or Receipt Void.

The accompanying deletion transitions require the source Connection to be
disabled and still bound to the exact target user/node/protocol before starting.
Verified removal (or an absent recovery read) closes the step; unreadable reads
remain retryable, and conflicts/unverified deletion require cleanup. Explicit
retry clears the old outcome in the same UPDATE. RADIUS-only deletion needs no
remote outcome. Deletion cannot enter creation compensation; its completion
barrier checks removed steps separately, while the original DB row remains until
the future deletion finalizer transaction. Neither path is wired live yet.
