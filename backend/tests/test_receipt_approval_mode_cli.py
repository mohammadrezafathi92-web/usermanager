"""The management command that switches receipt-approval registration
between off and shadow, and reports what has been recorded.
Run:  python3 backend/tests/test_receipt_approval_mode_cli.py
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models
from app.scripts import receipt_approval_mode as cli
from app.services import receipt_approval_runtime as runtime

failures: list[str] = []


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def run(*args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.main(["x", *args])
    return code, out.getvalue()


engine = create_engine(f"sqlite:///{tempfile.mkdtemp()}/t.db")
models.Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)
cli.engine, cli.SessionLocal = engine, Session

code, text = run("status")
check("status on a fresh database: off, keys ready, nothing recorded",
      (code, "requested=off  effective=off" in text, "ready=True" in text, text.count("none")), (0, True, True, 3))
code, text = run("shadow")
check("shadow: registration and the auto limiter both go to shadow",
      (code, text.count("requested=shadow  effective=shadow")), (0, 2))
db = Session()
runtime.record_shadow_event(db, pending_source_instance_id="i", pending_local_id=1, stage="effect", error_code="effect_manifest_mismatch")
db.commit()
db.close()
check("status shows what was recorded", "effect/effect_manifest_mismatch=1" in run("status")[1], True)
code, text = run("off")
check("off: both back to off; the recorded rows stay",
      (code, text.count("requested=off  effective=off"), "effect/effect_manifest_mismatch=1" in text), (0, 2, True))
check("'required' and anything else are not accepted", (run("required")[0], run("nope")[0]), (2, 2))

print("--- recent ---")
check("nothing yet", run("recent"), (0, "no approvals recorded yet\n"))
from app.services import bot_auth, receipt_approval_effects as fx, receipt_approval_intent as ri
from app.services import receipt_approval_registration as reg
run("shadow")
db = Session()
package = models.Package(name="P", quota_gb=5, duration_days=30, price=1000)
user = models.User(username="ali", telegram_id=111)
db.add_all([package, user, models.AdminUser(username="root", hashed_password="x", is_superadmin=True, telegram_id=1000)])
db.commit()
internal = bot_auth.BotPrincipal.internal(None)
uuid_ = reg.begin(db, internal, ri.ApprovalIntent(pending_source_instance_id="i", pending_local_id=42, kind="topup",
                                                  target_username="ali", amount=25000), approval_mode="manual",
                  approved_by_telegram_id=1000)["approval_uuid"]
entry = models.LedgerEntry(kind="wallet_topup", amount=25000, user_id=user.id)
db.add(entry)
db.flush()
fx.ShadowRecorder(db, uuid_).effect("ledger_topup", "topup", entry)
db.commit()
reg.begin(db, internal, ri.ApprovalIntent(pending_source_instance_id="i", pending_local_id=43, kind="renew",
                                          target_username="ghost", amount=1, package_id=package.id), approval_mode="auto")
db.close()
code, text = run("recent", "5")
lines = text.splitlines()
check("one approval: id, kind, shape, mode, state, amount, username",
      (code, lines[0].split()[:1], [w in lines[0] for w in ("topup", "manual", "mutating", "25,000", "ali")]),
      (0, ["#42"], [True] * 5))
check("its effects: 1 of 2 recorded, the missing one named",
      ("effects 1/2 (required 1/2)" in lines[1], "MISSING: wallet_credit_source_created:credit:receipt_topup" in lines[1]), (True, True))
check("a pending that could not be registered is listed separately, with its reason",
      ("not registered at all" in text, "#43" in text and "registration/target_user_not_found" in text), (True, True))
check("no credential-like column is ever printed", [w for w in ("password", "private", "uuid") if w in text.lower()], [])

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for label in failures:
        print(f"  - {label}")
    sys.exit(1)
print("all checks passed")
