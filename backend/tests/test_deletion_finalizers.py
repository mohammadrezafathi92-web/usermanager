"""P5 DB-only finalizers, rollback and audit retention on SQLite/MariaDB."""
import ast
import contextlib
import os
from pathlib import Path
import sys
import tempfile
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DATABASE_URL", "sqlite://")
import _mariadb_scratch as scratch
import _no_network
_no_network.install([os.environ.get("MARIADB_TEST_URL", "").strip()])
from fastapi import HTTPException
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from app import models, models_receipt_void as rv
from app.services import receipt_void_schema, user_ops, wallet_accounts


def scenario(engine):
    name = engine.dialect.name
    models.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    assert receipt_void_schema.bootstrap(engine, Session)["ready"]
    with Session() as db:
        user = wallet_accounts.create_user_with_wallet(db, username="delete-me")
        other = wallet_accounts.create_user_with_wallet(db, username="keep-me")
        db.flush()
        other.referred_by_id = user.id
        node = models.Node(name="test-node", type=models.NodeType.mikrotik)
        db.add(node)
        db.flush()
        purchases = [models.Purchase(user_id=user.id, quota_bytes=1000),
                     models.Purchase(user_id=user.id, quota_bytes=2000)]
        db.add_all(purchases)
        db.flush()
        connections = [models.Connection(user_id=user.id, node_id=node.id, purchase_id=p.id,
                                        type=models.ConnectionType.pptp, ppp_username=f"peer{i}")
                       for i, p in enumerate(purchases)]
        db.add_all(connections)
        db.flush()
        logs = [models.UsageLog(user_id=user.id, connection_id=c.id) for c in connections]
        limits = [models.RadiusLimitEventLog(user_id=user.id, connection_id=c.id, username="snapshot")
                  for c in connections]
        sessions = [models.RadiusActiveSession(connection_id=c.id, session_id=f"s{i}")
                    for i, c in enumerate(connections)]
        ledger = models.LedgerEntry(kind="sale", amount=100, user_id=user.id, purchase_id=purchases[0].id)
        code = models.DiscountCode(code="DELTEST")
        db.add_all(logs + limits + sessions + [ledger, code])
        db.flush()
        redemption = models.DiscountCodeRedemption(code_id=code.id, user_id=user.id, username=user.username)
        db.add(redemption)
        db.commit()
        uid, pid, cid = user.id, purchases[0].id, connections[0].id
        keep_pid, keep_cid = purchases[1].id, connections[1].id
        log_ids, limit_ids = [r.id for r in logs], [r.id for r in limits]
        ledger_id, redemption_id = ledger.id, redemption.id
        account_id = db.execute(select(rv.wallet_accounts.c.id).where(rv.wallet_accounts.c.user_id == uid)).scalar_one()
        for phase in ("fencing", "enforced"):
            db.execute(rv.wallet_runtime_state.update().values(phase=phase))
            db.commit()
            try:
                user_ops.finalize_purchase_deletion_after_deprovision(db, purchases[0])
                raise AssertionError("unsupported wallet generation accepted")
            except HTTPException as exc:
                assert exc.status_code == 503
            db.rollback()
            assert db.get(models.Purchase, pid) is not None
        db.execute(rv.wallet_runtime_state.update().values(phase="normal"))
        db.commit()
        print("PASS", name, "purchase finalizer refuses unsupported wallet generations")

        with patch.object(user_ops, "deprovision_connection", side_effect=AssertionError("remote in finalizer")):
            with patch.object(db, "commit", side_effect=AssertionError("helper committed")), patch.object(
                db, "rollback", side_effect=AssertionError("helper rolled back")
            ):
                user_ops.finalize_purchase_deletion_after_deprovision(db, purchases[0])
                db.flush()
            assert db.get(models.Purchase, pid) is None
            assert db.get(models.Connection, cid) is None
            assert db.get(models.Purchase, keep_pid) is not None
            assert db.get(models.Connection, keep_cid) is not None
            db.rollback()
            assert db.get(models.Purchase, pid) is not None
            assert db.get(models.Connection, cid) is not None
            assert db.get(models.LedgerEntry, ledger_id).purchase_id == pid
            assert db.get(models.UsageLog, log_ids[0]).connection_id == cid
            print("PASS", name, "purchase finalizer rollback is atomic, DB-only and service-scoped")
            user_ops.finalize_purchase_deletion_after_deprovision(db, db.get(models.Purchase, pid))
            db.commit()
            db.expire_all()
            assert db.get(models.User, uid) is not None
            assert db.get(models.Purchase, keep_pid) is not None
            assert db.get(models.Connection, keep_cid) is not None
            assert db.get(models.LedgerEntry, ledger_id).purchase_id is None
            assert db.get(models.UsageLog, log_ids[0]).connection_id is None
            assert db.get(models.UsageLog, log_ids[1]).connection_id == keep_cid
            assert db.get(models.RadiusLimitEventLog, limit_ids[0]).connection_id is None
            assert db.query(models.RadiusActiveSession).count() == 1
            print("PASS", name, "purchase audit retained; other service/session untouched")

        # A real valid hold must block before even one legacy remote call.
        opid = db.execute(rv.wallet_operations.insert().values(operation_type="wallet_debit",
            business_key=str(uuid.uuid4()), request_hash="a" * 64, tenant_scope_key="system",
            actor_kind="system", state="completed", epoch_at_start=0, result_snapshot="{}"
        )).inserted_primary_key[0]
        holdid = db.execute(rv.wallet_debit_events.insert().values(wallet_account_id=account_id,
            kind="sale", amount=10, hold_ref=str(uuid.uuid4()), state="held", wallet_operation_id=opid
        )).inserted_primary_key[0]
        db.commit()
        with patch.object(user_ops, "deprovision_connection") as remote:
            try:
                user_ops.delete_user_cascade(db, user)
                raise AssertionError("held wallet deletion allowed")
            except HTTPException as exc:
                assert (exc.status_code, exc.detail) == (409, "wallet_account_has_hold")
            assert remote.call_count == 0
            db.rollback()
        assert db.get(models.User, uid) is not None
        db.execute(rv.wallet_debit_events.delete().where(rv.wallet_debit_events.c.id == holdid))
        # Historical loyalty rows are retained, only their live user link
        # is detached. The payment and ledger evidence remains immutable.
        spend = models.LedgerEntry(kind="admin_credit_spend", amount=100, user_id=uid)
        db.add(spend)
        db.flush()
        evidence_id = db.execute(rv.sale_payment_evidence.insert().values(
            sale_ledger_entry_id=ledger_id, tenant_scope_key="system", evidence_kind="admin_credit_spend",
            credit_spend_ledger_entry_id=spend.id, user_id_snapshot=uid, sale_amount=100, captured_amount=100,
        )).inserted_primary_key[0]
        epoch_id = db.execute(rv.loyalty_policy_epochs.insert().values(
            threshold=1, credit_amount=5, quota_bytes=0, generation=1, policy_version=1,
        )).inserted_primary_key[0]
        event_id = db.execute(rv.loyalty_purchase_events.insert().values(
            user_id=uid, user_id_snapshot=uid, payment_evidence_id=evidence_id, ledger_entry_id=ledger_id,
            eligibility_reason="admin_credit_paid", completeness_generation=1, state="active",
        )).inserted_primary_key[0]
        reward_id = db.execute(rv.loyalty_reward_events.insert().values(
            user_id=uid, user_id_snapshot=uid, epoch_id=epoch_id, slot=1, reearn_generation=1,
            required_active_count=1, triggering_purchase_event_id=event_id,
            causation_type="admin_credit_spend", causation_credit_spend_ledger_entry_id=spend.id,
            credit_amount=5, quota_bytes=0, state="active",
        )).inserted_primary_key[0]
        anchor_id = db.execute(rv.loyalty_user_epoch_anchors.insert().values(
            user_id=uid, user_id_snapshot=uid, epoch_id=epoch_id, starts_after_active_count=0,
        )).inserted_primary_key[0]
        db.commit()
        print("PASS", name, "open hold refuses deletion before remote work")

        with patch.object(user_ops, "deprovision_connection", side_effect=AssertionError("remote in finalizer")):
            with patch.object(db, "commit", side_effect=AssertionError("helper committed")), patch.object(
                db, "rollback", side_effect=AssertionError("helper rolled back")
            ):
                user_ops.finalize_user_deletion_after_deprovision(db, user)
                db.flush()
            db.rollback()
            assert db.get(models.User, uid) is not None
            assert db.get(models.Connection, keep_cid) is not None
            assert db.get(models.DiscountCodeRedemption, redemption_id).user_id == uid
            assert db.get(models.User, other.id).referred_by_id == uid
            assert db.execute(select(rv.wallet_accounts.c.user_id).where(rv.wallet_accounts.c.id == account_id)).scalar_one() == uid
            assert db.execute(select(rv.loyalty_reward_events.c.user_id).where(rv.loyalty_reward_events.c.id == reward_id)).scalar_one() == uid
            print("PASS", name, "user rollback restores user, connections, wallet and nullable references")

        with patch.object(user_ops, "deprovision_connection", side_effect=RuntimeError("node offline")):
            try:
                user_ops.delete_user_cascade(db, user)
                raise AssertionError("remote failure did not stop wrapper")
            except RuntimeError:
                db.rollback()
        assert db.get(models.User, uid) is not None
        assert db.get(models.Connection, keep_cid) is not None
        print("PASS", name, "remote failure leaves DB rows and wallet attached")

        with patch.object(user_ops, "deprovision_connection") as remote:
            user_ops.delete_user_cascade(db, user)
            assert remote.call_count == 1
        db.expire_all()
        assert db.get(models.User, uid) is None
        assert db.get(models.Connection, keep_cid) is None
        assert db.get(models.Purchase, keep_pid) is None
        assert db.get(models.DiscountCodeRedemption, redemption_id).username == "delete-me"
        assert db.get(models.DiscountCodeRedemption, redemption_id).user_id is None
        assert db.get(models.User, other.id).referred_by_id is None
        assert db.get(models.LedgerEntry, ledger_id).amount == 100
        assert db.query(models.RadiusActiveSession).count() == 0
        assert db.query(models.UsageLog).count() == 0
        assert all(db.get(models.RadiusLimitEventLog, ident).user_id is None for ident in limit_ids)
        assert db.execute(select(rv.wallet_accounts.c.user_id).where(rv.wallet_accounts.c.id == account_id)).scalar_one() is None
        for table, ident in ((rv.loyalty_purchase_events, event_id), (rv.loyalty_reward_events, reward_id),
                             (rv.loyalty_user_epoch_anchors, anchor_id)):
            retained = db.execute(select(table).where(table.c.id == ident)).mappings().one()
            assert retained["user_id"] is None and retained["user_id_snapshot"] == uid
        assert db.execute(select(rv.loyalty_reward_events.c.state).where(rv.loyalty_reward_events.c.id == reward_id)).scalar_one() == "active"
        print("PASS", name, "legacy wrapper deprovisions once; wallet, ledger and audit survive")


app = Path(__file__).resolve().parents[1] / "app"
user_deletes = []
for path in app.rglob("*.py"):
    tree = ast.parse(path.read_text())
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for call in ast.walk(function):
            if not isinstance(call, ast.Call):
                continue
            if function.name.startswith("finalize_") and function.name.endswith("_after_deprovision"):
                called = call.func.attr if isinstance(call.func, ast.Attribute) else getattr(call.func, "id", "")
                assert called not in ("commit", "rollback", "deprovision_connection", "delete_user_cascade")
            if not isinstance(call.func, ast.Attribute):
                continue
            if call.func.attr == "delete" and call.args and isinstance(call.args[0], ast.Name) and call.args[0].id == "user":
                user_deletes.append((str(path.relative_to(app)), function.name))
assert user_deletes == [("services/user_ops.py", "finalize_user_deletion_after_deprovision")], user_deletes
print("PASS AST: User delete only in DB finalizer; no commit/rollback/remote wrapper in finalizers")

with contextlib.ExitStack() as cleanup:
    folder = cleanup.enter_context(tempfile.TemporaryDirectory(prefix="deletion_finalizers_"))
    sqlite = create_engine(f"sqlite:///{folder}/test.db")
    cleanup.callback(sqlite.dispose)
    @event.listens_for(sqlite, "connect")
    def foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")
    scenario(sqlite)
    url = os.environ.get("MARIADB_TEST_URL", "").strip()
    if url:
        maria = scratch.claim(url)
        cleanup.callback(scratch.release, maria)
        scenario(maria)
    elif os.environ.get("CI"):
        raise AssertionError("CI requires real MariaDB")
    else:
        print("SKIP local MariaDB: mandatory in CI")
assert _no_network.attempts == [], _no_network.attempts
