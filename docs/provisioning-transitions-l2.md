# Provisioning recovery transitions — partial L2

DB-only step transitions implement persisted remote attempts, typed identity
observations, compensation and atomic clearing of staged credentials. They
neither make remote calls nor commit transactions, start a worker, switch modes,
or enable Receipt Void. Approval-bearing operations are refused until their
atomic approval integration is implemented.

The future executor must hold canonical runtime/resource leases and commit
`remote_calling` before issuing a remote request. SQLite calls require a short
writer transaction. Any error rolls back the entire transaction. A stale step
version is rejected with CAS; recovery after an absent read does not forget a
previous remote attempt. Unreadable reads remain retryable. Identity conflicts
and unverified deletion remain cleanup-required, never automatic success.

Compensation removes never-attempted steps without contacting a node. Attempted
steps need a verified absence result. Password/private-key clearing and the state
change occur in one UPDATE, while the Xray UUID required to delete the remote
identity remains until absence is verified. Activation checks the final
Connection's owner, node, protocol, identity and copied credentials before
clearing its staged copy. Public serialization uses an explicit non-secret
allowlist.

Standalone tests run on disposable SQLite and mandatory disposable MariaDB in
CI. They exercise crash rollback, stale versions, unknown results, identity
conflicts, compensation retries, credential-copy rejection and RADIUS-only
activation. No live writer imports these helpers yet. Terminal operation
finalizers, approval integration, ownership fencing, runner action wiring and
worker recovery remain separate implementation steps, not claimed complete.
