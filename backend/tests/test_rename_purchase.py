"""services/user_ops.rename_purchase and the bot's own «✏️ تغییر نام» flow
(routers/bot.py's rename_purchase endpoint) - a customer replacing the
auto "اکانت N" fallback (see test_auto_service_label.py) with their own
text, and clearing it back to that fallback.

Run:  python3 backend/tests/test_rename_purchase.py

Requested 2026-09-23 alongside the auto-label itself landing in the bot's
own "👤 اکانت من"/"📊 مصرف سرویس‌ها" screens: a customer could now tell two
services apart, but only an admin could give one an actual name from the
panel. This is that same models.Purchase.comment field, editable by the
customer who owns the purchase - and only that customer's own purchases,
never someone else's by guessing an id (the ownership check this test
pins down)."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


from fastapi import HTTPException  # noqa: E402

from app.database import Base, engine, SessionLocal  # noqa: E402
from app import models, schemas  # noqa: E402
from app.routers import bot as bot_router  # noqa: E402
from app.services import hierarchy, user_ops  # noqa: E402

Base.metadata.create_all(bind=engine)
db = SessionLocal()

admin = models.AdminUser(username="admin1", hashed_password="x", is_superadmin=True, role="superadmin")
db.add(admin)
db.commit()
db.refresh(admin)
hierarchy.rebuild_path(db, admin)
db.commit()

node = models.Node(name="node1", type=models.NodeType.mikrotik)
db.add(node)
db.commit()
db.refresh(node)

user1 = user_ops.create_user_record(db, "cust1", telegram_id=111)
user2 = user_ops.create_user_record(db, "cust2", telegram_id=222)
db.commit()

db.add(models.Connection(user_id=user1.id, node_id=node.id, type=models.ConnectionType.wireguard, enabled=True))
db.commit()
purchase = user_ops.absorb_legacy_pool_into_purchase(db, user1)
db.commit()

print("--- rename_purchase sets the customer's own text ---")
check("started auto-labeled", purchase.comment, "اکانت 1")
renamed = user_ops.rename_purchase(db, purchase, "گوشی شخصی")
check("comment updated", renamed.comment, "گوشی شخصی")
check("same row, not a new purchase", renamed.id, purchase.id)

print("\n--- blank text clears back to the auto fallback, not to nothing ---")
cleared = user_ops.rename_purchase(db, purchase, "   ")
check("falls back to an auto label again", cleared.comment, "اکانت 2")
# ^ "اکانت 2" not "اکانت 1": _auto_service_label counts this user's
# EXISTING Purchase rows (there's still only the one) + 1 - see its own
# docstring. Renaming back to blank re-numbers rather than restoring the
# original ordinal, which is fine: the point is a non-empty, still-unique-
# enough label, not preserving history.

print("\n--- long input is truncated, not rejected ---")
long_renamed = user_ops.rename_purchase(db, purchase, "ز" * 400)
check("truncated to 255 chars", len(long_renamed.comment), 255)

print("\n--- the bot endpoint only lets a customer rename THEIR OWN purchase ---")
try:
    bot_router.rename_purchase(
        "cust2", purchase.id, schemas.BotRenamePurchaseRequest(comment="سرقتی"), db=db,
    )
    check("cust2 renaming cust1's purchase was rejected", "no exception raised", "HTTPException")
except HTTPException as exc:
    check("cust2 renaming cust1's purchase was rejected", exc.status_code, 404)
db.refresh(purchase)
check("cust1's purchase is untouched", purchase.comment, long_renamed.comment)

print("\n--- the bot endpoint renames the right purchase for its real owner ---")
out = bot_router.rename_purchase(
    "cust1", purchase.id, schemas.BotRenamePurchaseRequest(comment="اکانت اصلی"), db=db,
)
check("BotPurchaseInfo reflects the new comment", out.comment, "اکانت اصلی")
db.refresh(purchase)
check("...and the row itself was updated", purchase.comment, "اکانت اصلی")

print("\n--- a made-up purchase id 404s instead of 500ing ---")
try:
    bot_router.rename_purchase("cust1", 999999, schemas.BotRenamePurchaseRequest(comment="x"), db=db)
    check("unknown purchase id was rejected", "no exception raised", "HTTPException")
except HTTPException as exc:
    check("unknown purchase id was rejected", exc.status_code, 404)

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
