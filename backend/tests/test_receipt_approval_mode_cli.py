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

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for label in failures:
        print(f"  - {label}")
    sys.exit(1)
print("all checks passed")
