"""Checkout in the Mini App: money must never be able to go missing.

Run:  python3 backend/tests/test_miniapp_checkout.py

Until this was built the Mini App had no button in it anywhere except the
three tabs at the bottom - reported, accurately, as «نمیشه روش کلیک کرد».
What it grew is deliberately not a new shop: both paths call the very same
functions the bot calls, so a sale made from a web page and a sale made in
chat produce the same Purchase, the same ledger row, and the same entry in
«درخواست‌های در انتظار».

The three things worth holding still, each of which has a way of going
quietly wrong:

  1. The wallet is debited BEFORE anything is provisioned, and a failure
     after the debit refunds. The other order loses a service for free; no
     refund loses the customer's money.
  2. A pending receipt is filed under the owner passed in explicitly -
     never storage.create_pending's default, which reads a threading.local
     belonging to a bot thread and is meaningless on a web worker.
  3. Nothing in the request body can pick a package, an account, or a shop
     that does not belong to the visitor the signature named.
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

from app import models, schemas
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
    # Ali is himself charged by the panel for every sale (services/
    # admin_billing.py) - the Mini App path goes through that gate too, as
    # it must, so he needs credit for a sale to complete at all.
    db.add(models.AdminUser(id=2, username="ali", hashed_password="x",
                            own_bot_token=ALI_TOKEN, telegram_id=111,
                            balance=5_000_000))
    db.add(models.AdminUser(id=3, username="reza", hashed_password="x",
                            own_bot_token=REZA_TOKEN, telegram_id=222))
    db.add(models.Node(id=1, name="node1", type=models.NodeType.mikrotik, mt_host="1.2.3.4", mt_username="u", mt_password="p"))
    # Ali's bundled package - has a services list, so it can be sold here.
    db.add(models.Package(id=10, name="پلن علی", quota_gb=20, duration_days=30,
                          price=100000, enabled=True, bot_enabled=True, owner_admin_id=2))
    # Ali's PLAIN package - no bundle. The bot asks which node and which
    # protocol over several questions; this screen does not, so it must
    # refuse rather than sell a purchase with nothing inside it.
    db.add(models.Package(id=12, name="پلن ساده", quota_gb=5, duration_days=30,
                          price=50000, enabled=True, bot_enabled=True, owner_admin_id=2))
    db.add(models.Package(id=11, name="پلن رضا", quota_gb=50, duration_days=30,
                          price=200000, enabled=True, bot_enabled=True, owner_admin_id=3))
    db.commit()
    db.add(models.PackageConnection(package_id=10, node_id=1, protocol=models.ConnectionType.wireguard))
    # Ali's customer, with money in the wallet.
    db.add(models.User(id=100, username="ali_cust", telegram_id=555, balance=500000, owner_admin_id=2))
    # Reza's customer, same Telegram id on purpose: one person may shop in
    # two resellers' bots, and each shop must only ever see its own.
    db.add(models.User(id=101, username="reza_cust", telegram_id=555, balance=900000, owner_admin_id=3))
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


def visitor(db, token=ALI_TOKEN, telegram_id=555):
    return miniapp.current_visitor(x_telegram_init_data=sign(token, telegram_id), db=db)


# Provisioning itself reaches a real MikroTik and is covered by its own
# tests; what is under test HERE is the money around it. So the one call
# that would go to the network is replaced by a Purchase row, leaving the
# debit, the ordering and the refund exactly as they are in production.
from app.services import user_ops  # noqa: E402


def _fake_provision(db, user, package, connections_override=None, comment=None):
    purchase = models.Purchase(
        user_id=user.id, status="active",
        package_name_snapshot=package.name,
        quota_bytes=int((package.quota_gb or 0) * 1024 ** 3),
    )
    db.add(purchase)
    db.flush()
    return purchase


user_ops.apply_package_as_purchase = _fake_provision


def call(fn, *args, **kwargs):
    """Returns the result, or 'STATUS: detail' for a refusal - so a test can
    assert on a refusal as plainly as on a success."""
    try:
        return fn(*args, **kwargs)
    except HTTPException as exc:
        return f"{exc.status_code}: {exc.detail}"


# --------------------------------------------------------------------------
print("--- what the visitor may buy ---")
db = make_db()
v_ali = visitor(db)

check("a package from another reseller's shop is not for sale here",
      str(call(miniapp._package_or_404, db, 2, 11)).startswith("404"), True)
check("...nor is one that does not exist",
      str(call(miniapp._package_or_404, db, 2, 999)).startswith("404"), True)
check("his own is", miniapp._package_or_404(db, 2, 10).name, "پلن علی")

plain = miniapp._package_or_404(db, 2, 12)
refusal = call(miniapp._require_bundled, plain)
check("a package with no services bundle is refused", str(refusal).startswith("400"), True)
check("...and says what to do instead", "ربات" in str(refusal), True)
check("a bundled one passes", miniapp._require_bundled(miniapp._package_or_404(db, 2, 10)), None)


# --------------------------------------------------------------------------
print("\n--- whose account can be charged ---")
check("his own account is found", miniapp._own_account(db, v_ali, None).username, "ali_cust")
check("...by name too", miniapp._own_account(db, v_ali, "ali_cust").username, "ali_cust")
# The same Telegram id holds an account in Reza's shop too. Naming it in the
# body from inside Ali's shop must find nothing - routers/bot.py's
# add_balance takes no owner_admin_id and would debit it happily.
check("an account in ANOTHER reseller's shop is not his here",
      miniapp._own_account(db, v_ali, "reza_cust"), None)
check("nor is a username that does not exist",
      miniapp._own_account(db, v_ali, "nobody"), None)


# --------------------------------------------------------------------------
print("\n--- paying from the wallet ---")
db = make_db()
before = db.get(models.User, 100).balance
result = miniapp.checkout(
    miniapp.CheckoutRequest(package_id=10), visitor=visitor(db), db=db
)
after = db.get(models.User, 100).balance
check("it reports success", result["status"], "done")
check("the wallet paid exactly the price", before - after, 100000)
purchases = db.query(models.Purchase).filter(models.Purchase.user_id == 100).all()
check("one service was provisioned", len(purchases), 1)
check("...from the right package", purchases[0].package_name_snapshot, "پلن علی")

print("\n--- a wallet that cannot cover it ---")
db = make_db()
db.get(models.User, 100).balance = 100
db.commit()
refusal = call(miniapp.checkout, miniapp.CheckoutRequest(package_id=10), visitor=visitor(db), db=db)
check("refused", str(refusal).startswith("400"), True)
check("...with the reason", "موجودی" in str(refusal), True)
check("nothing was provisioned", db.query(models.Purchase).count(), 0)
check("the wallet was not touched", db.get(models.User, 100).balance, 100)


# --------------------------------------------------------------------------
print("\n--- the debit is refunded when provisioning fails ---")
# The order (debit, then provision) is what makes a mid-flow failure
# recoverable at all; this is the recovery. Without it the customer has paid
# for a service that was never created, and finds out by noticing a number.
db = make_db()
from app.routers import bot as bot_router  # noqa: E402

original = bot_router.purchase_package
bot_router.purchase_package = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("node unreachable"))
try:
    before = db.get(models.User, 100).balance
    blew_up = False
    try:
        miniapp.checkout(miniapp.CheckoutRequest(package_id=10), visitor=visitor(db), db=db)
    except Exception:
        blew_up = True
    check("the failure is not swallowed", blew_up, True)
    check("the money is back", db.get(models.User, 100).balance, before)
    check("and no service was left behind", db.query(models.Purchase).count(), 0)
finally:
    bot_router.purchase_package = original


# --------------------------------------------------------------------------
print("\n--- the card receipt joins the bot's own queue ---")
import asyncio  # noqa: E402
import tempfile  # noqa: E402

from app.telegram_bot import config as bot_config, storage  # noqa: E402


class FakeUpload:
    def __init__(self, data):
        self._data = data

    async def read(self):
        return self._data


db = make_db()
bot_config.config.db_path = os.path.join(tempfile.mkdtemp(), "bot_data.db")
storage.init_db()

# The trap this guards: storage.create_pending defaults owner_admin_id to
# config.bot_owner_admin_id, and `config` is a threading.local owned by a
# bot thread. On a web worker it is whatever that thread happens to hold -
# here, deliberately, the WRONG reseller. The receipt must still be filed
# under Ali, because Ali's bot token is what signed the blob.
bot_config.config.bot_owner_admin_id = 3

sent: list[tuple] = []
from app.telegram_bot import runner  # noqa: E402

original_send = runner.send_photo_sync
runner.send_photo_sync = lambda chat_id, image, **kw: sent.append((chat_id, image, kw)) or True
try:
    out = asyncio.run(
        miniapp.checkout_receipt(
            package_id=10, account=None, comment=None,
            photo=FakeUpload(b"\xff\xd8jpegbytes"), visitor=visitor(db), db=db,
        )
    )
finally:
    runner.send_photo_sync = original_send
    bot_config.config.bot_owner_admin_id = None

check("it reports the receipt as pending", out["status"], "pending")
row = storage.get_pending(out["request_id"])
check("a pending row exists", bool(row), True)
check("filed under the reseller the SIGNATURE named, not the thread's",
      row["owner_admin_id"], 2)
check("...for the right package", row["package_id"], 10)
check("...at the right price", row["final_price"], 100000)
check("...against the customer's own account", row["target_username"], "ali_cust")
check("nothing was provisioned yet - that is the approval's job",
      db.query(models.Purchase).count(), 0)
check("the wallet was not charged", db.get(models.User, 100).balance, 500000)

print("\n--- and the right person is shown it ---")
check("exactly one recipient", len(sent), 1)
check("...the owning reseller", sent[0][0], 111)
check("...with the photo itself", sent[0][1], b"\xff\xd8jpegbytes")
check("...through the reseller's OWN bot, not the shared one",
      sent[0][2].get("token"), ALI_TOKEN)
check("...and the same Approve/Reject keyboard as a receipt sent in chat",
      sent[0][2].get("reply_markup") is not None, True)


# --------------------------------------------------------------------------
print("\n--- a customer with no account yet ---")
db = make_db()
storage.init_db()
runner.send_photo_sync = lambda *a, **k: True
try:
    out = asyncio.run(
        miniapp.checkout_receipt(
            package_id=10, account=None, comment=None,
            photo=FakeUpload(b"jpeg"), visitor=visitor(db, telegram_id=777), db=db,
        )
    )
finally:
    runner.send_photo_sync = original_send
row = storage.get_pending(out["request_id"])
check("the receipt is still accepted", out["status"], "pending")
check("...under the same tgNNN placeholder the bot uses",
      row["target_username"], "tg777")
# Whereas paying from a wallet that does not exist cannot work, and says so.
refusal = call(miniapp.checkout, miniapp.CheckoutRequest(package_id=10),
               visitor=visitor(db, telegram_id=777), db=db)
check("but the wallet path refuses", str(refusal).startswith("400"), True)
check("...telling them where to start", "ربات" in str(refusal), True)


# --------------------------------------------------------------------------
print("\n--- the sender survives being called from inside the event loop ---")
# The bug this catches, reported as «رسیدی که از مینی اپ ارسال میشه تو بات
# ادمین نمیاد برای تایید»: checkout_receipt is `async def` (it must await the
# upload), so it runs ON the event loop, and send_photo_sync used
# asyncio.run(), which raises "cannot be called from a running event loop"
# there. The raise was caught and logged, so a receipt was accepted, filed,
# and then shown to nobody.
#
# The earlier test above could never have caught it: it replaces
# send_photo_sync itself. This one replaces only the TRANSPORT - the aiogram
# Bot - and exercises the real sender, from the real place.
uploaded: list = []


class _FakeSession:
    async def close(self):
        return None


class _FakeBot:
    def __init__(self):
        self.session = _FakeSession()

    async def send_photo(self, chat_id, photo, caption=None, reply_markup=None):
        uploaded.append(chat_id)
        return True

    async def send_message(self, chat_id, text, **kw):
        uploaded.append(chat_id)
        return True


original_make_bot = runner._make_bot
runner._make_bot = lambda token: _FakeBot()
try:
    check("off the loop, as it always worked",
          runner.send_photo_sync(1, b"x", token="1:AA"), True)

    async def from_inside_the_loop():
        return runner.send_photo_sync(2, b"x", token="1:AA")

    check("ON the loop - where an `async def` endpoint calls from",
          asyncio.run(from_inside_the_loop()), True)
    check("both actually reached the transport", uploaded, [1, 2])

    # And end to end: the real endpoint, the real sender, no stub between
    # them. This is the exact path a customer's receipt takes.
    db = make_db()
    storage.init_db()
    uploaded.clear()
    out = asyncio.run(
        miniapp.checkout_receipt(
            package_id=10, account=None, comment=None,
            photo=FakeUpload(b"jpeg"), visitor=visitor(db), db=db,
        )
    )
    check("a receipt sent through the whole real path reaches the owner",
          uploaded, [111])
    check("...and is still filed", bool(storage.get_pending(out["request_id"])), True)
finally:
    runner._make_bot = original_make_bot


# --------------------------------------------------------------------------
print("\n--- the shape of the code, not just its behaviour ---")
src = inspect.getsource(miniapp)
check("the receipt's owner is passed explicitly, never defaulted",
      "owner_admin_id=owner," in src, True)
check("the wallet is debited before provisioning",
      src.index("add_balance") < src.index("purchase_package"), True)
check("there is a refund path", "refund" in src.lower(), True)
check("no second approval queue was invented - it reuses the bot's storage",
      "storage.create_pending" in src, True)
check("the notification is pushed off the event loop, not run on it",
      "asyncio.to_thread(_notify_receipt" in src, True)
check("and the sender itself no longer calls asyncio.run blindly",
      "asyncio.run(" in inspect.getsource(runner.send_photo_sync), False)

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("پول در مینی‌اپ نه گم می‌شود و نه به حساب اشتباه می‌رود")
