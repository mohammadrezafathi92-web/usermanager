"""A reseller's own customer paid through "the reseller's own bot", but the
payment-receipt notification landed with the main/shared admin instead of
the reseller - reported 2026-09-06.

Run:  python3 backend/tests/test_receipt_notify_owner.py

Root cause: config.approval_targets() only reflects whichever bot
instance's thread actually handled the incoming message (see
telegram_bot/config.py's threading.local docstring) - the shared bot's
static BotSettings.admin_ids unless the customer happened to be talking to
the owning reseller's own dedicated bot AND it was correctly linked at the
time. There was no guarantee the reseller who actually owns the customer
(pending_purchases.owner_admin_id) ever received a copy.

Fix: telegram_bot/handlers/customer.py's _notify_targets() always adds the
pending request's real owner's own linked Telegram id (via the new
PanelBridge.get_admin_telegram_id) on top of whatever config.approval_
targets() resolves to - see that function's docstring. This test exercises
_notify_targets() directly against a real (in-memory) AdminUser row rather
than re-testing config.approval_targets() itself, which test_recovery_login
style tests elsewhere already cover indirectly.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# A real file, not "sqlite://" (in-memory) - get_admin_telegram_id runs its
# query via asyncio.to_thread on a worker thread, and an in-memory sqlite
# db is NOT shared across connections/threads (each gets its own empty db),
# unlike a real file which every connection/thread sees identically - the
# same as production, where BOT_DB_PATH/DATABASE_URL is always a real file.
_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_tmp_db.name}")

from app import models
from app.database import Base, engine
from app.telegram_bot.config import config
from app.telegram_bot.handlers.customer import _notify_targets

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


Base.metadata.create_all(engine)

from app.database import SessionLocal  # noqa: E402  (after create_all, same pattern as other test files)

db = SessionLocal()
seller = models.AdminUser(
    username="seller1", hashed_password="x", is_superadmin=False, telegram_id=555111,
)
unlinked_seller = models.AdminUser(
    username="seller2", hashed_password="x", is_superadmin=False, telegram_id=None,
)
db.add_all([seller, unlinked_seller])
db.commit()
db.refresh(seller)
db.refresh(unlinked_seller)


print("--- shared bot's thread (static admin_ids = the main admin) handles a linked seller's receipt ---")
config.configure("shared-token", {999000}, set(), True, None)  # simulates the shared bot's own thread
targets = asyncio.run(_notify_targets({"owner_admin_id": seller.id}))
check("the shared admin is still notified (unchanged behaviour)", 999000 in targets, True)
check("...AND the actual owning seller is now notified too", 555111 in targets, True)
check("exactly those two, no more", targets, {999000, 555111})

print("\n--- owner has no linked Telegram id: falls back to approval_targets() alone ---")
targets = asyncio.run(_notify_targets({"owner_admin_id": unlinked_seller.id}))
check("only the shared admin - nothing to add", targets, {999000})

print("\n--- ownerless request (shared-panel customer, no reseller involved) ---")
targets = asyncio.run(_notify_targets({"owner_admin_id": None}))
check("just approval_targets(), unaffected by this fix", targets, {999000})

print("\n--- the seller's OWN bot thread (config already scoped correctly) - no duplicate ---")
config.configure("sellers-own-token", {555111}, set(), True, seller.id)
targets = asyncio.run(_notify_targets({"owner_admin_id": seller.id}))
check("still exactly one id - the union doesn't duplicate it", targets, {555111})

print("\n--- unknown/deleted admin id: fails closed to approval_targets() alone ---")
config.configure("shared-token", {999000}, set(), True, None)
targets = asyncio.run(_notify_targets({"owner_admin_id": 999999}))
check("no crash, just the existing targets", targets, {999000})

db.close()

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
