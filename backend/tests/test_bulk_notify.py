"""Broadcasting to many customers: same answers, far fewer sessions.

Run:  python3 backend/tests/test_bulk_notify.py

bulk_notify_users used to send inside its loop, one fresh Telegram session
per recipient. It now collects the recipients first and hands them to
runner.send_many_sync, which reuses one session and paces itself under
Telegram's rate limit.

The counts it reports are what an operator acts on, so they matter more
than the speed: "12,000 sent, 300 failed" has to still mean that. These
tests pin the counting - including the case the rewrite most easily breaks,
where one Telegram account is linked to several customer rows.
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models
from app.services import user_ops
from app.telegram_bot import runner

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def build(n=10, *, shared_telegram=False, no_telegram=0):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    admin = models.AdminUser(username="a", hashed_password="x", role="admin", tree_path="/1/")
    db.add(admin)
    db.commit()
    db.refresh(admin)
    ids = []
    for i in range(n):
        tg = None if i < no_telegram else (700_000 if shared_telegram else 700_000 + i)
        u = models.User(username=f"u{i}", telegram_id=tg, owner_admin_id=admin.id,
                        status=models.UserStatus.active)
        db.add(u)
        db.flush()
        ids.append(u.id)
    db.commit()
    return db, admin, ids


class Recorder:
    """Stands in for Telegram. Records what it was asked to do."""

    def __init__(self, fail_keys=(), per_send=0.0, error="کاربر ربات را بلاک کرده"):
        self.calls = 0
        self.batches = 0
        self.fail_keys = set(fail_keys)
        self.per_send = per_send
        self.error = error

    def send_many(self, recipients, text, timeout=10.0, token=None,
                  parse_mode=None, **kw):
        self.batches += 1
        self.text = text
        self.parse_mode = parse_mode
        out = {}
        for key, chat in recipients:
            self.calls += 1
            if self.per_send:
                time.sleep(self.per_send)
            out[key] = (False, self.error) if key in self.fail_keys else (True, None)
        return out


def run(db, admin, ids, rec):
    original = runner.send_many_sync
    runner.send_many_sync = rec.send_many
    try:
        return user_ops.bulk_notify_users(db, user_ids=ids, message="سلام", admin=admin)
    finally:
        runner.send_many_sync = original


print("--- the counts an operator reads ---")
db, admin, ids = build(10)
rec = Recorder()
out = run(db, admin, ids, rec)
check("everyone was sent to", out["sent_count"], 10)
check("nothing failed", out["failed_count"], 0)
check("nobody was skipped", out["skipped_no_telegram_count"], 0)
check("the total is right", out["total_count"], 10)
check("no error reported", out["error"], None)

print("\n--- and it is ONE batch, not one session per person ---")
check("a single call to the sender", rec.batches, 1)
check("...covering all 10 recipients", rec.calls, 10)

print("\n--- customers with no Telegram account ---")
db, admin, ids = build(10, no_telegram=4)
rec = Recorder()
out = run(db, admin, ids, rec)
check("counted as skipped, not failed", out["skipped_no_telegram_count"], 4)
check("the rest were sent", out["sent_count"], 6)
check("still counted in the total", out["total_count"], 10)
check("the sender was not asked about them", rec.calls, 6)

print("\n--- some recipients blocked the bot ---")
db, admin, ids = build(10)
rec = Recorder(fail_keys=ids[:3])
out = run(db, admin, ids, rec)
check("failures counted", out["failed_count"], 3)
check("successes counted", out["sent_count"], 7)
check("the reason is handed back", out["error"], "کاربر ربات را بلاک کرده")

print("\n--- the trap: one Telegram account, several customer rows ---")
# Keying results by chat id instead of by user id would collapse these five
# into one and report "1 sent" for five real sends.
db, admin, ids = build(5, shared_telegram=True)
rec = Recorder()
out = run(db, admin, ids, rec)
check("all five rows counted separately", out["sent_count"], 5)
check("...and all five really were sent", rec.calls, 5)

print("\n--- free-form text must not be sent as HTML ---")
db, admin, ids = build(3)
rec = Recorder()
run(db, admin, ids, rec)
check("parse_mode is explicitly off", rec.parse_mode, None)
check("the admin's text is passed through untouched", rec.text, "سلام")

print("\n--- an admin may only message their own tree ---")
db, admin, ids = build(6)
other = models.AdminUser(username="other", hashed_password="x", role="admin", tree_path="/9/")
db.add(other)
db.commit()
db.refresh(other)
db.query(models.User).filter(models.User.id.in_(ids[:3])).update(
    {models.User.owner_admin_id: other.id}, synchronize_session=False)
db.commit()
rec = Recorder()
out = run(db, admin, ids, rec)
check("the other admin's customers are not reached", rec.calls, 3)
check("...and are not in the total either", out["total_count"], 3)

print("\n--- the whole point: one session, not one per recipient ---")
# 200 recipients at a stubbed 2ms each. The old shape paid this serially
# per message on top of a full TLS handshake; this shows the loop itself
# no longer adds anything on top.
db, admin, ids = build(200)
rec = Recorder(per_send=0.002)
t = time.perf_counter()
out = run(db, admin, ids, rec)
el = time.perf_counter() - t
check("all 200 sent", out["sent_count"], 200)
print(f"      200 recipients handled in {el:.2f}s, in {rec.batches} batch")

print("\n--- nothing to send ---")
db, admin, ids = build(3, no_telegram=3)
rec = Recorder()
out = run(db, admin, ids, rec)
check("the sender is not called at all", rec.calls, 0)
check("...and it reports honestly", (out["sent_count"], out["skipped_no_telegram_count"]), (0, 3))

print("\n--- send_many_sync itself: the new concurrency + pacing code ---")
import asyncio


class FakeSession:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class FakeBot:
    """Records concurrency and departure times, and can fail on demand."""

    def __init__(self, fail_chats=(), delay=0.01):
        self.session = FakeSession()
        self.sent = []
        self.times = []
        self.in_flight = 0
        self.max_in_flight = 0
        self.fail_chats = set(fail_chats)
        self.delay = delay

    async def send_message(self, chat_id, text, **kw):
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        self.times.append(asyncio.get_event_loop().time())
        try:
            await asyncio.sleep(self.delay)
            if chat_id in self.fail_chats:
                raise RuntimeError("Forbidden: bot was blocked by the user")
            self.sent.append(chat_id)
        finally:
            self.in_flight -= 1


def with_fake_bot(bot, fn):
    orig_make, orig_lookup = runner._make_bot, runner._lookup_bot_token
    runner._make_bot = lambda token: bot
    runner._lookup_bot_token = lambda: "123:FAKE"
    try:
        return fn()
    finally:
        runner._make_bot, runner._lookup_bot_token = orig_make, orig_lookup


recips = [(i, 800_000 + i) for i in range(40)]

bot = FakeBot()
res = with_fake_bot(bot, lambda: runner.send_many_sync(
    recips, "hi", rate_per_second=0, concurrency=10))
check("every recipient got the message", len(bot.sent), 40)
check("every recipient has a result", len(res), 40)
check("all reported successful", all(ok for ok, _ in res.values()), True)
check("the session was closed", bot.session.closed, True)
check("concurrency stayed within the limit", bot.max_in_flight <= 10, True)
check("...and was actually used (not serial)", bot.max_in_flight > 1, True)

print()
bot = FakeBot(fail_chats={800_000 + 3, 800_000 + 7})
res = with_fake_bot(bot, lambda: runner.send_many_sync(
    recips, "hi", rate_per_second=0, concurrency=10))
check("a blocked recipient does not stop the batch", len(bot.sent), 38)
check("...the two failures are reported", sum(1 for ok, _ in res.values() if not ok), 2)
check("...with Telegram's own words",
      res[3][1], "Forbidden: bot was blocked by the user")
check("...and everyone else still succeeded",
      sum(1 for ok, _ in res.values() if ok), 38)

print()
# Pacing: 20 messages at 20/sec must take about a second, not none.
bot = FakeBot(delay=0.001)
t = time.perf_counter()
with_fake_bot(bot, lambda: runner.send_many_sync(
    [(i, 900_000 + i) for i in range(20)], "hi",
    rate_per_second=20, concurrency=10))
el = time.perf_counter() - t
check("the rate limit is actually applied", 0.8 <= el <= 1.8, True)
print(f"      20 messages at 20/sec took {el:.2f}s (Telegram allows ~30/sec)")

print()
# With no bot token configured at all, every recipient must come back with
# a reason - not an exception out of the endpoint.
_orig_lookup = runner._lookup_bot_token
runner._lookup_bot_token = lambda: None
try:
    check("no token configured is reported per recipient, not raised",
          runner.send_many_sync([(1, 5), (2, 6)], "hi"),
          {1: (False, "توکن ربات تنظیم نشده است"), 2: (False, "توکن ربات تنظیم نشده است")})
    check("an empty list is a no-op", runner.send_many_sync([], "hi"), {})
finally:
    runner._lookup_bot_token = _orig_lookup

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
