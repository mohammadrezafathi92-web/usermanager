"""A volume-billed reseller with an empty GB pool cannot sell.

Run:  python3 backend/tests/test_volume_gate.py

Reported 2026-09-12: "نه اصلا بهش حجم ندادم ولی تونست پکیج بسازه" - a
reseller who had been given ZERO gigabytes created a customer with a
package anyway, and that customer's traffic then drove the pool to -5 GB.

There was no check at all. Both charge functions in admin_billing return
immediately for billing_mode "usage", which is correct on the COST side -
such an account is not charged a price at sale time, its pool drains as
traffic flows (quota_manager._apply_delta). But "not charged here" had
quietly become "not checked anywhere": a flat-priced reseller is refused
the moment their balance cannot cover a sale, while the volume-billed one
could sell out of an empty pool forever, which left the GB allowance
completely unenforced.

The rule pinned down here is "you must have volume to sell", NOT "you must
hold the package's full size" - reserving the package size up front would
turn usage billing into pre-paid billing, and how much of a 20GB package a
customer actually uses is unknowable at sale time.

Equally important, and easy to break: traffic already flowing is NOT cut
off. A pool that runs out mid-month keeps going negative, because that is
debt (see AdminUser.volume_balance_gb) - this only stops NEW business.
"""
from __future__ import annotations

import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from fastapi import HTTPException

from app import models
from app.services import admin_billing

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def admin(**kw):
    defaults = dict(id=2, username="r", hashed_password="x", is_superadmin=False,
                    billing_mode="usage", volume_balance_gb=0)
    defaults.update(kw)
    return models.AdminUser(**defaults)


def sell(a):
    """None = allowed, else the Persian reason."""
    try:
        admin_billing.ensure_volume_available(a)
        return None
    except HTTPException as exc:
        return str(exc.detail)


print("--- an empty or negative pool cannot open new business ---")
check("zero volume is refused", sell(admin(volume_balance_gb=0)) is not None, True)
check("None (never given any) is refused", sell(admin(volume_balance_gb=None)) is not None, True)
check("a negative pool is refused", sell(admin(volume_balance_gb=-5)) is not None, True)
check("the debt case says how much is owed", "۵" in (sell(admin(volume_balance_gb=-5)) or "")
      or "5" in (sell(admin(volume_balance_gb=-5)) or ""), True)
check("the zero case explains what a volume account is",
      "حجمی" in (sell(admin(volume_balance_gb=0)) or ""), True)

print("\n--- ...and any volume at all is enough, whatever the package size ---")
check("a fraction of a gigabyte still allows a sale", sell(admin(volume_balance_gb=0.1)), None)
check("plenty of volume", sell(admin(volume_balance_gb=500)), None)

print("\n--- nobody else is affected ---")
check("a flat-priced reseller with no volume is untouched - money is their gate",
      sell(admin(billing_mode="flat", volume_balance_gb=0)), None)
check("a flat-priced reseller with a negative volume field is untouched",
      sell(admin(billing_mode="flat", volume_balance_gb=-99)), None)
check("the superadmin is never gated",
      sell(admin(is_superadmin=True, billing_mode="usage", volume_balance_gb=-99)), None)

print("\n--- every panel selling path asks ---")
from app.routers import users as users_router  # noqa: E402

for fn in ("create_user", "bulk_create_users", "apply_package", "renew_purchase_endpoint",
           "bulk_update_users", "reset_usage", "reset_purchase_usage"):
    check(f"{fn} gates on volume",
          "ensure_volume_available" in inspect.getsource(getattr(users_router, fn)), True)

print("\n--- the bot refuses before walking someone through a flow ---")
from app.routers import bot as bot_router  # noqa: E402
from app.telegram_bot.handlers import admin_users as bot_admin_users  # noqa: E402

check("the answer rides along with 'who is this admin'",
      "sell_block_reason" in inspect.getsource(bot_router.get_admin_by_telegram), True)
for fn in ("cb_admin_create", "cmd_admin_create", "cb_user_renew_ask",
           "cb_service_renew_ask", "cb_user_add_package"):
    check(f"bot {fn} refuses up front",
          "_refuse_if_cannot_sell" in inspect.getsource(getattr(bot_admin_users, fn)), True)

print("\n--- traffic already flowing is still NOT cut off ---")
# The deduction in quota_manager must stay unconditional: a customer
# mid-download is not the reseller's billing problem, and stopping them
# would be a far worse bug than the debt.
from app.services import quota_manager  # noqa: E402

apply_delta = inspect.getsource(quota_manager._apply_delta)
check("usage is still metered with no balance check",
      "ensure_volume_available" in apply_delta, False)
check("...and still decrements the pool",
      'volume_balance_gb", -gb' in apply_delta, True)

print("\n--- the receipt-approval path stays untouched on purpose ---")
# routers/bot.py's create_user is called AFTER a customer has already paid
# (see its own comment). Refusing there would take their money and give
# them nothing, so that path still lets the reseller go into debt.
check("a paid-for customer is never refused for the reseller's empty pool",
      "ensure_volume_available" in inspect.getsource(bot_router.create_user), False)

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("فروش با حجم صفر ممکن نیست، ولی مصرف در جریان قطع نمی‌شود")
