"""Receipt Void phase P3 (fourth batch) - the central auto-approval
evaluator, central 'returning customer' history and the 60-minute cap, all
log-only in shadow (design 5.4).
Run:  python3 backend/tests/test_receipt_approval_auto.py
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import inspect
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import models, models_receipt_void as rv
from app.services import bot_auth, receipt_void_schema
from app.services import receipt_approval_auto as auto
from app.services import receipt_approval_intent as ri
from app.services import receipt_approval_registration as reg
from app.services import receipt_approval_runtime as runtime

failures: list[str] = []
A, SUB, EV = rv.receipt_approvals, rv.auto_approval_subjects, rv.auto_approval_rate_events


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


S = auto.AutoSettings(enabled=True, ignore_hours=False, from_hour=9, to_hour=23, max_amount=5000, returning_only=True,
                      source="global")


def ev(**changes):
    args = dict(kind="new", amount=1000, telegram_id=1, settings=S, returning=True, hour=12)
    args.update(changes)
    return auto.evaluate(**args)


print("--- the pure evaluator ---")
check("allowed", ev(), (True, "allowed"))
check("each rule and its reason code",
      [ev(kind="topup")[1], ev(settings=dataclasses.replace(S, enabled=False))[1], ev(hour=3)[1], ev(amount=5001)[1],
       ev(telegram_id=None)[1], ev(returning=False)[1], ev(returning=auto.UNKNOWN)[1]],
      ["kind_not_auto_eligible", "disabled", "outside_window", "over_cap", "no_telegram_identity", "not_returning",
       "returning_unknown"])
check("the amount cap is inclusive; 0 means no cap", (ev(amount=5000)[0], ev(amount=10**9, settings=dataclasses.replace(S, max_amount=0))[0]),
      (True, True))
check("returning_only off: a first-time buyer is allowed", ev(returning=False, settings=dataclasses.replace(S, returning_only=False))[0], True)
night = dataclasses.replace(S, from_hour=22, to_hour=6)
check("window: wraps past midnight; equal hours mean any time; ignore_hours",
      ([auto.in_window(night, h) for h in (23, 2, 6, 12)], auto.in_window(dataclasses.replace(S, from_hour=5, to_hour=5), 3),
       auto.in_window(dataclasses.replace(S, ignore_hours=True), 3)), ([True, True, False, False], True, True))
check("the evaluator does no I/O and does not call the bot's local decision",
      [w for w in ("db.", "auto_approve.", "storage", "utcnow") if w in inspect.getsource(auto.evaluate)], [])

print("--- the cap arithmetic ---")
now = dt.datetime(2026, 1, 1, 12, 0, 0)
check("never granted / 61 min ago / exactly 60 min ago -> allowed",
      [auto.retry_after_seconds(x, now) for x in (None, now - dt.timedelta(minutes=61), now - dt.timedelta(minutes=60))],
      [None, None, None])
check("59m59.5s ago -> 1 second (rounded up); just now -> 3600",
      (auto.retry_after_seconds(now - dt.timedelta(minutes=59, seconds=59, milliseconds=500), now),
       auto.retry_after_seconds(now, now)), (1, 3600))

engine = create_engine(f"sqlite:///{tempfile.mkdtemp()}/t.db")
models.Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)
receipt_void_schema.bootstrap(engine, Session)
db = Session()
admin = models.AdminUser(username="adm", hashed_password="x", is_superadmin=False)
db.add_all([admin, models.PanelSettings(id=1), models.BotSettings(id=1, auto_approve_enabled=True, auto_approve_ignore_hours=True,
                                                                 auto_approve_max_amount=5000, auto_approve_returning_only=True)])
db.flush()
package = models.Package(name="P", quota_gb=5, duration_days=30, price=1000)
shared = models.User(username="ali", telegram_id=111)
owned = models.User(username="sara", telegram_id=111, owner_admin_id=admin.id)      # same Telegram, another tenant
fresh_user = models.User(username="newcomer", telegram_id=555)
db.add_all([package, shared, owned, fresh_user])
db.commit()

print("--- settings ---")
check("global settings", (auto.load_settings(db, None).source, auto.load_settings(db, None).max_amount), ("global", 5000))
check("an owner with no override of their own uses the global row", auto.load_settings(db, admin.id).source, "global")
admin.own_auto_approve_enabled = False
db.commit()
check("an owner's explicit override wins - even when it says 'off'",
      (auto.load_settings(db, admin.id).source, auto.load_settings(db, admin.id).enabled), ("owner", False))

print("--- central history ---")
check("nothing on record", auto.history_returning(db, "shared", 111), False)
db.add(models.LedgerEntry(kind="sale_new", amount=1, user_id=shared.id))
db.commit()
check("a sale with no card evidence at all -> unknown (never assumed to be a card payment)",
      auto.history_returning(db, "shared", 111), auto.UNKNOWN)
db.add(models.LedgerEntry(kind="sale_new", amount=1, user_id=shared.id, payment_method="wallet"))
db.commit()
check("a wallet sale is no evidence either", auto.history_returning(db, "shared", 111), auto.UNKNOWN)
card_sale = models.LedgerEntry(kind="sale_renew", amount=1, user_id=shared.id, payment_method="card")
db.add(card_sale)
db.commit()
check("a card sale -> returning", auto.history_returning(db, "shared", 111), True)
check("the same Telegram in another tenant is another person", auto.history_returning(db, f"admin:{admin.id}", 111), False)
card_sale.voided_at = dt.datetime.utcnow()
db.commit()
check("a voided sale is not history", auto.history_returning(db, "shared", 111), auto.UNKNOWN)
card_sale.voided_at = None
db.commit()

print("--- registration records the central decision, and gates nothing ---")
internal = bot_auth.BotPrincipal.internal(None)
runtime.set_requested_mode(db, registration_mode="shadow")
runtime.set_requested_mode(db, auto_rate_limit_mode="shadow")
db.commit()


def begin(local_id, username="ali", amount=1000, local="allowed"):
    return reg.begin(db, internal, ri.ApprovalIntent(
        pending_source_instance_id="inst", pending_local_id=local_id, kind="renew", target_username=username,
        amount=amount, package_id=package.id), approval_mode="auto", local_decision=local)


def events():
    return [(e["outcome"], e["reason_code"], e["local_decision"], e["retry_after_seconds"])
            for e in db.execute(select(EV).order_by(EV.c.id)).mappings()]


first = begin(1)
check("returning customer: granted", (first["central_decision"], events()),
      ({"central_allowed": True, "central_reason": "allowed", "returning": True}, [("granted", None, "allowed", None)]))
subject = dict(db.execute(select(SUB)).mappings().one())
check("the subject row is (tenant, Telegram) and points at the approval",
      (subject["tenant_scope_key"], subject["telegram_id"], subject["last_approval_uuid"]), ("shared", 111, first["approval_uuid"]))
begin(1)
check("re-registering the same pending consumes nothing and logs nothing", len(events()), 1)
second = begin(2)
last = events()[-1]
check("a second receipt inside 60 minutes: in shadow it is REGISTERED anyway, and logged as shadow_would_limit",
      (bool(second["approval_uuid"]), second["proceed_legacy"], last[0], last[1], 3590 <= last[3] <= 3600),
      (True, False, "shadow_would_limit", "within_60_minutes", True))
check("still one subject row, now pointing at the newer grant",
      [(r["last_approval_uuid"] == second["approval_uuid"], r["version"]) for r in db.execute(select(SUB)).mappings()], [(True, 1)])
db.execute(SUB.update().values(last_granted_at=dt.datetime.utcnow() - dt.timedelta(minutes=61)))
db.commit()
begin(3)
check("after the window: granted again", events()[-1][0], "granted")
over = begin(4, amount=9000, local="denied")
check("central says no (over the cap): logged as policy_denied with the bot's own verdict beside it - and not blocked",
      (bool(over["approval_uuid"]), over["central_decision"]["central_reason"], events()[-1][:3]),
      (True, "over_cap", ("policy_denied", "over_cap", "denied")))
newcomer = begin(5, username="newcomer")
check("a first-time buyer: central not_returning", newcomer["central_decision"],
      {"central_allowed": False, "central_reason": "not_returning", "returning": False})
snapshot = json.loads(db.execute(select(A.c.auto_approve_policy_snapshot).where(A.c.approval_uuid == over["approval_uuid"])).scalar())
check("the policy that applied is frozen on the approval",
      (snapshot["max_amount"], snapshot["source"], snapshot["central_reason"]), (5000, "global", "over_cap"))
runtime.set_requested_mode(db, auto_rate_limit_mode="off")
db.commit()
db.execute(SUB.update().values(last_granted_at=dt.datetime.utcnow()))
db.commit()
begin(6)
check("limiter off: no would_limit event, but the grant and subject are still written",
      (events()[-1][0], db.execute(select(SUB.c.version).where(SUB.c.telegram_id == 111)).scalar() >= 3), ("granted", True))
check("the event log has no username and no amount column",
      [c.name for c in EV.columns if c.name in ("username", "target_username", "amount")], [])
before = len(events())
admin_root = models.AdminUser(username="root", hashed_password="x", is_superadmin=True, telegram_id=1000)
db.add(admin_root)
db.commit()
reg.begin(db, internal, ri.ApprovalIntent(pending_source_instance_id="inst", pending_local_id=8, kind="renew",
                                          target_username="ali", amount=1000, package_id=package.id),
          approval_mode="manual", approved_by_telegram_id=1000)
check("a manual approval is never rate-limited and writes no rate event", len(events()), before)

db.close()
print()
if failures:
    print(f"{len(failures)} FAILED:")
    for label in failures:
        print(f"  - {label}")
    sys.exit(1)
print("all checks passed")
