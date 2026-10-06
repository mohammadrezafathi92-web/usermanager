"""The fail-closed guard of approval-carrying endpoints must not keep
SQLite's single writer lock while the endpoint talks to a router.

`receipt_approval_runtime.guard_approval_mutation` decides "blocked or
not" before any lookup. On SQLite the whole database has ONE writer: if the
guard left its transaction open, a receipt-approved renewal of an expired
customer would hold that writer through `reconcile_user_connections`
(MikroTik / Xray calls), and RADIUS accounting, the quota poller and every
bot write would wait on it - or fail with "database is locked".

Driven through the real HTTP endpoint. The router call is replaced by a
probe that asks, from a second connection, whether the writer is free at
that exact moment. No node is contacted.
"""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app import models, models_receipt_void as rv
from app.database import get_db
from app.routers import bot as bot_router
from app.services import bot_auth, receipt_void_schema, user_ops
from app.services import receipt_approval_runtime as runtime

failures: list[str] = []


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


with tempfile.TemporaryDirectory(prefix="guard_lock_") as folder:
    path = f"{folder}/t.db"
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=15000")
        cursor.close()

    models.Base.metadata.create_all(engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)        # the application's settings
    check("schema ready", receipt_void_schema.bootstrap(engine, Session)["ready"], True)

    past = dt.datetime.utcnow() - dt.timedelta(days=3)
    setup = Session()
    key = models.ApiKey(key="guard-lock-key", label="remote bot", key_type=bot_auth.KeyType.REMOTE_SHARED_BOT)
    setup.add_all([models.PanelSettings(id=1), key] + [
        models.User(username=name, telegram_id=500 + i, balance=0, status=models.UserStatus.expired, expire_at=past)
        for i, name in enumerate(("expired_plain", "expired_approved", "expired_in_flight", "expired_blocked"))])
    setup.commit()
    runtime.set_requested_mode(setup, registration_mode="shadow")
    setup.commit()
    headers = {"X-API-Key": key.key}
    setup.close()

    def session_per_request():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    app = FastAPI()
    app.include_router(bot_router.router)
    app.dependency_overrides[get_db] = session_per_request
    client = TestClient(app)

    probes: list[str] = []

    def writer_is_free(_db, _user):
        """Stands where the router calls are: can another connection write now?"""
        other = sqlite3.connect(path, timeout=0.3, isolation_level=None)
        try:
            other.execute("BEGIN IMMEDIATE")
            other.execute("ROLLBACK")
            probes.append("free")
        except sqlite3.OperationalError:
            probes.append("locked")
        finally:
            other.close()

    original = user_ops.reconcile_user_connections
    user_ops.reconcile_user_connections = writer_is_free
    try:
        plain = client.post("/api/bot/users/expired_plain/renew", headers=headers,
                            json={"add_gb": 1, "add_days": 30})
        check("control: a renewal WITHOUT an approval reaches the router step with the writer free",
              (plain.status_code, probes), (200, ["free"]))

        probes.clear()
        approved = client.post("/api/bot/users/expired_approved/renew", headers=headers,
                               json={"add_gb": 1, "add_days": 30, "approval_uuid": str(uuid.uuid4())})
        check("a renewal WITH an approval also reaches the router step with the writer free",
              (approved.status_code, probes), (200, ["free"]))

        # THE TRADE-OFF, stated as a test. Because the writer is not held,
        # a request the guard already let through finishes even if the mode
        # becomes blocked while it is at the router step. (On MariaDB the
        # shared row lock makes the mode change wait instead.) Every request
        # that STARTS after the change is refused.
        def block_now(_db=None, _user=None):
            switch = Session()
            switch.execute(rv.receipt_approval_runtime_state.update().where(
                rv.receipt_approval_runtime_state.c.id == 1).values(
                    registration_mode="required", loyalty_timing_mode="payment_event", completeness_generation=1))
            switch.commit()
            switch.close()

        user_ops.reconcile_user_connections = block_now
        in_flight = client.post("/api/bot/users/expired_in_flight/renew", headers=headers,
                                json={"add_gb": 1, "add_days": 30, "approval_uuid": str(uuid.uuid4())})
        after = Session()
        status = after.query(models.User).filter_by(username="expired_in_flight").one().status
        after.close()
        check("SQLite trade-off: a renewal already past the guard completes when blocked arrives mid-way",
              (in_flight.status_code, status), (200, models.UserStatus.active))
        user_ops.reconcile_user_connections = writer_is_free

        # The guard still decides first: once required is requested without
        # execution-token support the effective mode is blocked.
        probes.clear()
        blocked = client.post("/api/bot/users/expired_blocked/renew", headers=headers,
                              json={"add_gb": 1, "add_days": 30, "approval_uuid": str(uuid.uuid4())})
        check("blocked still wins before the renewal and before any router step",
              (blocked.status_code, blocked.json().get("detail"), probes), (503, "receipt_approval_blocked", []))
        after = Session()
        status = after.query(models.User).filter_by(username="expired_blocked").one().status
        after.close()
        check("the blocked renewal changed nothing", status, models.UserStatus.expired)

        # ...and it leaves no open transaction behind after refusing.
        writer_is_free(None, None)
        check("a refused request leaves the writer free", probes, ["free"])
    finally:
        user_ops.reconcile_user_connections = original
        engine.dispose()

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for label in failures:
        print(f"  - {label}")
    sys.exit(1)
print("all checks passed")
