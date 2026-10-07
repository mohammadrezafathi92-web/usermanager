"""Status is superadmin-only, read-only and never advertises a finished P6."""
import datetime as dt
import os
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")
import _no_network
_no_network.install()
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from app import models, models_provisioning as mp, deps
from app.database import get_db
from app.routers import provisioning
from app.services import provisioning_schema


with tempfile.TemporaryDirectory(prefix="um-provisioning-status-") as directory:
    engine = create_engine("sqlite:///" + os.path.join(directory, "test.db"), connect_args={"check_same_thread": False})
    Factory = sessionmaker(bind=engine, autoflush=False)
    assert provisioning_schema.bootstrap(engine, Factory)["ready"]
    db = Factory()
    operation = mp.ProvisioningOperation(operation_type="add_connection", business_key=str(uuid.uuid4()),
        request_hash="0" * 64, tenant_scope_key="shared", actor_kind="system", intent="PRIVATE_INTENT_SENTINEL",
        state="prepared", forward_deadline=dt.datetime.utcnow() + dt.timedelta(minutes=5), wallet_epoch_at_start=0)
    db.add(operation)
    db.flush()
    db.add(mp.ProvisioningStep(operation_id=operation.id, slot_key="s", direction="create", step_order=0,
        backend="mikrotik_wg", node_id=1, protocol="wireguard", state="staged",
        staged_password="PASSWORD_SENTINEL", staged_wg_private_key="KEY_SENTINEL"))
    db.commit()
    db.close()
    current = {"admin": models.AdminUser(id=1, username="root", is_superadmin=True)}
    def authenticated():
        if current["admin"] is None:
            raise HTTPException(401, "unauthorized")
        return current["admin"]
    def session():
        with Factory() as db:
            yield db
    app = FastAPI()
    app.include_router(provisioning.router)
    app.dependency_overrides[deps.get_current_admin] = authenticated
    app.dependency_overrides[get_db] = session
    statements = []
    def record(conn, cursor, statement, parameters, context, many):
        statements.append(statement)
    event.listen(engine, "before_cursor_execute", record)
    client = TestClient(app)
    response = client.get("/api/provisioning/status")
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["schema_ready"] and not result["p6_ready"] and not result["receipt_void_execution_available"]
    assert result["operation_counts"] == {"prepared": 1} and result["step_counts"] == {"staged": 1}
    assert result["runtime"]["gate_mode"] == "off" and result["missing_runner_actions"]
    assert "SENTINEL" not in response.text and "intent" not in result and "owner_host_id" not in result["runtime"]
    assert statements and all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
    current["admin"] = models.AdminUser(id=2, username="seller", is_superadmin=False, parent_admin_id=1)
    assert client.get("/api/provisioning/status").status_code == 403
    current["admin"] = None
    assert client.get("/api/provisioning/status").status_code == 401
    current["admin"] = models.AdminUser(id=1, username="root", is_superadmin=True)
    original = provisioning_schema.is_ready
    provisioning_schema.is_ready = lambda: False
    try:
        statements.clear()
        result = client.get("/api/provisioning/status").json()
        assert not result["schema_ready"] and "provisioning_schema_not_ready" in result["blockers"] and statements == []
    finally:
        provisioning_schema.is_ready = original
    with engine.begin() as conn:
        conn.execute(mp.ProvisioningRuntimeState.__table__.delete())
    response = client.get("/api/provisioning/status")
    assert response.status_code == 200 and not response.json()["schema_ready"]
    assert "provisioning_status_unavailable" in response.json()["blockers"]
    assert "runtime" not in response.json() and "operation_counts" not in response.json()
    with engine.begin() as conn:
        mp.ProvisioningRuntimeState.__table__.drop(conn)
    response = client.get("/api/provisioning/status")
    assert response.status_code == 200 and not response.json()["schema_ready"]
    assert "SELECT" not in response.text and "SENTINEL" not in response.text
    event.remove(engine, "before_cursor_execute", record)
    engine.dispose()
assert _no_network.attempts == []
print("PASS status: root-only, SELECT-only, secret-free, honest blockers and no queries when schema unavailable")
