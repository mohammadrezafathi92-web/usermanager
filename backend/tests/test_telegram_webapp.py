"""The Mini App's authentication boundary (services/telegram_webapp.py).

Run:  python3 backend/tests/test_telegram_webapp.py

One URL serves every reseller on this install: they share a panel and a
hostname, and what differs is which bot the customer opened. Telegram signs
the initData blob with THAT bot's token, so a valid signature says both "this
really came from Telegram" and "it came through this particular bot" - which
is what names the reseller whose shop to show. Everything the mini app is
later allowed to see or sell rests on that, so this file is where the effort
goes.

What must hold, and what each one prevents:
  * a blob signed by reseller A's bot resolves to A and never to B - or one
    reseller sells from another's inventory and charges their credit;
  * a forged or edited blob is refused - or anyone with the URL is every
    customer at once;
  * an old blob is refused - or one captured from a shared screen works
    forever;
  * the comparison is constant-time - or the hash is discoverable a byte at
    a time.
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

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models
from app.services import telegram_webapp as twa

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


SHARED = "8000000001:AAshared"
ALI = "8000000002:AAali"
REZA = "8000000003:AAreza"


def make_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(models.BotSettings(id=1, bot_token=SHARED))
    db.add(models.AdminUser(id=2, username="ali", hashed_password="x", own_bot_token=ALI))
    db.add(models.AdminUser(id=3, username="reza", hashed_password="x", own_bot_token=REZA))
    db.add(models.AdminUser(id=4, username="nobot", hashed_password="x"))
    db.commit()
    return db


def sign(bot_token, *, telegram_id=555, age_seconds=10, **extra):
    """Builds initData exactly the way Telegram does.

    calendar.timegm, not .timestamp(): the latter reads a NAIVE datetime as
    local time, so on a machine at +03:30 this helper was quietly dating
    every blob three and a half hours off and the age assertions below
    disagreed with themselves. The service under test is consistent either
    way (utcnow and utcfromtimestamp are both naive UTC) - this was the test
    lying, not the code.
    """
    fields = {
        "user": json.dumps({"id": telegram_id, "first_name": "C", "username": "c"}),
        "auth_date": str(calendar.timegm(
            (dt.datetime.utcnow() - dt.timedelta(seconds=age_seconds)).utctimetuple())),
        "query_id": "AAA",
    }
    fields.update(extra)
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def verify(db, init_data):
    try:
        return twa.verify(db, init_data)
    except twa.WebAppAuthError as exc:
        return f"REFUSED: {exc}"


db = make_db()

print("--- the signature says WHOSE shop this is ---")
r = verify(db, sign(ALI))
check("a blob from Ali's bot resolves to Ali", r.get("owner_admin_id"), 2)
r = verify(db, sign(REZA))
check("...and one from Reza's bot to Reza", r.get("owner_admin_id"), 3)
r = verify(db, sign(SHARED))
check("...and the shared bot to the panel owner (None)", r.get("owner_admin_id"), None)
check("the customer's telegram id comes through", verify(db, sign(ALI, telegram_id=909))["telegram_id"], 909)

print("\n--- ...and nothing else is accepted ---")
check("a bot this panel does not run", verify(db, sign("9999:AAstranger")).startswith("REFUSED"), True)
check("empty initData", verify(db, "").startswith("REFUSED"), True)
check("no hash at all", verify(db, "user=%7B%22id%22%3A1%7D&auth_date=1").startswith("REFUSED"), True)
check("a hash of the right shape but wrong value",
      verify(db, sign(ALI).replace(sign(ALI)[-8:], "deadbeef")).startswith("REFUSED"), True)

# The whole point of signing: edit any field and the signature stops matching.
tampered = sign(ALI, telegram_id=555).replace("555", "556")
check("changing the user id after signing", verify(db, tampered).startswith("REFUSED"), True)

print("\n--- a reseller cannot be impersonated by editing the blob ---")
# The only thing naming the reseller is the key the signature verifies
# against. There is no reseller id in the data to change.
ali_data = sign(ALI)
check("Ali's blob never resolves to Reza", verify(db, ali_data)["owner_admin_id"] == 3, False)
check("an admin with no bot of their own is never a candidate",
      any(owner == 4 for owner, _ in twa._candidate_tokens(db)), False)

print("\n--- freshness ---")
check("a blob from a minute ago is fine", isinstance(verify(db, sign(ALI, age_seconds=60)), dict), True)
check("one from 23 hours ago is fine",
      isinstance(verify(db, sign(ALI, age_seconds=23 * 3600)), dict), True)
check("one from 25 hours ago is not",
      verify(db, sign(ALI, age_seconds=25 * 3600)).startswith("REFUSED"), True)
check("a timestamp in the future is refused - that is a replay buying time",
      verify(db, sign(ALI, age_seconds=-3600)).startswith("REFUSED"), True)
check("small clock drift is tolerated",
      isinstance(verify(db, sign(ALI, age_seconds=-60)), dict), True)
check("an unreadable auth_date", verify(db, sign(ALI, auth_date="soon")).startswith("REFUSED"), True)

print("\n--- the checks happen in the order that matters ---")
src = inspect.getsource(twa.verify)
check("the signature is verified before auth_date is believed",
      src.index("_signature_matches") < src.index("auth_date"), True)
check("the comparison is constant-time",
      "compare_digest" in inspect.getsource(twa._signature_matches), True)
check("the key is HMAC(\"WebAppData\", token), not the other way round",
      'hmac.new(b"WebAppData", bot_token.encode()' in inspect.getsource(twa._secret_key), True)
check("`hash` itself is excluded from what is signed",
      'key != "hash"' in inspect.getsource(twa._signature_matches), True)
check("...and the fields are sorted, as Telegram specifies",
      "sorted(pairs)" in inspect.getsource(twa._signature_matches), True)

print("\n--- a panel with no bots configured accepts nothing ---")
empty = sessionmaker(bind=create_engine("sqlite://", connect_args={"check_same_thread": False}))()
models.Base.metadata.create_all(empty.get_bind())
check("no candidates", twa._candidate_tokens(empty), [])
check("...so even a well-formed blob is refused",
      verify(empty, sign(ALI)).startswith("REFUSED"), True)

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("هویت مینی‌اپ فقط از روی امضای بات تشخیص داده می‌شود")
