"""Sending a message to one customer reports what actually happened.

Run:  python3 backend/tests/test_notify_result.py

Reported from the live panel: "not delivered - the user has probably
blocked the bot", on every single send. Two bugs stacked - the panel read a
field the API never returns, so success looked like failure; and the
failure text then guessed a cause that cannot hold when EVERY send fails.
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models, schemas
from app.services import user_ops
from app.telegram_bot import runner as telegram_bot_runner

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def setup(*, telegram_id=555):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    u = models.User(username="c1", telegram_id=telegram_id)
    db.add(u)
    db.commit()
    db.refresh(u)
    return db, u


# bulk_notify_users now hands the whole recipient list to
# runner.send_many_sync (one Telegram session for the batch instead of one
# per person - see test_bulk_notify.py). These stubs stand in for it while
# keeping each case's intent exactly as it was: a fixed answer for
# everyone, or a sequence of answers in recipient order.
def _always(result):
    return lambda recipients, text, **kw: {key: result for key, _ in recipients}


def _in_order(results):
    def _send(recipients, text, **kw):
        seq = iter(results)
        return {key: next(seq) for key, _ in recipients}
    return _send


print("--- a successful send ---")
db, u = setup()
telegram_bot_runner.send_many_sync = _always((True, None))
res = user_ops.bulk_notify_users(db, [u.id], "سلام")
check("counted as sent", res["sent_count"], 1)
check("nothing failed", res["failed_count"], 0)
check("no error is reported", res["error"], None)

print("\n--- a real failure carries its reason ---")
db, u = setup()
telegram_bot_runner.send_many_sync = _always((False, "Unauthorized"))
res = user_ops.bulk_notify_users(db, [u.id], "سلام")
check("counted as failed", res["failed_count"], 1)
check("the reason survives to the caller", res["error"], "Unauthorized")
check("...instead of a guess about blocking", "بلاک" in (res["error"] or ""), False)

print("\n--- a customer with no linked Telegram ---")
db, u = setup(telegram_id=None)
telegram_bot_runner.send_many_sync = _always((True, None))
res = user_ops.bulk_notify_users(db, [u.id], "سلام")
check("is skipped, not failed", (res["skipped_no_telegram_count"], res["failed_count"]), (1, 0))
check("and no error is invented", res["error"], None)

print("\n--- the first reason is the one reported ---")
db = sessionmaker(bind=create_engine("sqlite://", connect_args={"check_same_thread": False}))()
models.Base.metadata.create_all(db.get_bind())
ids = []
for i in range(3):
    x = models.User(username=f"c{i}", telegram_id=100 + i)
    db.add(x)
    db.commit()
    db.refresh(x)
    ids.append(x.id)
telegram_bot_runner.send_many_sync = _in_order([
    (False, "Forbidden: bot was blocked by the user"),
    (False, "Unauthorized"),
    (False, "timeout"),
])
res = user_ops.bulk_notify_users(db, ids, "سلام")
check("all three failed", res["failed_count"], 3)
check("the first reason is kept", res["error"], "Forbidden: bot was blocked by the user")

print("\n--- the schema carries it ---")
out = schemas.BulkNotifyUsersResult(
    sent_count=0, skipped_no_telegram_count=0, failed_count=1, total_count=1, error="Unauthorized",
)
check("error is part of the response", out.error, "Unauthorized")
check("and is optional for old callers",
      schemas.BulkNotifyUsersResult(
          sent_count=1, skipped_no_telegram_count=0, failed_count=0, total_count=1).error,
      None)

print("\n--- the panel reads COUNTS, not a boolean that never existed ---")
here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
page = open(os.path.join(here, "..", "frontend", "src", "pages", "UserDetail.jsx")).read()
check("success is decided from sent_count", "sent_count" in page, True)
# The original bug, guarded so it cannot come back: reading res.data.sent
# straight off the response.
check("no longer reads a bare .sent off the response",
      bool(re.search(r"res\.data\?\.sent\b(?!_)", page)), False)
check("a customer with no Telegram is told so, not called a failure",
      "messageNoTelegram" in page, True)

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
