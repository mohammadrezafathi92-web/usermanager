# Node-save contract verification

Creating a node or changing its management endpoint, backend selection or
management credentials saves the settings, then performs the existing bounded
contract probe. This is not a ping: the enforced runner uses a fresh namespaced
temporary identity, verifies creation/read/deletion and records a contract only
after all checks succeed. Existing runner ownership, mode and node locks remain
in effect. No new contract-versioned file is modified by this integration.

The node's owner (or superadmin) may save and explicitly retry via
`POST /api/nodes/{id}/verify-contract`. Sellers and admins merely granted use of
a node cannot trigger this operation. Listing/fetching nodes computes status
from stored state; it never probes. Name, public client endpoint and enable-only
edits do not probe. Enabling a node does not claim it is contract-ready.

The save response remains a `NodeOut`, with optional `contract_status`:
`state`, `ready`, `backend`, `reason`, `cleanup`, `checked_at`, `retry_allowed`.
Settings being saved and contract verification succeeding are separate results.
Raw exceptions, credential values and runner response bodies are never used as
public reason strings. Backend-specific finite reason codes identify known
failures; all other failures use fixed generic text in the UI.

An actual remote probe can create a temporary resource. If cleanup cannot be
proved, or the parent dies after dispatch, subsequent saves and either retry
endpoint will not create another resource. Status stays `cleanup_unknown` and
requires operator investigation/manual cleanup; there is deliberately no blind
"force ready" or "acknowledge and retry" shortcut. The existing password-checked
superadmin control endpoint can return the strictly validated temporary identity
for manual investigation; regular node responses do not expose it. This release
does not automate cleanup recovery when the resource's identity was lost.

Credential edits invalidate previously ready contracts even though the public
endpoint fingerprint intentionally excludes credentials. A private fingerprint
and attempt token fence stale probe completion. A separate per-node save lock
serializes mutating HTTP saves and both verification entry points across local
workers; it is distinct from the runner child node lock. The final node row is
locked and re-read before contract recording. Changes during verification are
never certified as the earlier tested configuration.

Off/shadow gate modes, unavailable provisioning schema and unsupported backends
are shown as not verified; no inline fallback or ownership claim is attempted.
This feature does not probe existing nodes merely by being deployed.

Tests use private SQLite and newly claimed MariaDB databases and fake probe
transports. No production node, customer service or real receipt is modified
for the integration tests. Actual node probe behavior remains covered by the
existing guarded probe and Linux runner suites.
