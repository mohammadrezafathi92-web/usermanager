"""Admins are told about an AUTOMATIC approval: receipt photo, details,
result, «خودکار تأیید شد», no approve button. Delivery is recorded and
retried by the bot apart from the approval, which never runs twice. A
transient failure is retried with no last attempt; a permanent one stays
durable, visible and resendable. Worker, resend button and a restarted bot
never send one notice at once, and a notice only goes to its own shop.

Run:  python3 backend/tests/test_auto_approve_notices.py
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import sqlite3
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")
_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_db.close()
os.environ["BOT_DB_PATH"] = _db.name

from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError  # noqa: E402
from aiogram.methods import SendMessage  # noqa: E402

from app.services import auto_approve  # noqa: E402
from app.telegram_bot import approval_notices, approval_session, storage  # noqa: E402
from app.telegram_bot.config import config  # noqa: E402
from app.telegram_bot.handlers import admin_pending, customer_common  # noqa: E402

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


class Bot:
    def __init__(self):
        self.sent = []
        self.fail = None          # exception instance to raise on the next sends

    async def _maybe_fail(self):
        if self.fail is not None:
            raise self.fail

    async def send_photo(self, chat_id, photo, caption=None, **kw):
        await self._maybe_fail()
        self.sent.append(("photo", chat_id, photo, caption, kw))

    async def send_message(self, chat_id, text, **kw):
        await self._maybe_fail()
        self.sent.append(("text", chat_id, None, text, kw))


def network_error():
    return TelegramNetworkError(method=SendMessage(chat_id=1, text="x"), message="Request timeout")


def forbidden():
    return TelegramForbiddenError(method=SendMessage(chat_id=1, text="x"), message="bot was blocked by the user")


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def rows(request_id):
    with sqlite3.connect(_db.name) as c:
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute("SELECT * FROM approval_notices WHERE request_id = ? ORDER BY chat_id",
                                           (request_id,))]


def make_due(request_id):
    with sqlite3.connect(_db.name) as c:
        c.execute("UPDATE approval_notices SET next_attempt_at = '2000-01-01T00:00:00' WHERE request_id = ?",
                  (request_id,))


asyncio.set_event_loop(asyncio.new_event_loop())
config.configure("777000:token-secret-part", {111}, set(), True, 20)
storage.init_db()
PKG = {"id": 3, "name": "ماهانه", "quota_gb": 50, "duration_days": 30, "price": 150000}


def new_request(owner=20, kind="renew"):
    rid = storage.create_pending(telegram_id=5005, telegram_username="cust", telegram_name="Cust", kind=kind,
                                 package=PKG, target_username="cust_vpn", receipt_file_id="RECEIPT-FILE-ID",
                                 owner_admin_id=owner)
    return storage.get_pending(rid)


async def owner_label(_owner):
    return "yasmin"


targets = {"value": {111, 222}}


async def notify_targets(_pending):
    value = targets["value"]
    if isinstance(value, Exception):
        raise value
    return set(value)


with patch.object(admin_pending, "_owner_label", owner_label), \
        patch.object(customer_common, "_notify_targets", notify_targets):

    print("--- an automatic approval reaches every admin of the shop, with the receipt ---")
    bot = Bot()
    p1 = new_request()
    run(approval_notices.queue_auto_approved(p1, "✅ تایید و فعال‌سازی شد.", "شرایط برقرار است"))
    check("one notice per recipient", [r["chat_id"] for r in rows(p1["id"])], [111, 222])
    check("the bot id is the public part of the token only",
          {r["bot_id"] for r in rows(p1["id"])}, {"777000"})
    run(approval_notices.deliver_due(bot, request_id=p1["id"]))
    check("sent as the receipt photo to both admins",
          sorted((kind, chat, photo) for kind, chat, photo, _c, _k in bot.sent),
          [("photo", 111, "RECEIPT-FILE-ID"), ("photo", 222, "RECEIPT-FILE-ID")])
    caption = bot.sent[0][3]
    check("says it was approved automatically", "خودکار تأیید شد" in caption, True)
    check("carries customer, amount, kind and the real result",
          all(x in caption for x in ("cust", "150,000", "تمدید", "تایید و فعال‌سازی شد")), True)
    check("no approve button (no reply markup at all)", all(not k for *_x, k in bot.sent), True)
    check("both marked sent", {r["status"] for r in rows(p1["id"])}, {"sent"})
    run(approval_notices.queue_auto_approved(p1, "✅", "x"))
    check("queuing the same approval again adds nothing", len(rows(p1["id"])), 2)

    print("--- a failed send is kept, retried with backoff, and visible ---")
    bot = Bot()
    p2 = new_request()
    bot.fail = network_error()
    run(approval_notices.queue_auto_approved(p2, "✅ تایید و فعال‌سازی شد.", "r"))
    run(approval_notices.deliver_due(bot, request_id=p2["id"]))
    check("network failure: queued for retry, nothing claimed as sent",
          [(r["status"], r["attempts"]) for r in rows(p2["id"])], [("failed", 1), ("failed", 1)])
    check("not due again immediately", run(approval_notices.deliver_due(bot, request_id=p2["id"])), 0)
    listed = storage.undelivered_notices("777000", {20}, False)
    check("listed for the shop's own admin (/notices)", {n["request_id"] for n in listed}, {p2["id"]})
    check("not listed for another shop's admin", storage.undelivered_notices("777000", {99}, False), [])
    bot.fail = None
    make_due(p2["id"])
    check("delivered on the retry", run(approval_notices.deliver_due(bot, request_id=p2["id"])), 2)

    print("--- a permanent failure stops, and can be retried by hand ---")
    bot = Bot()
    p3 = new_request()
    targets["value"] = {333}
    bot.fail = forbidden()
    run(approval_notices.queue_auto_approved(p3, "✅", "r"))
    run(approval_notices.deliver_due(bot, request_id=p3["id"]))
    check("blocked chat: abandoned without retries", rows(p3["id"])[0]["status"], "gave_up")
    nid = rows(p3["id"])[0]["id"]
    check("a manual retry re-queues it", storage.retry_notice(nid, "777000"), True)
    check("...only from this bot", storage.retry_notice(nid, "999"), False)
    bot.fail = None
    check("...and it is sent", run(approval_notices.deliver_due(bot, request_id=p3["id"])), 1)

    print("--- a transient failure is never final ---")
    bot = Bot()
    p4 = new_request()
    targets["value"] = {444}
    bot.fail = network_error()
    run(approval_notices.queue_auto_approved(p4, "✅", "r"))
    for _ in range(len(approval_notices.BACKOFF) * 3):
        make_due(p4["id"])
        run(approval_notices.deliver_due(bot, request_id=p4["id"]))
    row = rows(p4["id"])[0]
    check("after 15 failed attempts it is still queued for retry, never abandoned",
          (row["status"], row["attempts"]), ("failed", len(approval_notices.BACKOFF) * 3))
    wait = (dt.datetime.fromisoformat(row["next_attempt_at"]) - dt.datetime.utcnow()).total_seconds()
    check("...the wait is capped at the last step (an hour)", 3500 < wait <= approval_notices.BACKOFF[-1], True)
    check("...and it stays listed for the shop's admins", any(n["id"] == row["id"] for n in
                                                              storage.undelivered_notices("777000", {20}, False)), True)
    bot.fail = None
    make_due(p4["id"])
    check("...and goes out once the network is back", run(approval_notices.deliver_due(bot, request_id=p4["id"])), 1)

    print("--- recipients that cannot be resolved yet are resolved on retry ---")
    bot = Bot()
    p5 = new_request()
    targets["value"] = RuntimeError("panel unreachable")
    run(approval_notices.queue_auto_approved(p5, "✅", "r"))
    check("a placeholder row waits for the recipients", [r["chat_id"] for r in rows(p5["id"])], [0])
    targets["value"] = {555}
    make_due(p5["id"])
    run(approval_notices.deliver_due(bot, request_id=p5["id"]))
    run(approval_notices.deliver_due(bot, request_id=p5["id"]))
    check("then the real recipient is queued and sent",
          [(r["chat_id"], r["status"]) for r in rows(p5["id"])], [(0, "sent"), (555, "sent")])

    print("--- a Mini App receipt (no Telegram file id) still arrives as a photo ---")
    bot = Bot()
    targets["value"] = {666}
    p7 = storage.get_pending(storage.create_pending(
        telegram_id=6006, telegram_username="mini", telegram_name="Mini", kind="new", package=PKG,
        target_username="tg6006", owner_admin_id=20))
    run(approval_notices.queue_auto_approved(p7, "✅", "r", photo=b"\xff\xd8jpeg-bytes"))
    run(approval_notices.deliver_due(bot, request_id=p7["id"]))
    from aiogram.types import BufferedInputFile
    check("sent as an uploaded photo from the stored bytes",
          (bot.sent[0][0], isinstance(bot.sent[0][2], BufferedInputFile)), ("photo", True))

    print("--- the approval runs once, whatever happens to the notice ---")
    targets["value"] = {111}
    bot = Bot()
    bot.fail = network_error()
    p6 = new_request()
    calls = []

    async def perform(pending, bot, **kw):
        calls.append(pending["id"])
        storage.set_status(pending["id"], "approved")
        return True, "✅ تایید و فعال‌سازی شد."

    async def begin(pending, **kw):
        return {"mode": "legacy"}

    with patch.object(auto_approve, "decide", lambda p: (True, "همه شرط‌ها برقرار است")), \
            patch.object(approval_session, "begin", begin), \
            patch.object(admin_pending, "perform_approval", perform):
        approved, _reason = run(auto_approve.try_auto_approve(p6, bot))
    check("approved although the notice could not be sent", approved, True)
    check("the purchase/renewal ran exactly once", calls, [p6["id"]])
    check("the notice is queued for retry", rows(p6["id"])[0]["status"], "failed")
    bot.fail = None
    make_due(p6["id"])
    run(approval_notices.deliver_due(bot))
    check("the retry worker pass sends it without touching the approval", (rows(p6["id"])[0]["status"], calls),
          ("sent", [p6["id"]]))

    print("--- worker, resend button and the send after approval never send one notice at once ---")
    targets["value"] = {777}
    p8 = new_request()
    run(approval_notices.queue_auto_approved(p8, "✅", "r"))

    class SlowBot(Bot):
        async def send_photo(self, chat_id, photo, caption=None, **kw):
            await asyncio.sleep(0.05)
            await super().send_photo(chat_id, photo, caption=caption, **kw)
    slow = SlowBot()

    async def at_once():
        return await asyncio.gather(approval_notices.deliver_due(slow, request_id=p8["id"]),
                                    approval_notices.deliver_due(slow), approval_notices.deliver_due(slow))
    check("three concurrent senders: sent exactly once", (sum(run(at_once())), len(slow.sent)), (1, 1))

    p9 = new_request()
    run(approval_notices.queue_auto_approved(p9, "✅", "r"))
    nid = rows(p9["id"])[0]["id"]
    check("a sender claims the row", storage.claim_notice(nid, "777000", approval_notices.LEASE), True)
    check("...a second claim (worker / button) is refused while it sends",
          storage.claim_notice(nid, "777000", approval_notices.LEASE), False)
    check("...the resend button cannot re-queue a row being sent", storage.retry_notice(nid, "777000"), False)
    check("...nor is it listed as undelivered meanwhile",
          any(n["id"] == nid for n in storage.undelivered_notices("777000", {20}, False)), False)

    print("--- a bot that died mid-send: the claim expires and it is sent after the restart ---")
    bot = Bot()
    check("not due again before the claim expires", run(approval_notices.deliver_due(bot, request_id=p9["id"])), 0)
    with sqlite3.connect(_db.name) as c:
        c.execute("UPDATE approval_notices SET claimed_until = '2000-01-01T00:00:00' WHERE id = ?", (nid,))
    check("expired claim: sent once by the restarted bot", (run(approval_notices.deliver_due(bot)), len(bot.sent)), (1, 1))
    check("...and marked sent (not sent again)", (rows(p9["id"])[0]["status"],
                                                  run(approval_notices.deliver_due(bot)), len(bot.sent)), ("sent", 0, 1))
    check("a late outcome of the dead sender changes nothing", (storage.mark_notice(nid, "failed"),
                                                                rows(p9["id"])[0]["status"]), (None, "sent"))

    print("--- a notice stays in its own shop and never re-runs the approval ---")
    targets["value"] = {888}
    p10 = new_request(owner=30)
    bot = Bot()
    bot.fail = forbidden()
    run(approval_notices.queue_auto_approved(p10, "✅", "r"))
    run(approval_notices.deliver_due(bot, request_id=p10["id"]))
    nid = rows(p10["id"])[0]["id"]
    check("listed for shop 30's admins", any(n["id"] == nid for n in storage.undelivered_notices("777000", {30}, False)),
          True)
    check("not for shop 20's", any(n["id"] == nid for n in storage.undelivered_notices("777000", {20}, False)), False)

    class Call:
        def __init__(self):
            self.answers = []

        async def answer(self, text, show_alert=False):
            self.answers.append(text)

    async def scope_20(_event):
        return "scope-20"
    calls = []

    async def no_rerun(*a, **k):
        calls.append(a)
        raise AssertionError("a notice re-ran the approval")
    with patch.object(admin_pending, "_scope_of", scope_20), \
            patch.object(admin_pending, "_filters", lambda scope: ({20}, False)), \
            patch.object(admin_pending, "perform_approval", no_rerun):
        call = Call()
        bot.fail = None
        run(admin_pending.cb_notice_retry(call, admin_pending.NoticeRetryCB(notice_id=nid), bot))
        check("shop 20's admin cannot resend shop 30's notice", (call.answers, len(bot.sent), rows(p10["id"])[0]["status"]),
              (["این اعلان پیدا نشد"], 0, "gave_up"))
    with patch.object(admin_pending, "_scope_of", scope_20), \
            patch.object(admin_pending, "_filters", lambda scope: ({30}, False)), \
            patch.object(admin_pending, "perform_approval", no_rerun):
        call = Call()
        run(admin_pending.cb_notice_retry(call, admin_pending.NoticeRetryCB(notice_id=nid), bot))
        check("shop 30's admin resends it: to its own recipient only",
              (call.answers, [chat for _k, chat, *_r in bot.sent]), (["ارسال شد"], [888]))
    check("...and neither the worker nor the resend ever ran the approval again", calls, [])

os.unlink(_db.name)
print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("all checks passed")
