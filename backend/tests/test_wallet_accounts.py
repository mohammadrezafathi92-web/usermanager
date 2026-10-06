"""P5 account lifecycle on isolated SQLite and mandatory CI MariaDB."""
import ast
import contextlib
import os
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DATABASE_URL", "sqlite://")
import _mariadb_scratch as scratch
import _no_network
_no_network.install([os.environ.get("MARIADB_TEST_URL", "").strip()])

from fastapi import HTTPException
from sqlalchemy import create_engine, event, select, func
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker
from app import models, models_receipt_void as rv
from app.services import receipt_void_schema, user_ops, wallet_accounts

failures = []


def check(label, actual, expected=True):
    if actual == expected:
        print("PASS", label)
    else:
        failures.append(label)
        print("FAIL", label, repr(actual), "!=", repr(expected))


def scenario(engine, name):
    models.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False)
    check(name + " schema ready", receipt_void_schema.bootstrap(engine, Session)["ready"])
    db = Session()
    try:
        admin = models.AdminUser(username="account_admin", hashed_password="x", role="admin")
        other = models.AdminUser(username="account_other", hashed_password="x", role="admin")
        db.add_all([admin, other])
        db.commit()
        seller = models.AdminUser(username="account_seller", hashed_password="x", role="seller", parent_admin_id=admin.id)
        db.add(seller)
        db.commit()

        def create(username, **kw):
            return wallet_accounts.create_user_with_wallet(db, username=username, **kw)

        def account(user_id):
            return dict(db.execute(select(rv.wallet_accounts).where(rv.wallet_accounts.c.user_id == user_id)).mappings().one())

        def count(table):
            return db.execute(select(func.count()).select_from(table)).scalar_one()

        first = create("first", telegram_id=900, owner_admin_id=admin.id)
        second = create("second", telegram_id=900, owner_admin_id=seller.id)
        third = create("third", telegram_id=900, owner_admin_id=other.id)
        private1, private2 = create("private1"), create("private2")
        db.commit()
        a, b, c = account(first.id), account(second.id), account(third.id)
        check(name + " seller shares parent tenant identity", a["customer_identity_id"], b["customer_identity_id"])
        check(name + " another tenant is isolated", a["customer_identity_id"] != c["customer_identity_id"])
        check(name + " every account has fresh lineage", len({a["lineage_key"], b["lineage_key"], c["lineage_key"]}), 3)
        check(name + " Telegram-less identities are private", account(private1.id)["customer_identity_id"] != account(private2.id)["customer_identity_id"])
        identity = db.execute(select(rv.customer_identities).where(rv.customer_identities.c.id == a["customer_identity_id"])).mappings().one()
        check(name + " root scope", identity["tenant_scope_key"], f"admin:{admin.id}")
        check(name + " identity key", identity["identity_key"], "tg:900")

        baseline = count(rv.wallet_accounts), count(rv.customer_identities)
        create("rolled_back", telegram_id=901)
        db.rollback()
        check(name + " rollback removes user", db.query(models.User).filter_by(username="rolled_back").count(), 0)
        check(name + " rollback removes wallet data", (count(rv.wallet_accounts), count(rv.customer_identities)), baseline)

        def crash(_session):
            raise RuntimeError("injected commit failure")

        create("crash", telegram_id=902)
        event.listen(db, "before_commit", crash)
        try:
            db.commit()
        except RuntimeError:
            db.rollback()
        finally:
            event.remove(db, "before_commit", crash)
        check(name + " commit failure leaves no account", count(rv.wallet_accounts), baseline[0])
        check(name + " commit failure leaves no user", db.query(models.User).filter_by(username="crash").count(), 0)

        old_id, old_account = first.id, a["id"]
        event.listen(db, "before_commit", crash)
        try:
            user_ops.delete_user_cascade(db, first)
        except RuntimeError:
            db.rollback()
        finally:
            event.remove(db, "before_commit", crash)
        check(name + " delete rollback keeps user", db.get(models.User, old_id) is not None)
        check(name + " delete rollback keeps live account", account(old_id)["id"], old_account)
        user_ops.delete_user_cascade(db, first)  # no connections: no node I/O
        tombstone = db.execute(select(rv.wallet_accounts).where(rv.wallet_accounts.c.id == old_account)).mappings().one()
        check(name + " account retained", tombstone["user_id"], None)
        check(name + " tombstone stamped", tombstone["tombstoned_at"] is not None)
        check(name + " old user snapshot retained", tombstone["user_id_snapshot"], old_id)
        check(name + " old identity retained", tombstone["customer_identity_id"], a["customer_identity_id"])
        recreated = create("recreated", id=old_id, telegram_id=900, owner_admin_id=admin.id)
        db.commit()
        fresh = account(recreated.id)
        check(name + " explicit reused User ID has fresh account", fresh["id"] != old_account)
        check(name + " explicit reused User ID has fresh lineage", fresh["lineage_key"] != a["lineage_key"])
        check(name + " identity survives deletion", fresh["customer_identity_id"], a["customer_identity_id"])
        try:
            wallet_accounts.tombstone_user_account(db, recreated, account_id=old_account)
            check(name + " stale deletion refuses replacement", False)
        except HTTPException as exc:
            check(name + " stale deletion refuses replacement", (exc.status_code, exc.detail), (409, "wallet_account_changed"))
        db.rollback()
        check(name + " replacement account remains attached", account(old_id)["id"], fresh["id"])

        # In normal phase, deletion of a pre-P5 user does not invent data.
        with patch.object(receipt_void_schema, "is_ready", return_value=False):
            legacy = create("legacy")
            db.commit()
        baseline = count(rv.wallet_accounts)
        user_ops.delete_user_cascade(db, legacy)
        check(name + " old user deleted without invented account", count(rv.wallet_accounts), baseline)

        for phase in ("fencing", "enforced"):
            db.execute(rv.wallet_runtime_state.update().values(phase=phase))
            db.commit()
            try:
                create("refused_" + phase)
                check(name + " creation refused " + phase, False)
            except HTTPException as exc:
                check(name + " creation refused " + phase, (exc.status_code, exc.detail), (503, "wallet_legacy_writer_unavailable"))
            db.rollback()
            with patch.object(user_ops, "deprovision_connection") as remote:
                try:
                    user_ops.delete_user_cascade(db, recreated)
                    check(name + " deletion refused " + phase, False)
                except HTTPException as exc:
                    check(name + " deletion refused " + phase, exc.status_code, 503)
                check(name + " refusal before remote " + phase, remote.call_count, 0)
            db.rollback()
            check(name + " refused delete keeps live account " + phase, account(old_id)["id"], fresh["id"])
        db.execute(rv.wallet_runtime_state.update().values(phase="normal"))
        db.commit()
        check(name + " no financial operations", count(rv.wallet_operations), 0)
        check(name + " no financial lots", count(rv.wallet_lots), 0)
        owner_id = admin.id
        db.rollback()  # no open read snapshot across the thread exercise
        barrier = threading.Barrier(2)

        def concurrent_create(index):
            own = Session()
            try:
                barrier.wait(timeout=10)
                customer = wallet_accounts.create_user_with_wallet(
                    own, username=f"race_{index}", telegram_id=999, owner_admin_id=owner_id)
                own.commit()
                return own.execute(select(rv.wallet_accounts.c.customer_identity_id).where(
                    rv.wallet_accounts.c.user_id == customer.id)).scalar_one()
            finally:
                own.rollback()
                own.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(concurrent_create, index) for index in (1, 2)]
            identities = [future.result(timeout=30) for future in futures]
        check(name + " concurrent creation shares exactly one identity", len(set(identities)), 1)
        check(name + " concurrent creation preserves both users", db.query(models.User).filter(models.User.username.in_(("race_1", "race_2"))).count(), 2)
        if engine.dialect.name in ("mysql", "mariadb"):
            db.rollback()
            loser = Session()
            original_identity = wallet_accounts._identity

            def winner_after_snapshot(session, **kwargs):
                if session is loser:
                    session.execute(select(rv.customer_identities.c.id).where(
                        rv.customer_identities.c.identity_key == "tg:998")).first()
                    winner = Session()
                    try:
                        wallet_accounts.create_user_with_wallet(
                            winner, username="snapshot_winner", telegram_id=998, owner_admin_id=owner_id)
                        winner.commit()
                    finally:
                        winner.close()
                return original_identity(session, **kwargs)

            try:
                with patch.object(wallet_accounts, "_identity", side_effect=winner_after_snapshot):
                    wallet_accounts.create_user_with_wallet(
                        loser, username="snapshot_loser", telegram_id=998, owner_admin_id=owner_id)
                    loser.commit()
            finally:
                loser.rollback()
                loser.close()
            check(name + " stale snapshot preserves both creations", db.query(models.User).filter(
                models.User.username.in_(("snapshot_winner", "snapshot_loser"))).count(), 2)
            check(name + " stale snapshot has one exact identity", db.execute(select(func.count()).select_from(
                rv.customer_identities).where(rv.customer_identities.c.tenant_scope_key == f"admin:{owner_id}",
                    rv.customer_identities.c.identity_key == "tg:998")).scalar_one(), 1)
        check(name + " no outgoing network attempts", _no_network.attempts, [])
    finally:
        db.rollback()
        db.close()


app = Path(__file__).resolve().parents[1] / "app"
constructors = []
for file in app.rglob("*.py"):
    for node in ast.walk(ast.parse(file.read_text())):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if (node.func.attr == "User" and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "models"):
                constructors.append(str(file.relative_to(app)))
check("only the wallet factory constructs User", constructors, ["services/wallet_accounts.py"])

for dialect, code, found, accepted in (
        ("mysql", 1020, 77, True), ("mariadb", 1020, 77, True),
        ("mysql", 1020, None, False), ("mysql", 1213, 77, False),
        ("mysql", 1205, 77, False), ("sqlite", 1020, 77, False)):
    fake = Mock()
    fake.get_bind.return_value = SimpleNamespace(dialect=SimpleNamespace(name=dialect))
    error = OperationalError("upsert", {}, Exception(code, "injected"))
    read = Mock()
    read.scalar_one_or_none.return_value = found
    fake.execute.side_effect = [error, read]
    try:
        actual = wallet_accounts._identity(fake, lineage="test", telegram_id=900, owner_admin_id=None)
        check(f"{dialect} {code} winner={found} accepted", actual, 77 if accepted else "must raise")
    except OperationalError as exc:
        check(f"{dialect} {code} winner={found} propagated", not accepted and exc is error)
    check(f"{dialect} {code} never rolls back caller", fake.rollback.call_count, 0)
    check(f"{dialect} {code} never commits caller", fake.commit.call_count, 0)

with contextlib.ExitStack() as cleanup:
    folder = cleanup.enter_context(tempfile.TemporaryDirectory(prefix="wallet_accounts_"))
    sqlite = create_engine(f"sqlite:///{folder}/test.db")
    cleanup.callback(sqlite.dispose)

    @event.listens_for(sqlite, "connect")
    def foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    scenario(sqlite, "sqlite")
    url = os.environ.get("MARIADB_TEST_URL", "").strip()
    if url:
        maria = scratch.claim(url)
        cleanup.callback(scratch.release, maria)
        scenario(maria, "mariadb")
    elif os.environ.get("CI"):
        check("CI requires real MariaDB", False)
    else:
        print("SKIP local MariaDB: no test URL; mandatory in CI")

sys.exit(bool(failures))
