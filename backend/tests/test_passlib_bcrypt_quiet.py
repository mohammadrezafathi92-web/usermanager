"""Password hashing still works, and passlib's harmless "error reading
bcrypt version" traceback no longer lands in the log.
Run:  python3 backend/tests/test_passlib_bcrypt_quiet.py
"""
from __future__ import annotations

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

records: list[logging.LogRecord] = []


class Capture(logging.Handler):
    def emit(self, record):
        records.append(record)


logging.getLogger().addHandler(Capture())
logging.getLogger().setLevel(logging.DEBUG)

from app.security import hash_password, verify_password

failures: list[str] = []


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


hashed = hash_password("correct horse")
check("a hash is produced and verifies", (hashed.startswith("$2"), verify_password("correct horse", hashed)), (True, True))
check("a wrong password does not verify", verify_password("wrong", hashed), False)
noise = [r for r in records if r.name.startswith("passlib") and (r.exc_info or "bcrypt version" in r.getMessage())]
check("no passlib traceback / version warning was logged", [r.getMessage() for r in noise], [])

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for label in failures:
        print(f"  - {label}")
    sys.exit(1)
print("all checks passed")
