"""L4 wiring: add_connection and delete_connection through the durable path
from the REAL production endpoints (Lifecycle 7.5, 12, 13, 16).

Fake runner (no node, no network); real T1, step transitions, T_final,
deletion machine, idempotency and the background worker.

  * type 'legacy': the endpoints run today's code (legacy provision/delete);
  * type 'durable': bot add_connection commits T1, the runner is asked to
    ensure the peer, T_final builds the Connection inside the customer's one
    service, and the response body is today's BotConnectionInfo;
  * the same X-Idempotency-Key again: the same connection, no new remote
    call; the same key with another request: 409;
  * an unknown runner outcome: the request answers 409 operation_in_progress
    with the operation id; the background worker later finishes it;
  * panel delete_connection: the connection is disabled first, removed by
    identity on the node, and only then deleted from the database.
"""
import datetime as dt
import os
import sys
import tempfile
import uuid
from unittest.mock import patch

DIRECTORY = tempfile.mkdtemp(prefix="um-durable-conn-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(DIRECTORY, "t.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _no_network
_no_network.install([])

from fastapi import HTTPException

from app import models, models_provisioning as mp, schemas
from app.database import SessionLocal, engine
from app.routers import bot as bot_router, users as users_router
from app.services import bot_auth, provisioning_contracts as contracts, provisioning_durable as durable
from app.services import provisioning_parent_execute as execute, provisioning_schema, receipt_void_schema
from app.services import remote_action as action, user_ops, wallet_accounts
from app.services.provisioning_host import HostIdentity

failures = []


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


models.Base.metadata.create_all(engine)
check("schemas ready", (receipt_void_schema.bootstrap(engine, SessionLocal)["ready"],
                        provisioning_schema.bootstrap(engine, SessionLocal)["ready"]), (True, True))
HOST = HostIdentity("d4" * 32, str(uuid.uuid4()))
durable.read_identity = lambda: HOST
with SessionLocal() as db:
    runtime = db.get(mp.ProvisioningRuntimeState, 1)
    runtime.owner_state, runtime.owner_host_id, runtime.owner_boot_id = "active", HOST.host_id, HOST.boot_id
    runtime.owner_claimed_at = runtime.owner_heartbeat_at = runtime.lock_verified_at = dt.datetime.utcnow()
    runtime.ownership_epoch, runtime.gate_mode, runtime.lock_backend = 1, "enforced", "flock"
    admin = models.AdminUser(username="root", hashed_password="x", is_superadmin=True)
    db.add(admin)
    db.add(models.PanelSettings(id=1))
    user = wallet_accounts.create_user_with_wallet(db, username="cust", balance=0, telegram_id=77)
    node = models.Node(name="mt", type=models.NodeType.mikrotik, enabled=True, mt_host="10.7.7.7",
                       mt_wireguard_interface="wg0", mt_client_subnet="10.9.0.0/24")
    db.add(node)
    db.flush()
    service = models.Purchase(user_id=user.id, quota_bytes=1024 ** 3, status=models.UserStatus.active)
    db.add(service)
    db.flush()
    db.add(mp.ProvisioningNodeContract(node_id=node.id, backend="mikrotik_wg", state="ready",
        adapter_version=contracts.adapter_version("mikrotik_wg"), config_fingerprint=contracts.config_fingerprint(node),
        server_fingerprint="b" * 64, contract='{"not_exist":[]}', verified_by_admin_id=admin.id,
        verified_at=dt.datetime.utcnow()))
    db.commit()
    USER, NODE, SERVICE, ADMIN = user.id, node.id, service.id, admin.id
PRINCIPAL = bot_auth.BotPrincipal.internal(None, scope_enforced=False)


def set_mode(kind, mode):
    with SessionLocal() as db:
        db.get(mp.ProvisioningTypeMode, kind).mode = mode
        db.commit()


def add(key, protocol="wireguard"):
    # Building the share link asks the router for its public key: not part of
    # what is tested here, and it would be a network call.
    with SessionLocal() as db, patch.object(user_ops, "get_connection_share", side_effect=RuntimeError("no node")):
        spec = schemas.BotCreateConnectionSpec(node_id=NODE, protocol=protocol)
        return bot_router.add_connection("cust", spec, db=db, owner_admin_id=None, principal=PRINCIPAL,
                                         x_idempotency_key=key)


seen = []


def ok_runner(dto):
    seen.append(dto.action_type)
    if dto.action_type in (action.ActionType.WG_ENSURE_ABSENT,):
        return action.RemoteActionResult(dto.action_id, action.Outcome.ABSENT_VERIFIED, write_attempted=True,
                                         remote_outcome="verified_absent")
    return action.RemoteActionResult(dto.action_id, action.Outcome.SUCCEEDED, write_attempted=True)


# ------------------------------------------------------------------ legacy
with patch.object(user_ops, "provision_connection", side_effect=RuntimeError("legacy path used")):
    try:
        add(str(uuid.uuid4()))
        legacy = "durable path"
    except RuntimeError as exc:
        legacy = str(exc)
check("type 'legacy': bot add_connection runs today's code", legacy, "legacy path used")

# ------------------------------------------------------------------ durable add
set_mode("add_connection", "durable")
key = str(uuid.uuid4())
with patch.object(execute.remote_runner, "run_action", side_effect=ok_runner):
    info = add(key)
    first_calls = list(seen)
    again = add(key)
with SessionLocal() as db:
    connections = db.query(models.Connection).filter_by(user_id=USER).all()
    operation = db.query(mp.ProvisioningOperation).filter_by(operation_type="add_connection").one()
    step = db.query(mp.ProvisioningStep).filter_by(operation_id=operation.id).one()
check("durable: the runner ensured the peer once; T_final built ONE connection in the customer's service",
      (first_calls, len(connections), connections[0].purchase_id, operation.state, step.state),
      ([action.ActionType.WG_ENSURE_PRESENT], 1, SERVICE, "completed", "active"))
check("durable: today's response body", (type(info).__name__, info.id, info.type), ("BotConnectionInfo", connections[0].id,
                                                                                  models.ConnectionType.wireguard))
check("the same idempotency key again: same connection, no new remote call",
      (again.id, seen), (info.id, [action.ActionType.WG_ENSURE_PRESENT]))
try:
    add(key, protocol="openvpn")
    changed = "accepted"
except HTTPException as exc:
    changed = (exc.status_code, exc.detail)
check("the same key with another request: 409", changed, (409, "provisioning_request_changed"))
check("the staged private key is gone from the step", step.staged_wg_private_key, None)


# ------------------------------------------------------------------ unknown outcome -> in progress -> worker
def unknown(dto):
    return action.RemoteActionResult.killed_unknown(dto.action_id, "hard_deadline_exceeded")


with patch.object(execute.remote_runner, "run_action", side_effect=unknown), \
        patch.object(durable, "REQUEST_BUDGET_SECONDS", 0.5):
    try:
        add(str(uuid.uuid4()))
        pending = "completed"
    except HTTPException as exc:
        pending = (exc.status_code, exc.detail.get("error") if isinstance(exc.detail, dict) else exc.detail)
        pending_id = exc.detail.get("operation_id") if isinstance(exc.detail, dict) else None
check("an unknown runner outcome: 409 operation_in_progress, never success", pending, (409, "operation_in_progress"))
with SessionLocal() as db:
    step = db.query(mp.ProvisioningStep).filter_by(operation_id=pending_id).one()
    check("the step keeps its remote_calling marker for recovery", step.state, "remote_calling")
    db.query(mp.ProvisioningStep).filter_by(operation_id=pending_id).update({"next_retry_at": None})
    db.query(mp.ProvisioningOperation).filter_by(id=pending_id).update({"next_retry_at": None})
    db.commit()


def recovery_read(dto):
    # The recovery read finds the peer exactly as staged.
    return action.RemoteActionResult(dto.action_id, action.Outcome.ALREADY_PRESENT_VERIFIED, write_attempted=False)


with patch.object(execute.remote_runner, "run_action", side_effect=recovery_read):
    for _ in range(6):
        durable.recover_due(SessionLocal)
with SessionLocal() as db:
    op = db.get(mp.ProvisioningOperation, pending_id)
    check("the background worker finished it after reading the remote state", op.state, "completed")

# ------------------------------------------------------------------ durable delete (panel)
set_mode("delete_connection", "durable")
seen.clear()
with SessionLocal() as db, patch.object(execute.remote_runner, "run_action", side_effect=ok_runner):
    target = info.id
    result = users_router.delete_connection(USER, target, db=db, admin=db.get(models.AdminUser, ADMIN), _confirm=None,
                                            _perm=None, x_idempotency_key=str(uuid.uuid4()))
with SessionLocal() as db:
    check("panel delete: removed by identity on the node, then the row deleted",
          (result, seen, db.get(models.Connection, target)), ({"ok": True}, [action.ActionType.WG_ENSURE_ABSENT], None))

check("no network", _no_network.attempts, [])
engine.dispose()
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("all checks passed")
