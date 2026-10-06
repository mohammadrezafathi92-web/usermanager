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
