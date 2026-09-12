"""A reseller cannot give a customer quota or time for free.

Run:  python3 backend/tests/test_no_free_renewal.py

Reported 2026-09-12: "وقتی یه یوزر یه یوزر میگیره می‌تونه اینجوری تمدید بزنه
بدون هیچ گونه هزینه اضافی" - the «تمدید این خرید» dialog asked for raw
gigabytes and days, with no package anywhere in it, and charged nothing.

routers/users.py's create_user has required a package from a non-superadmin
since the credit system existed, for exactly this reason ("ساخت کاربر بدون
پکیج مجاز نیست"). Renewing is the same sale to the same customer and had no
such rule, so there were three free doors, all reachable from the panel:

  1. renew a purchase with add_gb/add_days and no package - charged only at
     the account's per-GB rate, which is 0 for a normal flat-priced
     reseller, and add_days was never priced at any rate;
  2. the same through the bulk action, once per selected customer;
  3. the edit-user form, by typing a bigger number into «حجم» or a later
     date into «تاریخ انقضا» - no package, no charge, no renewal at all as
     far as the credit system could see.

And "مصرف قبلی صفر شود" on its own hands the whole quota back, which is
selling it again - nothing looked at that checkbox on the cost side either.

What must keep working, and is easy to break while closing the above: a
superadmin is never charged and keeps the raw fields; a reseller can still
REDUCE a customer's quota or bring their expiry forward; and the edit form
resubmits quota and expiry untouched on every single save, so refusing
merely because those fields were present would stop a reseller renaming a
customer.
"""
from __future__ import annotations

import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from fastapi import HTTPException

from app import models
from app.routers.users import _QUOTA_ROUNDING_SLACK_BYTES, _refuse_free_grant
from app.services import admin_billing

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


GB = 1024 ** 3

superadmin = models.AdminUser(id=1, username="root", hashed_password="x", is_superadmin=True)
reseller = models.AdminUser(id=2, username="reseller", hashed_password="x", is_superadmin=False)
package = models.Package(id=9, name="۲۰ گیگ ۱ ماه", quota_gb=20, duration_days=30, price=100000)


def grant(admin, pkg):
    try:
        admin_billing.require_package_to_grant(admin, pkg)
        return "ok"
    except HTTPException as exc:
        return exc.status_code


print("--- renewing needs a package, unless you are the panel owner ---")
check("a reseller with no package is refused", grant(reseller, None), 400)
check("a reseller with a package goes through", grant(reseller, package), "ok")
check("the superadmin may still renew by hand", grant(superadmin, None), "ok")
try:
    admin_billing.require_package_to_grant(reseller, None)
except HTTPException as exc:
    check("the refusal tells them what to do", "پکیج" in exc.detail, True)


def refuse(admin, data, user):
    """None = allowed."""
    if admin.is_superadmin:
        return None
    try:
        _refuse_free_grant(data, user)
        return None
    except HTTPException as exc:
        return exc.status_code


def a_user(**kw):
    defaults = dict(id=1, username="c1", total_quota_bytes=20 * GB,
                    expire_at=dt.datetime(2026, 10, 1, 12, 0), expire_days_after_first_use=None)
    defaults.update(kw)
    return models.User(**defaults)


print("\n--- the edit form cannot be used as a free renewal ---")
u = a_user()
check("more quota is refused", refuse(reseller, {"total_quota_bytes": 50 * GB}, u), 400)
check("switching to UNLIMITED is refused - it is the largest grant of all",
      refuse(reseller, {"total_quota_bytes": 0}, u), 400)
check("a later expiry is refused",
      refuse(reseller, {"expire_at": dt.datetime(2027, 1, 1)}, u), 400)
check("clearing the expiry (never expires) is refused",
      refuse(reseller, {"expire_at": None}, u), 400)
check("a fresh count-from-first-use window is refused",
      refuse(reseller, {"expire_days_after_first_use": 60}, u), 400)

print("\n--- ...while taking things away is still a reseller's own business ---")
check("less quota is fine", refuse(reseller, {"total_quota_bytes": 5 * GB}, u), None)
check("an earlier expiry is fine",
      refuse(reseller, {"expire_at": dt.datetime(2026, 9, 20)}, u), None)
check("a fixed date replacing 'unlimited quota' is a reduction",
      refuse(reseller, {"total_quota_bytes": 5 * GB}, a_user(total_quota_bytes=0)), None)
check("setting an expiry on a never-expiring account is a reduction",
      refuse(reseller, {"expire_at": dt.datetime(2030, 1, 1)}, a_user(expire_at=None)), None)
check("fields that have nothing to do with quota or time are untouched",
      refuse(reseller, {"full_name": "x", "notes": "y", "status": "disabled"}, u), None)

print("\n--- saving the form UNCHANGED must not be mistaken for a grant ---")
# utils.js shows gigabytes to two decimals and converts back on save, so an
# untouched field can come back a few megabytes off. If that were refused, a
# reseller could not save a customer's name.
odd = a_user(total_quota_bytes=int(17.234 * GB))
round_tripped = int(round(17.23 * GB))  # what bytesToGb -> gbToBytes produces
check("a two-decimal round-trip is allowed",
      refuse(reseller, {"total_quota_bytes": round_tripped}, odd), None)
check("...and so is the same trip upward",
      refuse(reseller, {"total_quota_bytes": int(round(17.24 * GB))}, odd), None)
check("but the slack is megabytes, not a renewal",
      refuse(reseller, {"total_quota_bytes": int(17.234 * GB) + _QUOTA_ROUNDING_SLACK_BYTES + 1}, odd), 400)
check("the slack is small enough to be invisible", _QUOTA_ROUNDING_SLACK_BYTES < 20 * 1024 * 1024, True)
# The date field is shown as a bare day, so resubmitting it lands at
# midnight - earlier than a stored afternoon time, never later.
check("re-sending the date-only form value is a (harmless) reduction",
      refuse(reseller, {"expire_at": dt.datetime(2026, 10, 1, 0, 0)}, u), None)

print("\n--- the superadmin is exempt from all of it ---")
check("unlimited quota", refuse(superadmin, {"total_quota_bytes": 0}, u), None)
check("no expiry", refuse(superadmin, {"expire_at": None}, u), None)

print("\n--- the routers actually enforce it ---")
import inspect  # noqa: E402
from app.routers import users as users_router  # noqa: E402

check("the per-purchase renewal requires a package",
      "require_package_to_grant" in inspect.getsource(users_router.renew_purchase_endpoint), True)
check("so does the bulk action",
      "require_package_to_grant" in inspect.getsource(users_router.bulk_update_users), True)
check("and the edit form checks for a free grant",
      "_refuse_free_grant" in inspect.getsource(users_router.update_user), True)
check("a bulk action that GRANTS nothing stays free",
      "payload.add_gb or payload.add_days or payload.reset_usage"
      in inspect.getsource(users_router.bulk_update_users), True)

print("\n--- «بازنشانی مصرف» is a sale too, in the panel AND in the bot ---")
# Reported 2026-09-12 ("اره اونم پکیجی کن"): zeroing a used-up quota hands
# the whole thing back, and it was the last free door once the renewal ones
# were closed.
check("the user-level reset requires a package",
      "require_package_to_grant" in inspect.getsource(users_router.reset_usage), True)
check("...and charges it", "charge_for_package" in inspect.getsource(users_router.reset_usage), True)
check("...and books the revenue", "record_panel_sale" in inspect.getsource(users_router.reset_usage), True)
check("the per-purchase reset requires a package",
      "require_package_to_grant" in inspect.getsource(users_router.reset_purchase_usage), True)
check("...and charges it",
      "charge_for_package" in inspect.getsource(users_router.reset_purchase_usage), True)

from app.telegram_bot import keyboards  # noqa: E402
from app.telegram_bot.handlers import admin_users as bot_admin_users  # noqa: E402


def bot_buttons(allow_reset):
    kb = keyboards.admin_user_detail_kb("someone", True, allow_reset=allow_reset)
    return [b.text for row in kb.inline_keyboard for b in row]


check("the bot hides the reset button from a reseller",
      any("ریست مصرف" in b for b in bot_buttons(False)), False)
check("...and still shows it to the panel owner",
      any("ریست مصرف" in b for b in bot_buttons(True)), True)
check("تمدید is still offered to a reseller - that is where they go instead",
      any("تمدید" in b for b in bot_buttons(False)), True)
# Hiding a button does not retract the callbacks already sitting in old
# messages, so the handler has to refuse as well.
check("the bot handler refuses it too, not just the keyboard",
      "_is_panel_owner(acting_scope)" in inspect.getsource(bot_admin_users.cb_user_reset), True)
check("only the superadmin counts as the panel owner",
      bot_admin_users._is_panel_owner({"role": "superadmin"}), True)
for role in ("admin", "seller", "config", None):
    check(f"...not role={role!r}", bot_admin_users._is_panel_owner({"role": role}), False)
check("an empty scope is not the panel owner", bot_admin_users._is_panel_owner({}), False)

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("تمدید و افزایش حجم/زمان برای نماینده‌ها دیگر مجانی نیست")
