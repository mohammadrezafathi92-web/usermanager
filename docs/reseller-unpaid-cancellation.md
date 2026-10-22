# Unpaid reseller-service cancellation

This is a separate, opt-in cancellation path, not full Receipt Void.
The existing delete action remains distinct and never promises a refund.

## Calculation

Consumed fraction is the arithmetic mean of elapsed-time percentage and
used-quota percentage, with each capped at 100%. Example: 20% time + 30%
quota means 25% consumed and 75% refundable. An unlimited dimension is
excluded; a wholly unmetered service is refused. Refunds round down to whole
tomans and use the actual original reseller debit, never today's package price.
Credit returns to that charged reseller, not to the customer wallet or card pool.

## Current supported execution

Disabled by default (`RESELLER_CANCELLATION_ENABLED=false`). The UI can
preview eligible charges without enabling execution. This implementation
requires Linux, SQLite, one host, a shared local lock directory, HA disabled,
and exclusively WireGuard connections for the selected purchase. Do not
enable on a multi-host deployment or a network filesystem. All backend
processes must be restarted together when enabling; old/uninstrumented
processes and external mutation scripts cannot run alongside cancellation.

Only newly recorded, individually charged sales with proven debit/source
rows are eligible. Historical sources are not guessed. Renewed/changed
entitlements, receipt/card/wallet-linked sales, discounts and users with
untracked referral/loyalty rewards are refused. Usage-billed or free purchases
have no refundable reseller debit. Additional transports and full receipt
reversal remain outside this release.

Grant sellers `cancel_unpaid_services` explicitly. Admins retain their normal
tree scope. Execution requires password confirmation and an explicit unpaid
confirmation. Before enabling on a real installation, verify stop/read/delete
against a staging RouterOS node; fake-router tests are not hardware verification.

## Remote steps and the provisioning gate

The two RouterOS writes go through `legacy_remote` like every other legacy
writer: `wg_refund_stop` (disable the exact peer, read its final counters
twice, read the router uptime) and `wg_refund_remove` (re-check the stopped
counters, delete, verify absence and the peer's speed queue). The final
counters are committed in the panel database between the two, so a peer is
never deleted before its usage is durable. With the gate `off`/`shadow` they
run inline; with the gate `enforced` (required on production) they run in
the runner child under the node lock and ownership/endpoint fencing. A write
from the panel process itself is refused there; before this change the
cancellation could not stop or delete anything on an enforced install.
Reads (identity check before any stop) stay in the panel process. An
unknown runner outcome leaves the operation pending; the resume repeats
the stop (idempotent) and accepts only the same counters.

## What "unpaid" excludes

A new cancellation is refused while anything says the customer paid for this
service or other work owns it (`services/reseller_refund_holds.py`):

- a paid renewal queued on the service (or an old customer-level queue);
- a renewal receipt for this service (or the customer's only service) still
  `pending`/`processing` in the bot's queue - an unreadable queue counts as
  a hold, not as "unpaid";
- a receipt approval still registered/mutating/voiding for the customer;
- a receipt-void hold on the service;
- an unfinished durable provisioning operation or a reserved payment hold
  on the customer.

These holds are checked again inside the transaction that creates the
operation (SQLite `BEGIN IMMEDIATE`), so a renewal or operation committed
after the first check cannot slip in; afterwards the pending operation
refuses the customer's mutations, and a renewal racing the remote delete
waits behind the cancellation barrier and then finds the service gone.

The operator resolves that item first (approve or reject the receipt, let
the operation finish); the cancellation never decides between them. Once an
operation exists it is resumed, not re-judged: its connections may already
be stopped or deleted.

The preview shows the exact target service (package, connections, node,
usage, expiry), how many other services stay untouched, and the reseller
credited. `GET /api/users/{id}/unpaid-cancellations` lists each operation
with its state and how many connections were stopped/verified removed, so a
lost response or a reload shows where it stands; the customer page keeps an
unfinished one visible until it is resumed.

SQLite reuses the id of the last deleted row. A settled operation and its
sale basis therefore retire their purchase id (stored as `-id`, the original
in `retired_purchase_id`, plus the service's `history_key`): a later service
under the same id gets its own basis and is never answered by the old result.
The panel sends the previewed service's `history_key` with the request: a
retry of that request returns its own operation's stored result even after
the id was reused, and an operation never resumes against a service whose
key differs (`reseller_service_changed`); the newer service is untouched.
Operation, basis, refund ledger entry and sale void marker stay for audit.

## Recovery and accounting

RouterOS uptime must prove that the node did not reboot since the sale/
connection was created. Unknown uptime or detected reboot refuses automatic
settlement, even when current counters happen to exceed the cached counters.
External/manual peer recreation or counter editing is not supported. Server
clock correctness and staging verification of RouterOS uptime formats are
prerequisites; this is not cryptographic accounting of historical traffic.

Cancellation holds an exclusive host-local barrier across preflight, remote
stop, durable final-counter capture, verified deletion and financial commit.
Ordinary instrumented writes share this barrier. No SQLite writer transaction
is held across remote calls. Timeouts can leave a pending operation with some
peers stopped/deleted, but do not pay until all are verified. Mutations for
that customer are refused while pending; resume the SAME cancellation.
Do not use ordinary delete, manual credit or disable the feature to bypass a
pending operation. Resolve all pending operations before rollback/disable.

Final refund, refund ledger, sale void marker, purchase-count adjustment and
database deletion commit together. Usage/audit history is retained. Retry
returns the original result and cannot credit twice. Accounting aggregates
exclude the voided unpaid sale while transaction history retains it, and
reseller cost reflects original spend minus refund. Other purchases and the
customer's wallet remain unchanged.
