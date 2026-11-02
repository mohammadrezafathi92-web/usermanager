"""After a durable run the request session must see what was committed.

Run:  python3 backend/tests/test_durable_fresh_view.py
      (also against MariaDB when MARIADB_TEST_URL is set)

Bug (2026-11-03, MariaDB): panel "create user" with a package showed
"error creating user" although the user WAS created. run() commits in its
own sessions; the request session had started a REPEATABLE READ snapshot
before T_final (touching the expired `admin.id` after its rollback), and
expire_all() does not end a snapshot - db.get() returned None and the
response failed validation (500). SQLite in its default mode never holds a
read snapshot, which is why the suite did not see it; here SQLite runs in
WAL with an explicit BEGIN, which reproduces the snapshot.
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("SECRET_KEY", "durable-fresh-view-test-only-0123456789")

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app import models
from app.services import provisioning_durable

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def snapshot_sqlite(path):
    engine = create_engine(f"sqlite:///{path}", connect_args={"isolation_level": None})

    @event.listens_for(engine, "connect")
    def _wal(dbapi, _record):
        dbapi.execute("PRAGMA journal_mode=WAL")

    @event.listens_for(engine, "begin")
    def _begin(conn):
        conn.exec_driver_sql("BEGIN")
    return engine


def scenario(engine, name):
    models.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    admin = models.AdminUser(username=f"root-{name}", hashed_password="x", is_superadmin=True)
    db.add(admin); db.commit()
    db.rollback()                     # what the router does before run()
    actor_id = admin.id               # ... and this reload opens a snapshot
    other = Session()                 # the durable machine's own session
    user = models.User(username=f"created-{name}", owner_admin_id=actor_id)
    other.add(user); other.commit()
    new_id = user.id
    other.close()
    db.expire_all()                   # the old code
    check(f"[{name}] emulation: the old expire_all() still misses the new row", db.get(models.User, new_id), None)
    provisioning_durable.fresh_view(db)
    found = db.get(models.User, new_id)
    check(f"[{name}] fresh_view(): the created user is visible", getattr(found, "username", None), f"created-{name}")
    db.close()


with tempfile.TemporaryDirectory(prefix="fresh-view-") as temp:
    engine = snapshot_sqlite(Path(temp) / "panel.db")
    scenario(engine, "sqlite-wal")
    engine.dispose()

if os.environ.get("MARIADB_TEST_URL"):
    import _mariadb_scratch as scratch
    engine = scratch.claim(os.environ["MARIADB_TEST_URL"])
    try:
        scenario(engine, "mariadb")
    finally:
        scratch.release(engine)
else:
    print("SKIP  MariaDB (MARIADB_TEST_URL not set)")

# Every durable run in a router is followed by fresh_view before any read.
routers = Path(__file__).resolve().parents[1] / "app" / "routers"
for path in sorted(routers.glob("*.py")):
    lines = path.read_text().splitlines()
    for i, line in enumerate(lines):
        if "provisioning_durable.run(" not in line:
            continue
        window = "\n".join(lines[i:i + 4])
        check(f"{path.name}:{i + 1} durable run is followed by fresh_view",
              bool(re.search(r"provisioning_durable\.fresh_view\(db\)", window)), True)

if failures:
    print(f"\n{len(failures)} FAILED")
    sys.exit(1)
print("\nall fresh view checks passed")
