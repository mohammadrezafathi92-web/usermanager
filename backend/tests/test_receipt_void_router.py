"""The receipt-void HTTP routes (Receipt Void 17, 18, 21): superadmin only;
every route 404 while its flag is off; execute, force and the flag change
need the admin's own password (X-Confirm-Password) and a wrong one changes
nothing; a request that fails validation writes nothing. SQLite; no network.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
DIRECTORY = tempfile.mkdtemp(prefix="um-void-router-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(DIRECTORY, "t.db")
import _no_network
_no_network.install([])

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import deps, models, models_receipt_void as rv
from app.database import SessionLocal, engine, get_db
from app.routers import receipt_void as router
from app.security import hash_password
from app.services import receipt_void_schema

failures = []


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


models.Base.metadata.create_all(engine)
check("schema ready", receipt_void_schema.bootstrap(engine, SessionLocal)["ready"], True)
with SessionLocal() as db:
    db.add(models.PanelSettings(id=1))
    root = models.AdminUser(username="root", hashed_password=hash_password("pw-root"), is_superadmin=True)
    seller = models.AdminUser(username="seller", hashed_password=hash_password("pw-s"))
    db.add_all([root, seller])
    db.commit()
    db.refresh(root)
    db.refresh(seller)
    db.expunge_all()

app = FastAPI()
app.include_router(router.router)
current = {"admin": root}


def session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[deps.get_current_admin] = lambda: current["admin"]
app.dependency_overrides[get_db] = session
client = TestClient(app)
PW = {"X-Confirm-Password": "pw-root"}

current["admin"] = seller
check("a non-superadmin: 403 on every route", [client.post("/api/accounting/receipt-void/preview",
      json={"ledger_entry_id": 1}).status_code, client.get("/api/accounting/receipt-void/flags").status_code,
      client.post("/api/accounting/receipt-void/execute", headers={"X-Confirm-Password": "pw-s"},
                  json={"preview_token": "x", "typed_username": "u", "reason": "r", "danger_ack": True}).status_code],
      [403, 403, 403])
current["admin"] = root
check("flags: all off", client.get("/api/accounting/receipt-void/flags").json(),
      {"preview": False, "execute": False, "worker": False})
check("preview / status / audit while off: 404 receipt_void_disabled", [
    (r.status_code, r.json()["detail"]) for r in (
        client.post("/api/accounting/receipt-void/preview", json={"ledger_entry_id": 1}),
        client.get("/api/accounting/receipt-void/status/1"),
        client.get("/api/accounting/receipt-void/audit/00000000-0000-0000-0000-000000000000"))],
      [(404, "receipt_void_disabled")] * 3)
check("execute without the password: 403", client.post("/api/accounting/receipt-void/execute", json={
    "preview_token": "x", "typed_username": "u", "reason": "r", "danger_ack": True}).status_code, 403)
check("execute with a wrong password: 403", client.post("/api/accounting/receipt-void/execute",
      headers={"X-Confirm-Password": "nope"}, json={"preview_token": "x", "typed_username": "u", "reason": "r",
                                                    "danger_ack": True}).status_code, 403)
response = client.post("/api/accounting/receipt-void/execute", headers=PW, json={
    "preview_token": "x", "typed_username": "u", "reason": "r", "danger_ack": False})
check("execute without danger_ack: 422 before anything", (response.status_code, response.json()["detail"]),
      (422, "danger_ack_required"))
response = client.post("/api/accounting/receipt-void/execute", headers=PW, json={
    "preview_token": "x", "typed_username": "u", "reason": "r", "danger_ack": True})
check("execute while off: 404", (response.status_code, response.json()["detail"]), (404, "receipt_void_disabled"))
check("force while the worker is off: 404", client.post("/api/accounting/receipt-void/force-step/1", headers=PW, json={
    "step_key": "verify:1", "reason": "r", "danger_ack": True}).status_code, 404)
check("retry while the worker is off: 404", client.post("/api/accounting/receipt-void/retry/1").status_code, 404)
check("a flag change without the password: 403", client.put("/api/accounting/receipt-void/flags",
      json={"flag": "preview", "enabled": True}).status_code, 403)
response = client.put("/api/accounting/receipt-void/flags", headers=PW, json={"flag": "preview", "enabled": True})
check("preview on before the joint activation: 409", (response.status_code, response.json()["detail"]),
      (409, "receipt_void_requires_joint_activation"))
with SessionLocal() as db:
    db.execute(rv.receipt_approval_runtime_state.update().values(registration_mode="required",
                                                                 loyalty_timing_mode="payment_event",
                                                                 completeness_generation=1, key_identity_ready=True,
                                                                 key_identity_failure=None))
    db.commit()
check("after the activation: preview on with the password",
      client.put("/api/accounting/receipt-void/flags", headers=PW, json={"flag": "preview", "enabled": True}).json(),
      {"preview": True, "execute": False, "worker": False})
response = client.post("/api/accounting/receipt-void/preview", json={"ledger_entry_id": 999})
check("preview of a missing ledger row: 404", (response.status_code, response.json()["detail"]),
      (404, "receipt_approval_not_found"))
response = client.post("/api/accounting/receipt-void/preview", json={})
check("preview with neither key: 422", response.status_code, 422)
with SessionLocal() as db:
    operations = db.execute(select(func.count()).select_from(rv.wallet_operations)).scalar_one()
check("nothing above wrote an operation", operations, 0)
check("no network", _no_network.attempts, [])
engine.dispose()
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("all checks passed")
