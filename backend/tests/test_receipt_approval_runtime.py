"""Receipt Void phase P3 (first batch) - requested vs effective mode of
receipt-approval registration, the operator endpoints, shadow events and
the canonical hash (design 5.5, 5.6, section 18).
Run:  python3 backend/tests/test_receipt_approval_runtime.py
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import models, models_receipt_void as rv
from app.routers import receipt_approvals as router
from app.services import receipt_approval_runtime as runtime
from app.services import receipt_void_schema

failures: list[str] = []
T = rv.receipt_approval_runtime_state


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def rejected(**kwargs):
    try:
        runtime.set_requested_mode(db, **kwargs)
    except runtime.ModeChangeRejected as exc:
        db.rollback()
        return exc.code
    db.commit()
    return None


engine = create_engine(f"sqlite:///{tempfile.mkdtemp()}/t.db")
models.Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)
db = Session()

print("--- schema not ready ---")
check("no state, everything off", (runtime.read_state(db), runtime.describe(db)["effective_registration_mode"],
                                   runtime.describe(db)["schema_ready"]), (None, "off", False))
try:
    router.put_runtime_mode(router.RuntimeModeUpdate(registration_mode="shadow"), db=db, _confirm=None)
    status = None
except HTTPException as exc:
    status = (exc.status_code, exc.detail)
check("a mode change is refused with 503", status, (503, "receipt_void_schema_not_ready"))

check("bootstrap", receipt_void_schema.bootstrap(engine, Session)["ready"], True)

print("--- the derivation table of 5.5 ---")
table = [
    ("off", True, "off", None), ("off", False, "off", None),
    ("shadow", True, "shadow", None), ("shadow", False, "shadow", "key_identity_not_ready"),
    ("required", True, "required", None), ("required", False, "blocked", None),
]
check("requested x key readiness -> effective, shadow error",
      [(runtime.effective_registration_mode({"registration_mode": m, "key_identity_ready": r}),
        runtime.shadow_error({"registration_mode": m, "key_identity_ready": r})) for m, r, _e, _s in table],
      [(e, s) for _m, _r, e, s in table])
rate = lambda reg, ready, limit: runtime.effective_auto_rate_limit_mode(
    {"registration_mode": reg, "key_identity_ready": ready, "auto_rate_limit_mode": limit})
check("auto rate limit: enforced only under an effective 'required'; nothing under 'off'",
      (rate("required", True, "enforced"), rate("required", False, "enforced"), rate("shadow", True, "shadow"),
       rate("off", True, "shadow")), ("enforced", "off", "shadow", "off"))

print("--- changing the requested mode ---")
start = runtime.describe(db)
check("starts off; keys ready after bootstrap", (start["requested_registration_mode"], start["key_identity_ready"]), ("off", True))
check("required is never reachable here", rejected(registration_mode="required"), "use_joint_activation")
check("unknown values", (rejected(registration_mode="x"), rejected(auto_rate_limit_mode="x")),
      ("invalid_registration_mode", "invalid_auto_rate_limit_mode"))
check("enforced without required", rejected(auto_rate_limit_mode="enforced"), "enforced_requires_required")
check("rate limit shadow while registration is off", rejected(auto_rate_limit_mode="shadow"), "rate_limit_requires_registration")
check("off -> shadow", (rejected(registration_mode="shadow"), runtime.describe(db)["effective_registration_mode"]), (None, "shadow"))
check("rate limit shadow now allowed", (rejected(auto_rate_limit_mode="shadow"),
                                        runtime.describe(db)["effective_auto_rate_limit_mode"]), (None, "shadow"))
version = db.execute(select(T.c.version)).scalar()
check("each change bumps the row version (readiness check 1 + two mode changes)", version, 3)
check("the schema is still 'ready' after a restart in shadow (seed validation no longer pins the modes)",
      receipt_void_schema.bootstrap(engine, Session)["ready"], True)
check("shadow -> off also turns the limiter off",
      (rejected(registration_mode="off"), runtime.describe(db)["requested_auto_rate_limit_mode"]), (None, "off"))
db.execute(T.update().values(loyalty_timing_mode="payment_event", registration_mode="required", completeness_generation=1))
db.commit()
check("leaving 'required' is not this endpoint's job", rejected(registration_mode="shadow"), "use_deactivate")
check("...and a pinned column that moved makes the schema NOT ready", receipt_void_schema.bootstrap(engine, Session)["ready"], False)
db.execute(T.update().values(loyalty_timing_mode="legacy_activation", registration_mode="off", completeness_generation=0))
db.commit()
receipt_void_schema.bootstrap(engine, Session)

print("--- endpoints ---")
body = router.put_runtime_mode(router.RuntimeModeUpdate(registration_mode="shadow"), db=db, _confirm=None)
check("PUT returns the description", (body["requested_registration_mode"], body["effective_registration_mode"]), ("shadow", "shadow"))
try:
    router.put_runtime_mode(router.RuntimeModeUpdate(registration_mode="required"), db=db, _confirm=None)
    status = None
except HTTPException as exc:
    status = (exc.status_code, exc.detail)
check("PUT required -> 409 use_joint_activation", status, (409, "use_joint_activation"))
db.execute(T.update().values(key_identity_ready=False, key_identity_failure="duplicate_uuid"))
db.commit()
body = router.get_runtime_mode(db=db)
check("GET shows a readiness failure", (body["key_identity_ready"], body["key_identity_failure"], runtime.shadow_error(runtime.read_state(db))),
      (False, "duplicate_uuid", "key_identity_not_ready"))
router.engine, router.SessionLocal = engine, Session
body = router.recheck_runtime_mode(db=db)
check("recheck recomputes readiness", (body["key_identity_ready"], body["key_identity_failure"]), (True, None))
from app.deps import require_superadmin, require_confirm_password
deps = {d.dependency for d in router.router.dependencies}
put_route = next(r for r in router.router.routes if "PUT" in r.methods)
check("router is superadmin-only and PUT also needs the password",
      (require_superadmin in deps, require_confirm_password in {d.call for d in put_route.dependant.dependencies}), (True, True))

print("--- shadow events ---")
E = rv.receipt_approval_shadow_events
runtime.record_shadow_event(db, pending_source_instance_id="inst-1", pending_local_id=7, stage="registration",
                            error_code="key_identity_not_ready")
runtime.record_shadow_event(db, pending_source_instance_id="inst-1", pending_local_id=7, stage="bogus", error_code="x")
db.commit()
rows = db.execute(select(E.c.stage, E.c.error_code, E.c.pending_local_id)).all()
check("a valid event is stored; an invalid one is swallowed without breaking the transaction",
      [tuple(r) for r in rows], [("registration", "key_identity_not_ready", 7)])
check("the table has no username/amount column", sorted(c.name for c in E.columns),
      ["approval_uuid", "created_at", "error_code", "id", "pending_local_id", "pending_source_instance_id", "stage"])

print("--- canonical JSON ---")
check("key order and whitespace do not matter", runtime.sha256_hex({"b": 1, "a": [1, {"d": None, "c": "x"}]}),
      runtime.sha256_hex({"a": [1, {"c": "x", "d": None}], "b": 1}))
check("canonical form", runtime.canonical_json({"b": 1, "a": "پ"}), '{"a":"پ","b":1}')
try:
    runtime.canonical_json({"a": 1.5})
    floats = "accepted"
except TypeError:
    floats = "rejected"
check("floats are rejected", floats, "rejected")

db.close()
print()
if failures:
    print(f"{len(failures)} FAILED:")
    for label in failures:
        print(f"  - {label}")
    sys.exit(1)
print("all checks passed")
