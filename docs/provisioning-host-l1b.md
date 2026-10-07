# Host identity / fresh fencing reads — partial L1b

`provisioning_host` reads the external 0600 host-secret file without creating it,
refuses symlinks/non-regular files, hashes the secret and pairs it with the kernel
boot UUID. Neither the raw secret nor its path is included in errors or returned
identity data. This module can be imported by the future runner without ORM or
application session imports.

Each revalidation uses a fresh connection/transaction and checks installation,
active owner state, exact host/boot identity, ownership epoch and verified lock
backend. Stale heartbeat never changes ownership, and draining rejects a new
writer. No cached ORM object is accepted as authority.

Tests cover secure file handling and read-only SQL, wrong host/boot/installation,
an old heartbeat, an epoch changed by another transaction and drain. Disposable
MariaDB is mandatory in CI. Ownership claim/verification probes, explicit
reclaim/handoff endpoints, installer mounts and runtime guard integration are
not implemented by this module. It is not imported by live writers, changes no
mode and does not imply enforced gate readiness.
