"""Every path through the auto-approval decision, and the reason it gives.

Run:  python3 backend/tests/test_auto_approve.py

The reason strings matter as much as the booleans here: they are what an
admin now sees on the receipt notification, and "it does not work" was
unanswerable precisely because they existed only in a log file.
"""
from __future__ import annotations

import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from app import models
from app.services import auto_approve

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def settings(**kw):
    row = models.BotSettings(id=1)
    row.auto_approve_enabled = kw.get("enabled", True)
    row.auto_approve_ignore_hours = kw.get("ignore_hours", False)
    row.auto_approve_from_hour = kw.get("from_hour", 9)
    row.auto_approve_to_hour = kw.get("to_hour", 23)
    row.auto_approve_max_amount = kw.get("cap", 0)
    row.auto_approve_returning_only = kw.get("returning_only", False)
    return row


def at(hour: int) -> dt.datetime:
    return dt.datetime(2026, 1, 1, hour, 0)


# --------------------------------------------------------------- window
print("--- the time window ---")
# get_display_offset is read from the DB; pin it so these assert on the
# window logic rather than on whatever timezone this machine has.
auto_approve.get_display_offset = lambda: 0

check("inside a normal window", auto_approve._in_window(settings(), at(10)), True)
check("before it", auto_approve._in_window(settings(), at(8)), False)
check("the end hour is exclusive", auto_approve._in_window(settings(), at(23)), False)
check("the start hour is inclusive", auto_approve._in_window(settings(), at(9)), True)

# Wrapping past midnight, e.g. 22:00 -> 06:00.
wrap = settings(from_hour=22, to_hour=6)
check("wrapped window, late evening", auto_approve._in_window(wrap, at(23)), True)
check("wrapped window, small hours", auto_approve._in_window(wrap, at(3)), True)
check("wrapped window, afternoon", auto_approve._in_window(wrap, at(15)), False)

# The new explicit flag - and the old implicit trick still works, because
# installs already rely on it.
always = settings(ignore_hours=True, from_hour=9, to_hour=23)
check("ignore_hours at 3am", auto_approve._in_window(always, at(3)), True)
check("ignore_hours at noon", auto_approve._in_window(always, at(12)), True)
check("equal hours still mean 'any time'",
      auto_approve._in_window(settings(from_hour=0, to_hour=0), at(4)), True)
check("ignore_hours wins over an impossible window",
      auto_approve._in_window(settings(ignore_hours=True, from_hour=5, to_hour=5), at(4)), True)

# A row from before the column existed must not crash the check.
class Old:
    auto_approve_from_hour = 9
    auto_approve_to_hour = 23
check("a settings row without the new column still works",
      auto_approve._in_window(Old(), at(10)), True)

# ---------------------------------------------------------------- amount
print("\n--- the amount ---")
check("discounted price wins over list price",
      auto_approve._amount_of({"price": 100000, "final_price": 60000}), 60000)
check("falls back to list price", auto_approve._amount_of({"price": 100000}), 100000)
check("a free request is zero", auto_approve._amount_of({}), 0)
check("a null final_price falls back",
      auto_approve._amount_of({"price": 5000, "final_price": None}), 5000)
check("garbage is treated as zero, not as an error",
      auto_approve._amount_of({"price": "abc"}), 0)

# ---------------------------------------------------------------- decide
print("\n--- the whole decision, and the reason it gives ---")


def decide_with(row, pending, returning=False):
    """Runs decide() against an in-memory settings row."""
    class FakeDb:
        def get(self, model, pk):
            return row

        def close(self):
            pass

    real_session, real_returning = auto_approve.SessionLocal, auto_approve._is_returning
    auto_approve.SessionLocal = lambda: FakeDb()
    auto_approve._is_returning = lambda db, p: returning
    try:
        return auto_approve.decide(pending)
    finally:
        auto_approve.SessionLocal = real_session
        auto_approve._is_returning = real_returning


buy = {"id": 1, "kind": "new", "price": 50000, "telegram_id": 7}

ok, reason = decide_with(settings(ignore_hours=True), buy)
check("a plain purchase with no conditions is approved", ok, True)

ok, reason = decide_with(settings(enabled=False), buy)
check("switched off", (ok, reason), (False, "تایید خودکار خاموش است"))

ok, reason = decide_with(settings(from_hour=1, to_hour=2), buy)
check("outside the window", (ok, reason), (False, "خارج از ساعات تایید خودکار"))

ok, reason = decide_with(settings(ignore_hours=True, cap=10000), buy)
check("over the cap is refused", ok, False)
check("...and the reason names both numbers",
      "50,000" in reason and "10,000" in reason, True)

ok, reason = decide_with(settings(ignore_hours=True, cap=50000), buy)
check("exactly at the cap is allowed", ok, True)

ok, reason = decide_with(settings(ignore_hours=True, returning_only=True), buy, returning=False)
check("a first-time buyer is refused when returning-only is on",
      (ok, reason), (False, "مشتری خرید تاییدشده‌ی قبلی ندارد"))

ok, reason = decide_with(settings(ignore_hours=True, returning_only=True), buy, returning=True)
check("a returning buyer passes", ok, True)

for kind in ("topup", "link"):
    ok, reason = decide_with(settings(ignore_hours=True), {**buy, "kind": kind})
    check(f"a '{kind}' request is never auto-approved", ok, False)

# A missing settings row must fail closed rather than explode.
ok, reason = decide_with(None, buy)
check("no settings row at all fails closed", ok, False)

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
