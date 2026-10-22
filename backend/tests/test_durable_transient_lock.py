"""A MariaDB lock conflict inside a durable operation's iteration (deadlock
1213, lock wait 1205, snapshot conflict 1020) is a retry, like a busy lease
- not an error thrown at the customer's request. Anything else still raises.

Seen in CI (test_receipt_void_hardening on MariaDB): a concurrent card
payment's worker iteration lost a deadlock in its precondition reads, the
OperationalError reached the request thread and the purchase call failed
although its operation was intact and resumable.

Run:  python3 backend/tests/test_durable_transient_lock.py
"""
from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from fastapi import HTTPException  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402

from app.services import provisioning_deletion_worker as deletion  # noqa: E402
from app.services import provisioning_durable  # noqa: E402
from app.services import provisioning_operation_worker as operation  # noqa: E402
from app.services import resource_leases  # noqa: E402

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def mariadb_error(code):
    return OperationalError("SELECT ... FOR SHARE", {}, Exception(code, "lock conflict"))


class FakeSession:
    def commit(self):
        pass


@contextmanager
def factory():
    yield FakeSession()


released = []

for module in (operation, deletion):
    name = module.__name__.rsplit(".", 1)[-1]

    def run(raises):
        def tick(*args, **kwargs):
            raise raises
        released.clear()
        with patch.object(module, "reacquire", lambda *a, **k: ["token"]), \
                patch.object(module, "tick", tick), \
                patch.object(module.leases, "release", lambda db, token: released.append(token)):
            return module.resume_one(factory, 7, object(), installation_uuid="u", ownership_epoch=1)

    for code in (1213, 1205, 1020):
        result = run(mariadb_error(code))
        check(f"{name}: MariaDB {code} -> retry like a busy lease", result, {"operation_id": 7, "status": "lease_busy"})
        check(f"{name}: ...and this iteration's leases are released", released, ["token"])
    check(f"{name}: lease_busy is a status the request loop pauses on and retries",
          "lease_busy" in provisioning_durable._PAUSE, True)
    for label, exc in (("another database error", mariadb_error(1146)),
                       ("a refusal", HTTPException(409, "provisioning_lease_scope_invalid")),
                       ("a programming error", KeyError("c"))):
        try:
            run(exc)
            raised = None
        except Exception as err:  # noqa: BLE001
            raised = type(err)
        check(f"{name}: {label} still raises (never hidden as a retry)", raised, type(exc))

check("the classifier ignores non-MariaDB errors", resource_leases.transient_conflict(RuntimeError(1213)), False)

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("all checks passed")
