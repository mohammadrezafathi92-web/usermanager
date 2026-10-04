"""Registration mode of receipt approvals (Receipt Void design 5.5, P3).

  status  - requested and effective mode, key readiness, and what has been
            recorded so far: approvals by state, shadow errors by code
            (read-only)
  shadow  - off -> shadow: every approval the bots make is registered and
            its effects recorded for comparison. Nothing is gated.
  off     - shadow -> off: nothing is registered any more. What was
            recorded stays.

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


def main(argv: list[str]) -> int:
    action = argv[1] if len(argv) > 1 else "status"
    if action not in ("status", "shadow", "off"):
        print(__doc__)
        return 2
    state = receipt_void_schema.bootstrap(engine, SessionLocal)
    if not state.get("ready"):
        print("receipt void schema is NOT ready:", "; ".join(state.get("problems") or []))
        return 1
    db = SessionLocal()
    try:
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
