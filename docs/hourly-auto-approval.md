# One automatic approval per rolling hour (P9b)

This is a risk limit, **not a timer that approves a waiting receipt every
hour**, and not independent verification that a bank transfer happened.
It retains the existing global/per-reseller auto-approval switches, hours,
amount ceiling and returning-customer condition.

## Rule

- A subject is one Telegram identity inside one tenant. Its accounts and
  shared/dedicated/remote bots share one slot; other tenants are independent.
- New purchases and renewals share that slot. Wallet top-ups are never
  automatically approved. Manual approval remains available immediately.
- A second receipt inside 60 minutes remains pending for manual review.
  The backend returns HTTP 429 with remaining seconds and `Retry-After`.
  It is neither rejected nor scheduled for later automatic processing.
- Exactly 60 minutes after the grant, another eligible receipt may receive
  automatic approval. Retrying the same receipt does not consume a new slot.
- Failure, void, cancellation or refund does not release an issued slot.
- The central database clock and durable subject lock determine the window;
  no bot's local history, cache or clock can extend the allowance.

## Operator control

Settings → wallet/receipt runtime shows requested and effective limiter
modes separately: off, monitoring (`shadow`), and enforced.
Saving requires the superadmin's authenticated password confirmation and
a second confirmation. This release **does not change modes on startup**.

Enforcement requires effective receipt registration `required`, ready key
identity and HA disabled (there is no implemented external coordinator).
Enabling it seeds subjects from recent auto grants atomically with the mode
change. Switching back to monitoring stops denials without deleting grants
or audit history. Normal updates preserve the selected mode.

Update remote bots before depending on central-history parity. Old managed
bots still face the backend limit, but may apply their old, stricter local
history check and send an otherwise eligible request for manual review.

## Deployment checks

Use fake receipts only on an owner's explicit test account. Never use a
real customer's receipt to exercise a rejection or void. Automated tests
use disposable databases and no node/Telegram access. Production activation
is separate from installing the code; do not bypass password-authenticated
operator routes or directly change the runtime database.

## Licensing boundary

This release also fixes local signed-license expiry checks during a failed
heartbeat. It retains existing offline/grace/master policies and does not
rotate signing keys, impose new pricing or customer limits, disconnect VPN
services on licence expiry, or broaden licence scopes. Those product
decisions and binary/source distribution remain separate work.
