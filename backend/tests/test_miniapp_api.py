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
import pathlib
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


def plan_names(home):
    """The shop arrives as cards now (see miniapp._shop_shelves), so the
    plans are one level down. Flattened here because what these tests are
    about is WHOSE plans came back, not how they are arranged - that is
    test_package_groups.py's job."""
    return [p.name for shelf in home["shop"]["groups"] for p in shelf["packages"]]


names = plan_names(ali)
check("Ali sees his own package", "پلن علی" in names, True)
check("...and not Reza's", "پلن رضا" in names, False)
check("the shop is named after the reseller", ali["shop"]["title"], "ali")

reza = miniapp.home(visitor=visitor(db, sign(REZA_TOKEN)), db=db)
check("Reza sees his own", plan_names(reza), ["پلن رضا"])
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
for fn in ("list_users_by_telegram", "get_payment_info"):
    check(f"{fn} is the bot router's", f"bot_router.{fn}" in home_src, True)
# list_packages moved one step away, into _shop_shelves, which applies the
# Mini App's own shelf rules on top - but it is still the BOT's answer that
# is being filtered, never a second query against models.Package.
check("the packages still come from the bot router's own list",
      "bot_router.list_packages" in inspect.getsource(miniapp._shop_shelves), True)

print("\n--- the bot points its own Menu button at the Mini App ---")
# BotFather keeps moving where the Menu button lives, and every reseller on
# this panel has their own bot - asking each of them to find that setting is
# a support burden with no end. The panel already holds the token, so it
# says so itself, on every start (which also repairs a button someone
# changed or a URL that moved).
from app.telegram_bot import runner  # noqa: E402

from app.telegram_bot.panel_bridge import PanelBridge  # noqa: E402
from app.telegram_bot.remote_bridge import RemoteBridge  # noqa: E402

menu_src = inspect.getsource(runner._set_menu_button)
check("it is a web_app button, not a link", "MenuButtonWebApp" in menu_src, True)
check("...pointing at /app", '/app"' in menu_src, True)
check("refuses anything but https - Telegram will not open a Mini App over http",
      'startswith("https://")' in menu_src, True)
check("a failure cannot stop the bot starting", "except Exception" in menu_src, True)
check("...and it runs on every start", "_set_menu_button(bot)" in inspect.getsource(runner._main), True)

# The label is a panel setting, not a code constant and not a BotFather
# errand: it applies to EVERY bot this panel runs, and doing it by hand per
# reseller never ends. Asked for 2026-09-13 ("میخوام همون دکمه منو بشه دکمه
# اوپن اپ").
check("the label comes from settings", "get_miniapp_button_text" in menu_src, True)
check("...with a default when unset", "MINIAPP_BUTTON_TEXT" in menu_src, True)
check("both bridges can answer it",
      hasattr(RemoteBridge, "get_miniapp_button_text") and hasattr(PanelBridge, "get_miniapp_button_text"),
      True)

# Changing a label must not cost a round of bot restarts - each one drops
# the poll for a few seconds, and a panel with a dozen resellers' bots would
# take a dozen small outages for a cosmetic change.
from app.routers import telegram_bot_settings as bot_settings  # noqa: E402

save_src = inspect.getsource(bot_settings.update_settings)
check("saving a new label re-applies it in place", "refresh_menu_buttons" in save_src, True)
check("...only when it actually changed", "label_changed" in save_src, True)
check("...and it is capped to what Telegram accepts", "[:32]" in save_src, True)
# The word "restart" appears in its docstring explaining why it does not,
# so check for the CALLS rather than the word.
refresh_src = inspect.getsource(runner.refresh_menu_buttons)
check("the refresh calls no restart function",
      any(call in refresh_src for call in ("restart_bot(", "restart_admin_bot(", "stop_bot(")), False)
check("...it schedules the set onto each running bot's own loop",
      "run_coroutine_threadsafe" in refresh_src, True)
check("...and skips a bot whose loop is not running",
      "loop.is_running()" in refresh_src, True)

# A bot deployed to a second server talks to the panel over HTTP instead of
# in-process. The two bridges must offer the same surface or that bot dies
# on a method only one of them has.
check("the remote bridge can answer it too",
      hasattr(RemoteBridge, "get_panel_public_url") and hasattr(PanelBridge, "get_panel_public_url"),
      True)

print("\n--- Telegram's script is served by this panel, not by telegram.org ---")
# telegram.org is exactly the host that is blocked on the networks this is
# sold into, and the page was depending on it AT LOAD TIME for both the
# theme and, on some clients, the credential itself.
script_src = inspect.getsource(miniapp.telegram_web_app_script)
check("fetched through the same route the bots use to reach Telegram",
      "_lookup_telegram_api_proxy_url" in script_src, True)
check("...and cached, so it is fetched once rather than per visitor",
      "_script_cache" in script_src, True)
check("a stale copy beats none when Telegram is unreachable",
      script_src.count("cached") >= 2, True)
check("...and even with no copy at all it answers valid JavaScript",
      "/* telegram-web-app.js unavailable */" in script_src, True)
check("never a 5xx - the page copes with its absence, an error only adds noise",
      "status_code=5" in script_src, False)

page = (pathlib.Path(__file__).resolve().parents[2]
        / "frontend" / "src" / "pages" / "MiniApp.jsx").read_text()
check("the page loads it from its own origin",
      'TELEGRAM_SCRIPT = "/api/miniapp/telegram-web-app.js"' in page, True)
check("...and no longer from telegram.org", "https://telegram.org/js" in page, False)

print("\n--- and when it fails, the page can say why ---")
check("it reports what it found", "جزئیات فنی" in page, True)
check("...lengths, never the signature itself",
      "initData.length" in page and "{initData}" not in page, True)

print("\n--- «سرویس‌های من» lists what was BOUGHT, not every connection ---")
# First version listed every Connection, so a customer saw "xray / فعال"
# repeated once per protocol, with no quota, no usage and no expiry - a
# Connection carries none of those. What someone means by "my services" is
# models.Purchase, which is what the bot's «اکانت من» has always shown.
home_src = inspect.getsource(miniapp.home)
check("purchases are what the tab is built from",
      "list_user_purchases" in home_src, True)
check("...with the quota and usage a progress bar needs",
      "quota_bytes" in home_src and "used_bytes" in home_src, True)
check("...and the expiry", "expire_at" in home_src, True)
check("the package name is the snapshot taken at purchase time",
      "package_name_snapshot" in home_src, True)
check("a reserved renewal is surfaced, so nobody pays twice",
      "reserved_quota_bytes" in home_src, True)
check("connections are no longer flattened into the list",
      'getattr(account, "connections"' in home_src, False)

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("مینی‌اپ فقط از روی امضا می‌فهمد فروشگاه کیست")
