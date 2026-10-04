"""Receipt Void phase P3 (sixth batch) - the bot registers an approval with
the panel before it acts, and that can never stop an approval (design 5.6).
Run:  python3 backend/tests/test_bot_approval_session.py
"""
from __future__ import annotations

import asyncio
import inspect
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
folder = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{folder}/panel.db"

from sqlalchemy import select

from app import models, models_receipt_void as rv
from app.database import SessionLocal, engine
from app.services import auto_approve, receipt_approval_runtime as runtime, receipt_void_schema
from app.telegram_bot import approval_session, panel_bridge, storage
from app.telegram_bot.config import config
from app.telegram_bot.handlers import admin_pending
from app.telegram_bot.remote_bridge import RemoteBridge

failures: list[str] = []
A = rv.receipt_approvals


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


config.db_path = os.path.join(folder, "bot.db")
storage.init_db()
models.Base.metadata.create_all(engine)
receipt_void_schema.bootstrap(engine, SessionLocal)
db = SessionLocal()
root = models.AdminUser(username="root", hashed_password="x", is_superadmin=True, telegram_id=1000)
package = models.Package(name="P", quota_gb=5, duration_days=30, price=1000)
db.add_all([root, package, models.PanelSettings(id=1), models.User(username="ali", telegram_id=111)])
db.commit()

PENDING = {"id": 7, "kind": "renew", "target_username": "ali", "price": 1000, "final_price": 900, "package_id": package.id,
           "telegram_id": 111, "payment_card_id": None, "discount_code": "OFF", "referral_code": "R", "node_id": None,
           "protocol": None, "renew_purchase_id": None, "receipt_file_id": "file-1", "owner_admin_id": None}

print("--- instance id ---")
first = storage.instance_id()
check("made once, then stable, and a uuid", (first == storage.instance_id(), len(first)), (True, 36))

print("--- the request body ---")
body = approval_session.intent_of(PENDING)
check("renew: paid amount is the discounted price, list price kept, no referral on a renewal",
      (body["amount"], body["list_price"], body["referral_code"], body["discount_code"], body["pending_local_id"],
       body["pending_source_instance_id"] == first, body["connections"]), (900, 1000, None, "OFF", 7, True, []))
new = approval_session.intent_of({**PENDING, "kind": "new", "node_id": 3, "protocol": "wireguard", "final_price": None})
check("new with a hand-picked service: one request slot; no final price -> the price",
      (new["connections"], new["amount"], new["referral_code"]),
      ([{"node_id": 3, "protocol": "wireguard", "flow": ""}], 1000, "R"))
top = approval_session.intent_of({**PENDING, "kind": "topup", "final_price": 5})
check("topup: the price itself, no package, no discount", (top["amount"], top["package_id"], top["discount_code"]), (1000, None, None))
check("a link request is never registered", approval_session.intent_of({**PENDING, "kind": "link"}), None)
check("the body carries no mode and no 'returning' flag (only the panel decides those)",
      [k for k in body if k in ("mode", "registration_mode", "returning", "proceed_legacy")], [])


def run(coro):
    return asyncio.run(coro)


print("--- off: nothing happens ---")
answer = run(approval_session.begin(PENDING, approved_by_telegram_id=1000))
check("the panel answers 'off', nothing is stored",
      (answer, len(db.execute(select(A.c.id)).all())), ({"mode": "off", "approval_uuid": None, "proceed_legacy": True}, 0))
check("no approver and not auto (the free-trial path): no call at all",
      run(approval_session.begin(PENDING)), approval_session.LEGACY)

print("--- shadow: registered, through the real in-process bridge ---")
runtime.set_requested_mode(db, registration_mode="shadow")
db.commit()
manual = run(approval_session.begin(PENDING, approved_by_telegram_id=1000))
db.expire_all()
row = dict(db.execute(select(A)).mappings().one())
check("manual approval registered with the approver and the bot's instance id",
      (manual["mode"], manual["proceed_legacy"], row["approval_mode"], row["approved_by_telegram_id"],
       row["pending_source_instance_id"] == first, row["pending_local_id"], row["amount_snapshot"]),
      ("shadow", False, "manual", 1000, True, 7, 900))
auto = run(approval_session.begin({**PENDING, "id": 8}, auto=True, local_decision="allowed"))
check("auto approval registered as auto, with the central verdict returned for the log",
      (auto["mode"], "central_reason" in auto["central_decision"]), ("shadow", True))
failed = run(approval_session.begin({**PENDING, "id": 9}, approved_by_telegram_id=4242))
check("a registration the panel refuses is a shadow error and the bot proceeds",
      (failed["approval_uuid"], failed["shadow_error"], failed["proceed_legacy"]), (None, "manual_approver_not_authorized", True))

print("--- nothing here can stop an approval ---")
real = panel_bridge.api.begin_approval


async def boom(intent, mode):
    raise RuntimeError("panel unreachable")


panel_bridge.api.begin_approval = boom
try:
    check("the bridge raising -> legacy", run(approval_session.begin(PENDING, approved_by_telegram_id=1000)), approval_session.LEGACY)
finally:
    panel_bridge.api.begin_approval = real
check("a broken pending row -> legacy", run(approval_session.begin({"kind": "new"}, approved_by_telegram_id=1)), approval_session.LEGACY)
source = inspect.getsource(admin_pending.perform_approval)
check("perform_approval begins the session before it provisions anything",
      0 < source.index("approval_session.begin(") < source.index("api.list_packages()"), True)
check("the manual button passes who tapped it; the automatic path says auto",
      ("approved_by_telegram_id=call.from_user.id" in inspect.getsource(admin_pending.cb_approval),
       "perform_approval(pending, bot, auto=True)" in inspect.getsource(auto_approve.try_auto_approve)), (True, True))

print("--- finish ---")
calls = []
real_finalize = panel_bridge.api.finalize_approval


async def fake_finalize(uuid_, failed=False):
    calls.append((uuid_, failed))
    if uuid_ == "boom":
        raise RuntimeError("down")
    return {"state": "completed"}


panel_bridge.api.finalize_approval = fake_finalize
try:
    run(approval_session.finish(approval_session.LEGACY, ok=True))
    run(approval_session.finish({"approval_uuid": "u1"}, ok=True))
    run(approval_session.finish({"approval_uuid": "u2"}, ok=False))
    run(approval_session.finish({"approval_uuid": "boom"}, ok=True))
finally:
    panel_bridge.api.finalize_approval = real_finalize
check("nothing for an unregistered approval; success and failure are reported; an error is swallowed",
      calls, [("u1", False), ("u2", True), ("boom", False)])
done = run(panel_bridge.api.finalize_approval(manual["approval_uuid"], failed=True))
check("through the real in-process bridge: a registered approval with no effect, reported failed", done["state"], "failed")

print("--- remote bridge ---")
sent2 = {}
bridge2 = RemoteBridge("http://panel:8000/api/bot", "k")
bridge2._request = lambda method, path, **kw: sent2.update(method=method, path=path, **kw) or {"state": "completed"}
run(bridge2.finalize_approval("abc", failed=True))
check("finalize posts to the approval's own URL",
      (sent2["base_url"] + sent2["path"], sent2["json"]),
      ("http://panel:8000/api/accounting/receipt-approvals/abc/finalize", {"failed": True}))
sent = {}
bridge = RemoteBridge("http://panel:8000/api/bot", "k")
bridge._request = lambda method, path, **kw: sent.update(method=method, path=path, **kw) or {"mode": "off"}
run(bridge.begin_approval({"x": 1}, "manual"))
check("posts to /api/accounting/receipt-approvals/<mode> on the same panel",
      (sent["method"], sent["base_url"] + sent["path"], sent["json"]),
      ("POST", "http://panel:8000/api/accounting/receipt-approvals/manual", {"x": 1}))

db.close()
print()
if failures:
    print(f"{len(failures)} FAILED:")
    for label in failures:
        print(f"  - {label}")
    sys.exit(1)
print("all checks passed")
