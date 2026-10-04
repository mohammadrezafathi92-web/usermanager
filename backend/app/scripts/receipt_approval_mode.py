"""Registration mode of receipt approvals (Receipt Void design 5.5, P3).

  status  - requested and effective mode, key readiness, and what has been
            recorded so far: approvals by state, shadow errors by code
            (read-only)
  shadow  - off -> shadow: every approval the bots make is registered and
            its effects recorded for comparison. Nothing is gated.
  off     - shadow -> off: nothing is registered any more. What was
            recorded stays.
  recent  - the last approvals, newest first, one per line: what it was,
            for whom, how it was approved, its state, and how many of the
            expected effects were recorded (with the missing ones named).
            `recent 30` shows thirty. (read-only)

'required' cannot be set from here (it needs the joint activation of a
later phase).

Run:  docker compose exec backend python -m app.scripts.receipt_approval_mode status
"""
from __future__ import annotations

import sys

from sqlalchemy import func, select

from .. import models_receipt_void as rv
from ..database import SessionLocal, engine
from ..services import receipt_approval_runtime as runtime
from ..services import receipt_void_schema


def show_recent(db, limit: int) -> int:
    approvals, expected, effects = rv.receipt_approvals, rv.receipt_approval_expected_effects, rv.receipt_approval_effects
    shadow = rv.receipt_approval_shadow_events
    rows = db.execute(select(approvals).order_by(approvals.c.id.desc()).limit(limit)).mappings().all()
    if not rows:
        print("no approvals recorded yet")
        return 0
    for row in rows:
        uuid_ = row["approval_uuid"]
        manifest = db.execute(select(expected.c.effect_type, expected.c.effect_key, expected.c.requirement)
                              .where(expected.c.approval_uuid == uuid_)).all()
        written = {(t, k) for t, k in db.execute(select(effects.c.effect_type, effects.c.effect_key)
                                                 .where(effects.c.approval_uuid == uuid_))}
        required = [(t, k) for t, k, requirement in manifest if requirement == "required"]
        missing = [f"{t}:{k}" for t, k in required if (t, k) not in written]
        errors = [code for (code,) in db.execute(select(shadow.c.error_code).where(shadow.c.approval_uuid == uuid_))]
        when = row["created_at"].strftime("%m-%d %H:%M") if row["created_at"] else "?"
        print(f"#{row['pending_local_id']:<6} {when}  {row['kind']:<6} {row['target_shape']:<14} {row['approval_mode']:<6} "
              f"{row['state']:<10} {row['amount_snapshot']:>10,}  {row['target_username_snapshot']}")
        print(f"        effects {len(written)}/{len(manifest)} (required {len(required) - len(missing)}/{len(required)})"
              + (f"  MISSING: {', '.join(missing)}" if missing else "")
              + (f"  SHADOW ERRORS: {', '.join(errors)}" if errors else ""))
    orphans = db.execute(select(shadow.c.pending_local_id, shadow.c.stage, shadow.c.error_code, shadow.c.created_at)
                         .where(shadow.c.approval_uuid.is_(None)).order_by(shadow.c.id.desc()).limit(limit)).all()
    if orphans:
        print("\nnot registered at all (the approval itself still went through):")
        for pending_id, stage, code, created in orphans:
            print(f"#{pending_id:<6} {created.strftime('%m-%d %H:%M') if created else '?'}  {stage}/{code}")
    return 0


def main(argv: list[str]) -> int:
    action = argv[1] if len(argv) > 1 else "status"
    if action not in ("status", "shadow", "off", "recent"):
        print(__doc__)
        return 2
    state = receipt_void_schema.bootstrap(engine, SessionLocal)
    if not state.get("ready"):
        print("receipt void schema is NOT ready:", "; ".join(state.get("problems") or []))
        return 1
    db = SessionLocal()
    try:
        if action == "recent":
            limit = int(argv[2]) if len(argv) > 2 and argv[2].isdigit() else 15
            return show_recent(db, limit)
        if action != "status":
            try:
                runtime.set_requested_mode(db, registration_mode=action,
                                           auto_rate_limit_mode="shadow" if action == "shadow" else None)
                db.commit()
            except runtime.ModeChangeRejected as exc:
                db.rollback()
                print("refused:", exc.code)
                return 1
        info = runtime.describe(db)
        print(f"registration   requested={info['requested_registration_mode']}  effective={info['effective_registration_mode']}")
        print(f"auto limit     requested={info['requested_auto_rate_limit_mode']}  effective={info['effective_auto_rate_limit_mode']}")
        print(f"key identity   ready={info['key_identity_ready']}  failure={info['key_identity_failure']}")
        approvals, shadow, rate = rv.receipt_approvals, rv.receipt_approval_shadow_events, rv.auto_approval_rate_events
        for title, column, table in (("approvals by state", approvals.c.state, approvals),
                                     ("shadow errors by stage/code", func.concat(shadow.c.stage, "/", shadow.c.error_code), shadow),
                                     ("auto decisions", rate.c.outcome, rate)):
            rows = db.execute(select(column, func.count()).select_from(table).group_by(column).order_by(column)).all()
            print(f"{title}:", ", ".join(f"{name}={count}" for name, count in rows) or "none")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
