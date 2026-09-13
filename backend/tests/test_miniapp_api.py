"""The Mini App's API is scoped by the signature and nothing else.

Run:  python3 backend/tests/test_miniapp_api.py

The dangerous mistake in a multi-reseller shop served from ONE url is letting
anything in the request decide whose shop it is. There is no reseller id in
the path, the query or the body here - only the initData blob, whose
signature can only have been produced by one particular bot's token (see
services/telegram_webapp.py). These tests hold that door shut and check the
page is actually built from that reseller's own data.
"""
from __future__ import annotations

import calendar
import datetime as dt
import hashlib
import hmac
import inspect
import json
import os
import sys
from urllib.parse import urlencode

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models
from app.routers import miniapp

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


ALI_TOKEN = "8000000002:AAali"
REZA_TOKEN = "8000000003:AAreza"


def make_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(models.AdminUser(id=2, username="ali", hashed_password="x", own_bot_token=ALI_TOKEN))
    db.add(models.AdminUser(id=3, username="reza", hashed_password="x", own_bot_token=REZA_TOKEN))
    # A package each, owned by each reseller.
    db.add(models.Package(id=10, name="پلن علی", quota_gb=20, duration_days=30,
                          price=100000, enabled=True, bot_enabled=True, owner_admin_id=2))
    db.add(models.Package(id=11, name="پلن رضا", quota_gb=50, duration_days=30,
                          price=200000, enabled=True, bot_enabled=True, owner_admin_id=3))
    db.commit()
    return db


def sign(bot_token, telegram_id=555):
    fields = {
        "user": json.dumps({"id": telegram_id, "first_name": "Cust"}),
        "auth_date": str(calendar.timegm(dt.datetime.utcnow().utctimetuple())),
    }
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def visitor(db, init_data):
    try:
        return miniapp.current_visitor(x_telegram_init_data=init_data, db=db)
    except HTTPException as exc:
        return f"{exc.status_code}: {exc.detail}"


db = make_db()

print("--- the door ---")
v = visitor(db, sign(ALI_TOKEN))
check("a blob from Ali's bot identifies Ali's shop", v.get("owner_admin_id"), 2)
check("...and the customer", v.get("telegram_id"), 555)
check("no initData at all is refused", str(visitor(db, "")).startswith("401"), True)
check("a forged blob is refused", str(visitor(db, "user=x&hash=deadbeef")).startswith("401"), True)
check("a blob from a bot this panel does not run is refused",
      str(visitor(db, sign("9999:AAstranger"))).startswith("401"), True)

print("\n--- the refusal does not coach the forger ---")
detail = str(visitor(db, "user=x&hash=deadbeef"))
for leak in ("hash", "signature", "auth_date", "token"):
    check(f"says nothing about {leak}", leak in detail, False)
check("...it just says where to open it from", "تلگرام" in detail, True)

print("\n--- the page is built from THAT reseller's data ---")
ali = miniapp.home(visitor=visitor(db, sign(ALI_TOKEN)), db=db)
names = [p.name for p in ali["shop"]["packages"]]
check("Ali sees his own package", "پلن علی" in names, True)
check("...and not Reza's", "پلن رضا" in names, False)
check("the shop is named after the reseller", ali["shop"]["title"], "ali")

reza = miniapp.home(visitor=visitor(db, sign(REZA_TOKEN)), db=db)
check("Reza sees his own", [p.name for p in reza["shop"]["packages"]], ["پلن رضا"])
check("...under his own name", reza["shop"]["title"], "reza")

print("\n--- nothing in the request can choose the shop ---")
src = inspect.getsource(miniapp)
check("no owner_admin_id is read from the query or body",
      "owner_admin_id: " in src.split("def current_visitor")[0], False)
check("every scope comes from the verified visitor",
      src.count('visitor["owner_admin_id"]') >= 1, True)
check("the only header consulted is the signed blob",
      src.count("Header(") , 1)

print("\n--- it reuses the bot's own answers rather than a second copy ---")
# A price or a hidden package that is right in the bot must be right here,
# because it is the same function answering.
home_src = inspect.getsource(miniapp.home)
for fn in ("list_packages", "list_users_by_telegram", "get_payment_info"):
    check(f"{fn} is the bot router's", f"bot_router.{fn}" in home_src, True)

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("مینی‌اپ فقط از روی امضا می‌فهمد فروشگاه کیست")
