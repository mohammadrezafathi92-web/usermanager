"""The Telegram bot under a 20,000-customer load.

Run:  python3 backend/tests/stress_seed.py /tmp/stress.db
      python3 backend/tests/stress_bot.py /tmp/stress.db

Nothing here touches Telegram. Network sends are replaced with a stub that
costs a fixed, realistic amount of time, so what gets measured is OUR
overhead per message - the part we control - rather than the internet.

Three things are worth knowing about this bot:
  1. it calls the panel in-process (panel_bridge), not over HTTP, so a
     customer tapping a button costs a DB query, not a round trip;
  2. those DB calls go through asyncio.to_thread, so the number of
     threads in the default executor is the real concurrency ceiling;
  3. broadcasts and the daily warning job are plain synchronous loops,
     and that is where the size of the customer base actually bites.
"""
from __future__ import annotations

import asyncio
import logging
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB = sys.argv[1] if len(sys.argv) > 1 else "/tmp/stress.db"
os.environ["DATABASE_URL"] = f"sqlite:///{DB}"
logging.disable(logging.CRITICAL)

from sqlalchemy import event
from sqlalchemy.orm import selectinload

# A WireGuard connection makes get_connection_share open a LIVE MikroTik
# API session (see the report). There are no MikroTik boxes here, so it is
# stubbed - but that stub is itself the finding: with it, /start costs a
# few ms; without it, a network round trip per connection, and a 400 for
# the whole response if the node is down.
import app.services.mikrotik_client as _mt


class _FakeMt:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_public_key(self, iface):
        return "FAKEPUBKEYFAKEPUBKEYFAKEPUBKEYFAKEPUBKEY="


_mt.MikrotikClient.for_node = staticmethod(lambda node: _FakeMt())

from app.database import SessionLocal, engine
from app import models, schemas
from app.routers import bot as bot_router
from app.services import notify

_q = {"n": 0}


@event.listens_for(engine, "before_cursor_execute")
def _count(conn, cursor, statement, params, context, executemany):
    _q["n"] += 1


db = SessionLocal()
n_users = db.query(models.User).count()
n_tg = db.query(models.User).filter(models.User.telegram_id.isnot(None)).count()
sample_tg = db.query(models.User.telegram_id).filter(
    models.User.telegram_id.isnot(None)).limit(500).all()
sample_tg = [r[0] for r in sample_tg]
sample_user = db.query(models.User).filter(models.User.telegram_id.isnot(None)).first()
pkg = db.query(models.Package).filter_by(enabled=True).first()

print(f"database: {n_users:,} customers, {n_tg:,} of them with a Telegram account linked\n")


def bench(label, fn, runs=20, budget_ms=None):
    times = []
    queries = 0
    for i in range(runs):
        _q["n"] = 0
        t = time.perf_counter()
        try:
            fn(i)
        except Exception as exc:  # noqa: BLE001
            print(f"  {label:<46} ERROR: {type(exc).__name__}: {exc}")
            return None
        times.append((time.perf_counter() - t) * 1000)
        queries = _q["n"]
        db.expunge_all()
    med = statistics.median(times)
    p95 = sorted(times)[min(int(len(times) * 0.95), len(times) - 1)]
    flag = ""
    if budget_ms:
        flag = "  ✓" if med <= budget_ms else f"  ← SLOW"
    print(f"  {label:<46} {med:6.1f} ms   p95 {p95:6.1f}   {queries:>3} queries{flag}")
    return med


print("=" * 96)
print("WHAT ONE CUSTOMER TAPPING A BUTTON COSTS")
print("=" * 96)

bench("/start - find me by telegram id",
      lambda i: bot_router.get_user_by_telegram(sample_tg[i % len(sample_tg)], db=db), budget_ms=100)
bench("account picker - all my accounts",
      lambda i: bot_router.list_users_by_telegram(sample_tg[i % len(sample_tg)], db=db), budget_ms=100)
bench("'خرید' - list packages",
      lambda i: bot_router.list_packages(db=db), budget_ms=100)
bench("'اکانت من' - my services",
      lambda i: bot_router.list_user_purchases(sample_user.username, db=db), budget_ms=100)
bench("payment info (card details)",
      lambda i: bot_router.get_payment_info(db=db), budget_ms=100)
bench("node list",
      lambda i: bot_router.list_nodes(db=db), budget_ms=100)
bench("tutorials",
      lambda i: bot_router.list_tutorials(db=db), budget_ms=100)
bench("menu config (called on EVERY button)",
      lambda i: bot_router.get_customer_menu_config(db=db), budget_ms=50)

print()
print("  a typical purchase flow is ~6 of these back to back:")
print("  → roughly what a customer waits for between taps, excluding Telegram itself\n")

print("=" * 96)
print("CONCURRENCY - how many customers the bot can serve at once")
print("=" * 96)
# panel_bridge runs every DB call through asyncio.to_thread, whose default
# executor size is min(32, cpu_count + 4). That number, not the event loop,
# is the ceiling.
import concurrent.futures
default_workers = min(32, (os.cpu_count() or 1) + 4)
print(f"  this machine: {os.cpu_count()} cores → default thread pool = {default_workers} workers")
print(f"  a 2-core VPS:  2 cores → default thread pool = 6 workers\n")


async def simulate_rush(n_customers: int):
    """n customers all tap a button at the same moment."""
    from app.telegram_bot import panel_bridge

    async def one(i):
        t = time.perf_counter()
        await panel_bridge._call(bot_router.get_user_by_telegram,
                                 sample_tg[i % len(sample_tg)])
        return (time.perf_counter() - t) * 1000

    t0 = time.perf_counter()
    results = await asyncio.gather(*[one(i) for i in range(n_customers)])
    return results, time.perf_counter() - t0


for n in (10, 50, 200, 500):
    lat, wall = asyncio.run(simulate_rush(n))
    lat = sorted(lat)
    print(f"  {n:>3} customers at once   all served in {wall:5.2f} s"
          f"   median wait {statistics.median(lat):6.1f} ms"
          f"   worst {lat[-1]:7.1f} ms")

print()
print("=" * 96)
print("BROADCAST - 'ارسال پیام تلگرام' to everyone")
print("=" * 96)

# Replace the network with a stub costing a realistic per-message time, so
# what is measured is our own per-message overhead and loop structure.
# 120ms is a fair figure for a Telegram sendMessage over a fresh TLS
# session from Iran; the point is the SHAPE, not the constant.
PER_SEND_MS = 120
import app.telegram_bot.runner as runner

sends = {"n": 0}


def _fake_send(chat_id, text, timeout=10.0, token=None, parse_mode=None):
    sends["n"] += 1
    time.sleep(PER_SEND_MS / 1000)
    return True, None


runner.send_message_sync_detailed = _fake_send

from app.services import user_ops

# Must be an admin who can actually SEE these customers - a superadmin
# cannot (hierarchy.owned_admin_ids walls each Admin's roster off), and
# using one silently makes the loop skip everybody and report zero.
from app.services import hierarchy as _h
su = max(db.query(models.AdminUser).filter(models.AdminUser.role == "admin").all(),
         key=lambda a: db.query(models.User).filter(
             models.User.owner_admin_id.in_(_h.owned_admin_ids(db, a))).count())
_owned = _h.owned_admin_ids(db, su)
ids = [r[0] for r in db.query(models.User.id)
       .filter(models.User.telegram_id.isnot(None))
       .filter(models.User.owner_admin_id.in_(_owned)).limit(50).all()]

sends["n"] = 0
_q["n"] = 0
t = time.perf_counter()
user_ops.bulk_notify_users(db, user_ids=ids, message="تست", admin=su)
el = time.perf_counter() - t
per_msg = el / len(ids)
assert sends["n"] == len(ids), f"stub not reached: {sends['n']} of {len(ids)} - the numbers below would be meaningless"
print(f"  50 recipients                     {el:5.2f} s   ({per_msg * 1000:.0f} ms each, {_q['n']} queries)")
print(f"  → the loop is SEQUENTIAL: one message finishes before the next starts\n")

for n in (1_000, 5_000, 13_000, 20_000):
    projected = n * per_msg
    m, s = divmod(int(projected), 60)
    h, m = divmod(m, 60)
    stamp = f"{h}h {m:02d}m" if h else f"{m}m {s:02d}s"
    warn = ""
    if projected > 660:
        warn = "  ← longer than nginx's 660s timeout: the panel shows an error"
    print(f"  {n:>6,} recipients   ≈ {stamp:>10}{warn}")

print()
print("  Telegram's own limit is ~30 messages/sec for a bot.")
print(f"  This loop sends {1 / per_msg:.1f}/sec - so the bot is {30 * per_msg:.0f}x slower than")
print("  Telegram would allow. The ceiling here is our own code, not Telegram.")

print()
print("=" * 96)
print("THE DAILY WARNING JOB (quota / expiry reminders, 10:00 every day)")
print("=" * 96)

sent = {"n": 0}


def _fake_send_simple(chat_id, text, timeout=10.0, token=None, parse_mode=None):
    sent["n"] += 1
    return True   # no sleep: isolating the DB cost from the network cost


runner.send_message_sync = _fake_send_simple
notify.telegram_bot_runner = runner if hasattr(notify, "telegram_bot_runner") else None

_q["n"] = 0
t = time.perf_counter()
users = (
    db.query(models.User)
    .filter(models.User.telegram_id.isnot(None))
    .filter(models.User.status != models.UserStatus.disabled)
    .all()
)
# Exactly what the job's loop touches, per user.
for u in users:
    _ = [p.status for p in u.purchases]
    _ = any(c.purchase_id is None for c in u.connections)
lazy_time = time.perf_counter() - t
lazy_q = _q["n"]
db.expunge_all()

_q["n"] = 0
t = time.perf_counter()
users = (
    db.query(models.User)
    .options(selectinload(models.User.purchases), selectinload(models.User.connections))
    .filter(models.User.telegram_id.isnot(None))
    .filter(models.User.status != models.UserStatus.disabled)
    .all()
)
for u in users:
    _ = [p.status for p in u.purchases]
    _ = any(c.purchase_id is None for c in u.connections)
eager_time = time.perf_counter() - t
eager_q = _q["n"]
db.expunge_all()

print(f"  as written (lazy)                 {lazy_time:6.2f} s   {lazy_q:>6,} queries")
print(f"  with the collections preloaded    {eager_time:6.2f} s   {eager_q:>6,} queries")
if lazy_q > eager_q * 10:
    print(f"  → {lazy_q - eager_q:,} avoidable queries, once a day, same N+1 shape as poll_all had")

print()
print("=" * 96)
print("SUMMARY")
print("=" * 96)
print("  Reading is cheap: every button a customer taps is a few ms of DB work.")
print("  Sending is not: the broadcast loop is sequential and rebuilds a TLS")
print("  session per message, so it scales linearly with the customer base and")
print("  runs inside an HTTP request that will time out long before it finishes.")
