"""Private P6 approval precondition: DB lock order and fail-closed binding."""
import os
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")
import _mariadb_scratch as scratch
import _no_network
_no_network.install([os.environ.get("MARIADB_TEST_URL", "")])

from fastapi import HTTPException
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app import models, models_provisioning as mp, models_receipt_void as rv
from app.services import bot_auth, provisioning_approval_binding as binding
from app.services import provisioning_schema, receipt_approval_intent as ri
from app.services import receipt_approval_registration as registration
from app.services import receipt_approval_runtime as runtime, receipt_void_schema, resource_leases


def scenario(engine):
    models.Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine, autoflush=False)
    assert receipt_void_schema.bootstrap(engine, Factory)["ready"]
    assert provisioning_schema.bootstrap(engine, Factory)["ready"]
    with Factory() as db:
        db.add_all([models.AdminUser(username="root", hashed_password="x", is_superadmin=True),
                    models.PanelSettings(id=1), models.Package(name="P", quota_gb=1, duration_days=30, price=100),
                    models.User(username="customer", telegram_id=123),
                    models.ApiKey(key="remote-approval", label="remote", key_type=bot_auth.KeyType.REMOTE_SHARED_BOT)])
        db.commit()
        package = db.query(models.Package).one()
        key = db.query(models.ApiKey).one()
        internal = bot_auth.BotPrincipal.internal(None)
        remote = bot_auth.BotPrincipal.from_api_key(key)
        base = dict(kind="renew", target_username="customer", amount=100, package_id=package.id,
                    pending_source_instance_id="p6-binding")
        first = registration.register(db, internal, ri.ApprovalIntent(pending_local_id=1, **base), approval_mode="auto")
        second = registration.register(db, remote, ri.ApprovalIntent(pending_local_id=2, **base), approval_mode="auto")
        db.get(mp.ProvisioningTypeMode, "renew_user").mode = "durable"
        db.commit()
        key_id = key.id

    def call(uuid, token, principal, expected=None):
        with Factory() as db:
            resource_leases.begin_business(db)
            statements = []
            def record(conn, cursor, statement, parameters, context, executemany):
                if statement.lstrip().lower().startswith("select"):
                    statements.append(statement.lower())
            event.listen(engine, "before_cursor_execute", record)
            try:
                if expected is None:
                    result = binding.lock_prepare(db, uuid, token, principal, "renew_user")
                    assert result.approval_uuid == uuid and result.execution_version == token
                    assert result.wallet_phase == "normal" and result.wallet_epoch == 0
                else:
                    try:
                        binding.lock_prepare(db, uuid, token, principal, "renew_user")
                    except HTTPException as exc:
                        assert (exc.status_code, exc.detail) == expected, (exc.status_code, exc.detail, expected)
                    else:
                        raise AssertionError("approval binding unexpectedly accepted")
            finally:
                event.remove(engine, "before_cursor_execute", record)
                db.rollback()
            return statements

    statements = call(first.approval_uuid, first.execution_token, internal)
    order = [next(i for i, statement in enumerate(statements) if f"from {table}" in statement)
             for table in ("wallet_runtime_state", "receipt_approval_runtime_state",
                           "provisioning_type_modes", "receipt_approvals")]
    assert order == sorted(order) and len(set(order)) == 4, statements
    assert call(second.approval_uuid, second.execution_token, remote)
    call(first.approval_uuid, first.execution_token + 1, internal, (409, "approval_superseded"))
    call(first.approval_uuid, first.execution_token, remote, (403, "execution_principal_mismatch"))
    call(second.approval_uuid, second.execution_token, internal, (403, "execution_principal_mismatch"))
    # The shared approval has a NULL key instance and owner. A dedicated
    # in-process bot also has a NULL key, but must not cross that tenant.
    call(first.approval_uuid, first.execution_token,
         bot_auth.BotPrincipal.internal(1), (403, "execution_scope_mismatch"))
    with Factory() as db:
        db.execute(rv.receipt_approval_effects.insert().values(
            approval_uuid=first.approval_uuid, effect_type="ledger_sale", effect_key="sale",
            resource_type="LedgerEntry", actual_projection="{}", resource_snapshot="{}"))
        db.commit()
    call(first.approval_uuid, first.execution_token, internal, (409, "approval_has_effects"))
    with Factory() as db:
        db.execute(rv.receipt_approval_effects.delete().where(
            rv.receipt_approval_effects.c.approval_uuid == first.approval_uuid))
        db.add(models.LedgerEntry(kind="sale_renew", amount=100, approval_uuid=first.approval_uuid))
        db.commit()
    call(first.approval_uuid, first.execution_token, internal, (409, "approval_has_effects"))
    with Factory() as db:
        db.query(models.LedgerEntry).filter(
            models.LedgerEntry.approval_uuid == first.approval_uuid).delete()
        db.commit()
    outsider = bot_auth.BotPrincipal(key_id=key_id, key_type=bot_auth.KeyType.GLOBAL_INTEGRATION,
        owner_admin_id=None, capabilities=frozenset(), label="outsider")
    call(first.approval_uuid, first.execution_token, outsider, (403, "execution_principal_mismatch"))
    with patch.object(runtime, "effective_registration_mode", return_value=runtime.BLOCKED):
        statements = call(first.approval_uuid, first.execution_token, internal, (503, "receipt_approval_blocked"))
    assert not any("from receipt_approvals" in statement for statement in statements)
    with Factory() as db:
        db.get(models.ApiKey, key_id).enabled = False
        db.commit()
    call(second.approval_uuid, second.execution_token, remote, (403, "execution_key_revoked"))
    with Factory() as db:
        db.get(mp.ProvisioningTypeMode, "renew_user").mode = "legacy"
        db.commit()
    statements = call(first.approval_uuid, first.execution_token, internal, (503, "provisioning_type_not_durable"))
    assert not any("from receipt_approvals" in statement for statement in statements)
    with Factory() as db:
        db.get(mp.ProvisioningTypeMode, "renew_user").mode = "durable"
        db.execute(rv.wallet_runtime_state.update().where(rv.wallet_runtime_state.c.id == 1).values(phase="fencing"))
        db.commit()
    statements = call(first.approval_uuid, first.execution_token, internal, (503, "wallet_phase_unavailable"))
    assert not any("from receipt_approvals" in statement for statement in statements)
    with Factory() as db:
        db.execute(rv.wallet_runtime_state.update().where(rv.wallet_runtime_state.c.id == 1).values(phase="normal"))
        db.execute(rv.receipt_approvals.update().where(rv.receipt_approvals.c.approval_uuid == first.approval_uuid)
                   .values(state="failed"))
        db.commit()
    call(first.approval_uuid, first.execution_token, internal, (409, "approval_precondition_failed"))
    with Factory() as db:
        try:
            binding.lock_prepare(db, first.approval_uuid, first.execution_token, internal, "renew_user")
        except resource_leases.LeaseProtocolError as exc:
            assert str(exc) == "business_transaction_not_fenced"
        else:
            raise AssertionError("unfenced transaction accepted")
    print("PASS", engine.dialect.name, "private approval precondition and runtime-first lock order")


with tempfile.TemporaryDirectory(prefix="um-p6-approval-binding-") as directory:
    engine = create_engine("sqlite:///" + os.path.join(directory, "test.db"))
    try:
        scenario(engine)
    finally:
        engine.dispose()
url = os.environ.get("MARIADB_TEST_URL", "").strip()
if url:
    engine = scratch.claim(url)
    try:
        scenario(engine)
    finally:
        scratch.release(engine)
elif os.environ.get("CI", "").lower() == "true":
    raise AssertionError("CI requires real MariaDB")
else:
    print("SKIP real MariaDB locally; mandatory in CI")
assert _no_network.attempts == []
