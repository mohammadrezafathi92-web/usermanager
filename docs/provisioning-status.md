# Read-only provisioning operator status

`GET /api/provisioning/status` is superadmin-only and performs SELECTs only.
It reports gate/lock/owner-state metadata, aggregate operation/step/reservation
counts, type modes, stored contract-state counts and missing real runner actions.
It does not return intents, staged credentials, raw errors or host secrets/IDs.
Unavailable schema is reported without querying its tables or leaking inspector
details. No POST/PUT activation, retry or force endpoint is introduced.

The deployment is explicitly `infrastructure_only`: `p6_ready` and
`receipt_void_execution_available` remain false. DB-only cores and successful
schema checks alone do not imply durable orchestration, host verification,
real runner actions or node-contract probes are implemented/verified. These
blockers are reported honestly; this endpoint is not an activation mechanism.

Tests use the real authorization dependency with root, seller and anonymous
principals, assert SELECT-only execution, assert credentials/intent are absent,
and verify no database query occurs when schema readiness is unavailable.
