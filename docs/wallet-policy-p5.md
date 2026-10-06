# P5: customer purchase and top-up policy

`services/wallet_policy.py` is the bot router's shared policy boundary.
PanelBridge and RemoteBridge reach the same router checks. This batch
does not change the wallet phase, schema, balances, or server configuration.

In `normal`, existing purchase locks and their admin-supplied explanations
still block top-ups. Purchases and renewals also check other accounts with
the same Telegram ID, closing a second-account bypass. Administrative
panel operations and the existing wallet debit path are unchanged.

In `enforced`, the policy uses the live wallet account: positive outstanding
identity debt blocks purchases, while only `topup_blocked` blocks top-ups.
An absent account or unavailable phase is refused, not guessed. These are
policy checks only: the enforced wallet writer, repayment, cutover, and
Receipt Void execution are **not** implemented by this batch.

Before enabling P9, its transaction must copy legacy purchase locks to
`wallet_accounts.topup_blocked`, and account creation/rebinding must supply
the authoritative tenant-scoped identity. Creation's existing Telegram
lock check does not yet perform a tenant-scoped debt check; completing that
belongs with the account factory. The bot's UX prechecks still display
legacy flags; they must consume the final backend policy before cutover.

Verification: `test_purchase_lock.py` exercises the live router's existing
behavior. `test_wallet_policy.py` exercises normal/fencing/enforced policy,
identity debt, repayment permission, missing account, and absence of writes
on isolated SQLite fixtures. Its debt fixture does not test a complete
void operation or foreign-key enforcement. No real node is contacted.
