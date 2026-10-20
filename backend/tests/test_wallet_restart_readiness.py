"""Real startup after P9/P9a/rollback; no activation or money is replayed.

SQLite and a newly allocated MariaDB scratch database. No node/network calls.
"""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
import _mariadb_scratch as scratch
from app import models, models_provisioning as mp, models_receipt_void as rv
from app.services import loyalty_activation, provisioning_readiness, provisioning_schema
from app.services import receipt_void_schema as schema, wallet_cutover, wallet_reconcile


def run(engine):
    models.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    assert provisioning_schema.bootstrap(engine, Session)["ready"]
    assert schema.bootstrap(engine, Session)["ready"]
    with Session() as db:
        db.add(models.PanelSettings(id=1))
        admin = models.AdminUser(username="root", hashed_password="unused", is_superadmin=True)
        db.add(admin)
        db.add(models.User(username="wallet-owner", balance=123))
        for mode in db.query(mp.ProvisioningTypeMode):
            mode.mode = "durable"
        db.execute(rv.receipt_approval_runtime_state.update().values(registration_mode="shadow"))
        db.commit()
        actor = admin.id

    def snapshot():
        with engine.connect() as conn:
            wallet = dict(conn.execute(select(rv.wallet_runtime_state)).mappings().one())
            approval = conn.execute(select(rv.receipt_approval_runtime_state)).mappings().one()
            fields = ("registration_mode", "loyalty_timing_mode", "auto_rate_limit_mode",
                      "completeness_generation", "activated_at")
            lots = [dict(r) for r in conn.execute(select(rv.wallet_lots)).mappings()]
            return wallet, {f: approval[f] for f in fields}, lots

    def restart(label):
        before = snapshot()
        result = schema.bootstrap(engine, Session)
        assert result["ready"], (label, result["problems"])
        assert snapshot() == before, label + ": runtime and money must not change"
        with Session() as db:
            assert wallet_reconcile.check(db)["ok"], label
        print("PASS", engine.dialect.name, label)

    ready = patch.object(provisioning_readiness, "p6_ready", return_value=(True, []))
    with Session() as db, ready:
        assert wallet_cutover.cutover(db)["baseline_lots"] == 1
    restart("post-P9 restart preserves epoch, baseline and balance")
    with Session() as db:
        from app.services import product_decisions
        product_decisions.confirm(db, actor, version=product_decisions.VERSION, content_hash=product_decisions.CONTENT_HASH)
    with Session() as db, ready:
        assert loyalty_activation.activate(db, actor)["activated"]
    restart("post-P9a restart preserves required/payment_event and generation")
    with Session() as db:
        loyalty_activation.deactivate(db)
    restart("deactivated P9a retains historical generation")
    with Session() as db:
        wallet_cutover.rollback(db)
    restart("wallet rollback retains epoch and baseline rows")

    # Structurally valid CHECK-constrained rows can still be invalid together.
    with engine.begin() as conn:
        conn.execute(rv.wallet_runtime_state.update().values(phase="fencing", epoch=1))
    assert not schema.bootstrap(engine, Session)["ready"], "committed fencing must fail closed"
    with engine.begin() as conn:
        conn.execute(rv.wallet_runtime_state.update().values(phase="enforced", epoch=0))
    assert not schema.bootstrap(engine, Session)["ready"], "enforced epoch zero must fail closed"
    with engine.begin() as conn:
        conn.execute(rv.wallet_runtime_state.update().values(phase="normal", epoch=3))
        conn.execute(rv.receipt_approval_runtime_state.update().values(
            registration_mode="required", loyalty_timing_mode="payment_event"))
    assert not schema.bootstrap(engine, Session)["ready"], "required with normal wallet must fail closed"
    with engine.begin() as conn:
        conn.execute(rv.wallet_runtime_state.update().values(phase="enforced", epoch=2))
        conn.execute(rv.receipt_approval_runtime_state.update().values(activated_at=None))
    assert not schema.bootstrap(engine, Session)["ready"], "generation without activation timestamp must fail closed"
    print("PASS", engine.dialect.name, "invalid states remain fail-closed")


sqlite = create_engine("sqlite://")
try:
    run(sqlite)
finally:
    sqlite.dispose()
url = os.environ.get("MARIADB_TEST_URL")
if url:
    maria = scratch.claim(url)
    try:
        run(maria)
    finally:
        scratch.release(maria)
elif os.environ.get("CI"):
    raise SystemExit("CI requires real MariaDB")
else:
    print("SKIP MariaDB: MARIADB_TEST_URL not set")
print("all checks passed")
