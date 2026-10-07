"""P5 identity changes: isolated SQLite and mandatory real CI MariaDB."""
import ast
import contextlib
import os
from pathlib import Path
import sys
import tempfile
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DATABASE_URL", "sqlite://")
import _mariadb_scratch as scratch
import _no_network
_no_network.install([os.environ.get("MARIADB_TEST_URL", "").strip()])

from fastapi import HTTPException
from sqlalchemy import create_engine, event, select, func
from sqlalchemy.orm import sessionmaker
from app import models, models_receipt_void as rv, schemas
from app.services import wallet_accounts, wallet_identity, receipt_void_schema
from app.routers import admins as admin_router

failures = []


def check(label, actual, expected=True):
    print("PASS" if actual == expected else "FAIL", label)
    if actual != expected:
        failures.append((label, actual, expected))


def scenario(engine, name):
    models.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False)
    check(name + " schema ready", receipt_void_schema.bootstrap(engine, Session)["ready"])
    with Session() as db:
        root = models.AdminUser(username="root", hashed_password="x", is_superadmin=True, role="superadmin")
        a = models.AdminUser(username="a", hashed_password="x", role="admin")
        b = models.AdminUser(username="b", hashed_password="x", role="admin")
        db.add_all([root, a, b])
        db.commit()
        seller = models.AdminUser(username="seller", hashed_password="x", role="seller", parent_admin_id=a.id)
        db.add(seller)
        db.commit()
        user = wallet_accounts.create_user_with_wallet(db, username="customer", owner_admin_id=seller.id, balance=700)
        db.commit()

        def account():
            return dict(db.execute(select(rv.wallet_accounts).where(rv.wallet_accounts.c.user_id == user.id)).mappings().one())

        def count(table):
            return db.execute(select(func.count()).select_from(table)).scalar_one()

        original = account()
        user.notes = "pending caller edit"
        token = str(uuid.uuid4())
        op = wallet_identity.change(db, user, telegram_id=901, actor_kind="admin", actor_id=root.id, token=token)
        db.commit()
        check(name + " caller edits preserved", user.notes, "pending caller edit")
        check(name + " Telegram link applied", user.telegram_id, 901)
        check(name + " account lineage retained", account()["lineage_key"], original["lineage_key"])
        check(name + " balance unchanged", user.balance, 700)
        check(name + " binding changed", account()["customer_identity_id"] != original["customer_identity_id"])
        check(name + " one operation", count(rv.wallet_operations), 1)
        check(name + " one audit", count(rv.wallet_account_identity_rebinds), 1)
        again = wallet_identity.change(db, user, telegram_id=901, actor_kind="admin", actor_id=root.id, token=token)
        db.commit()
        check(name + " exact replay returns operation", again, op)
        check(name + " replay adds no audit", count(rv.wallet_account_identity_rebinds), 1)
        try:
            wallet_identity.change(db, user, telegram_id=902, token=token)
            check(name + " token mismatch refuses", False)
        except HTTPException as exc:
            check(name + " token mismatch refuses", exc.status_code, 409)
        db.rollback()
        before = account()
        wallet_identity.change(db, user, telegram_id=903)
        db.rollback()
        db.refresh(user)
        check(name + " rollback restores user", user.telegram_id, 901)
        check(name + " rollback restores account", account(), before)
        check(name + " rollback removes audit", count(rv.wallet_account_identity_rebinds), 1)
        wallet_identity.change(db, user, owner_admin_id=a.id, actor_kind="admin", actor_id=root.id)
        db.commit()
        check(name + " same-tenant owner transfer keeps identity", account()["customer_identity_id"], before["customer_identity_id"])
        check(name + " same-tenant owner transfer audited", count(rv.wallet_account_identity_rebinds), 2)
        wallet_identity.change(db, user, owner_admin_id=b.id)
        db.commit()
        check(name + " cross-tenant owner transfer changes identity", account()["customer_identity_id"] != before["customer_identity_id"])
        wallet_identity.change(db, user, telegram_id=None)
        db.commit()
        ident = db.execute(select(rv.customer_identities).where(
            rv.customer_identities.c.id == account()["customer_identity_id"])).mappings().one()
        check(name + " unlink uses private lineage", ident["identity_key"], f"private:{original['lineage_key']}")
        current = account()
        lot = db.execute(rv.wallet_lots.insert().values(wallet_account_id=current["id"],
            source_kind="legacy_baseline", source_key="fixture", original_amount=100,
            available_amount=100, consumed_amount=0, state="active")).inserted_primary_key[0]
        void = db.execute(rv.wallet_operations.insert().values(operation_type="void_receipt",
            business_key=str(uuid.uuid4()), request_hash="a" * 64, tenant_scope_key=ident["tenant_scope_key"],
            actor_kind="system", state="completed", epoch_at_start=0, result_snapshot="{}" )).inserted_primary_key[0]
        debt = db.execute(rv.wallet_debts.insert().values(customer_identity_id=current["customer_identity_id"],
            wallet_account_id=current["id"], source_wallet_lot_id=lot, source_void_operation_id=void,
            amount=100, state="open")).inserted_primary_key[0]
        db.commit()
        before_events = count(rv.wallet_account_identity_rebinds)
        for fields in ({"telegram_id": 904}, {"owner_admin_id": a.id}):
            try:
                wallet_identity.change(db, user, **fields)
                check(name + " debt refuses " + str(fields), False)
            except HTTPException as exc:
                check(name + " debt refuses " + str(fields), (exc.status_code, exc.detail), (409, "wallet_identity_has_debt"))
            db.rollback()
        check(name + " debt refusal preserves binding", account(), current)
        check(name + " debt refusal adds no audit", count(rv.wallet_account_identity_rebinds), before_events)
        db.execute(rv.wallet_debts.update().where(rv.wallet_debts.c.id == debt).values(settled_amount=100, state="settled"))
        db.commit()
        wallet_identity.change(db, user, telegram_id=904)
        db.commit()
        check(name + " settled debt stays with old identity", db.execute(select(rv.wallet_debts.c.customer_identity_id).where(
            rv.wallet_debts.c.id == debt)).scalar_one(), current["customer_identity_id"])
        for phase in ("fencing", "enforced"):
            db.execute(rv.wallet_runtime_state.update().values(phase=phase))
            db.commit()
            try:
                wallet_identity.change(db, user, telegram_id=905)
                check(name + " refuses " + phase, False)
            except HTTPException as exc:
                check(name + " refuses " + phase, exc.status_code, 503)
            db.rollback()
        db.execute(rv.wallet_runtime_state.update().values(phase="normal"))
        db.commit()
        child_user = wallet_accounts.create_user_with_wallet(db, username="child_customer", telegram_id=950, owner_admin_id=seller.id)
        db.commit()
        child_account = db.execute(select(rv.wallet_accounts).where(rv.wallet_accounts.c.user_id == child_user.id)).mappings().one()
        previous_identity = child_account["customer_identity_id"]
        admin_router.reparent_admin(seller.id, schemas.AdminReparentRequest(parent_admin_id=b.id, role="seller"), db=db, _s=root)
        bound = db.execute(select(rv.customer_identities.c.tenant_scope_key).select_from(
            rv.wallet_accounts.join(rv.customer_identities)).where(rv.wallet_accounts.c.user_id == child_user.id)).scalar_one()
        check(name + " seller reparent rebinds customer", bound, f"admin:{b.id}")
        check(name + " prior identity retained", db.execute(select(rv.customer_identities.c.id).where(
            rv.customer_identities.c.id == previous_identity)).scalar_one(), previous_identity)
        child_binding = db.execute(select(rv.wallet_accounts).where(
            rv.wallet_accounts.c.user_id == child_user.id)).mappings().one()
        child_lot = db.execute(rv.wallet_lots.insert().values(wallet_account_id=child_binding["id"],
            source_kind="legacy_baseline", source_key="hierarchy_debt", original_amount=100,
            available_amount=100, consumed_amount=0, state="active")).inserted_primary_key[0]
        child_debt = db.execute(rv.wallet_debts.insert().values(
            customer_identity_id=child_binding["customer_identity_id"], wallet_account_id=child_binding["id"],
            source_wallet_lot_id=child_lot, source_void_operation_id=void, amount=100,
            settled_amount=10, state="partially_settled")).inserted_primary_key[0]
        db.commit()
        seller_id, b_id, a_id = seller.id, b.id, a.id
        try:
            admin_router.reparent_admin(seller.id, schemas.AdminReparentRequest(parent_admin_id=a.id, role="seller"), db=db, _s=root)
            check(name + " hierarchy debt refuses", False)
        except HTTPException as exc:
            check(name + " hierarchy debt refuses", exc.detail, "wallet_identity_has_debt")
        db.rollback()
        check(name + " hierarchy rollback retains seller parent", db.get(models.AdminUser, seller_id).parent_admin_id, b_id)
        check(name + " hierarchy rollback retains account identity", db.execute(select(rv.wallet_accounts.c.customer_identity_id).where(
            rv.wallet_accounts.c.user_id == child_user.id)).scalar_one(), child_binding["customer_identity_id"])
        db.execute(rv.wallet_debts.update().where(rv.wallet_debts.c.id == child_debt).values(state="settled", settled_amount=100))
        db.commit()
        # Deleting a reseller also uses the shared writer, not a bulk owner UPDATE.
        admin_router.delete_admin(seller_id, db=db, current=root, _confirm=None)
        db.refresh(child_user)
        check(name + " admin deletion transfers customer to heir", child_user.owner_admin_id, b_id)
        check(name + " admin deletion retains wallet lineage", db.execute(select(rv.wallet_accounts.c.lineage_key).where(
            rv.wallet_accounts.c.user_id == child_user.id)).scalar_one(), child_binding["lineage_key"])
        racer = wallet_accounts.create_user_with_wallet(db, username="racer", telegram_id=960, owner_admin_id=a_id)
        db.commit()
        racer_id = racer.id
        initial_events = count(rv.wallet_account_identity_rebinds)
        db.rollback()
        barrier = threading.Barrier(2)

        def compete(telegram):
            with Session() as own:
                customer = own.get(models.User, racer_id)
                barrier.wait(timeout=10)
                try:
                    wallet_identity.change(own, customer, telegram_id=telegram)
                    own.commit()
                    return 200
                except HTTPException as exc:
                    own.rollback()
                    return exc.status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(compete, value) for value in (961, 962)]
            outcomes = [future.result(timeout=30) for future in futures]
        check(name + " one concurrent identity change wins", outcomes.count(200), 1)
        check(name + " stale concurrent request refuses", all(status in (200, 409, 503) for status in outcomes))
        check(name + " concurrent loser leaves no audit", count(rv.wallet_account_identity_rebinds), initial_events + 1)
        check(name + " no outgoing network", _no_network.attempts, [])


app = Path(__file__).resolve().parents[1] / "app"
for path in app.rglob("*.py"):
    if path.name == "wallet_identity.py":
        continue
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                targets = target.elts if isinstance(target, ast.Tuple) else [target]
                for item in targets:
                    if (isinstance(item, ast.Attribute) and isinstance(item.value, ast.Name)
                            and item.value.id == "user" and item.attr in ("telegram_id", "owner_admin_id")):
                        check("RV-63 identity writer outside service: " + str(path), False)

with contextlib.ExitStack() as cleanup:
    folder = cleanup.enter_context(tempfile.TemporaryDirectory(prefix="wallet_rebind_"))
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
        print("SKIP local MariaDB; mandatory in CI")
for failure in failures:
    print(failure)
sys.exit(bool(failures))
