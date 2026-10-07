# P5: quota reward destinations

Implementation of Receipt Void section 14.1. Both manifest projection and
actual referral/loyalty grants use `quota_rewards.resolve_quota_reward_target`.

- No Purchase: grant only to a finite User quota.
- Exactly one Purchase: grant only to that finite Purchase quota.
- Multiple Purchases or an unlimited destination: no quota grant; credit
  rewards remain independent.
- Bot creation and package bulk creation defer loyalty until after connection
  provisioning and legacy-pool absorption. Creation evidence records the sale
  allowance before the separate loyalty grant.
- Referral effects record the actual destination ID and before/after allowance
  in the same transaction as the reward. Retries retain existing reward flags.

This intentionally fixes two customer-visible bugs: an unlimited account no
longer becomes limited by a reward, and a reward is no longer stranded on an
unused User allowance when an independent Purchase enforces the service.
Ambiguous multi-service accounts receive credit rewards but no guessed quota
grant, matching the frozen design's default policy.

No tables, modes, loyalty timing cutover, reward threshold/amount changes or
receipt-void execution are introduced. Legacy loyalty is still not correlated
until P9a; old/shadow approvals remain ineligible for void. Self-referral policy
is unchanged. No server deployment or activation occurs in this batch.

Regression coverage extends `test_receipt_approval_referral_rewards.py` on
SQLite and mandatory real CI MariaDB: Purchase effect binding and snapshots,
unlimited and ambiguous destinations, credit independence, loyalty after
absorption, retry, and existing reward/effect rollback checks.
