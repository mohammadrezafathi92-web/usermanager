"""Superadmin operator actions (Lifecycle 11.5, 13, 10.10). Fake runner; no
network.

  * a forward identity conflict leaves the operation cleanup_required (never
    compensated automatically); GET operations/{id} shows it with no secret;
  * retry: the safe side - compensation by the step's own identity (a
    cleanup state erased the staged credential; forward is impossible);
  * force-step: the step is abandoned with the admin, reason and time, the
    operation compensated (money released), the step listed as an orphan;
  * rotate refuses while not inert; fork-reset needs the exact confirmation,
    is refused on the original owner host, and otherwise resets everything
    in one commit (types legacy, gate off, no owner, contracts invalidated,
    a new namespace in the column AND the file, one audited event).
"""
import datetime as dt
import os
import sys
import tempfile
import uuid
from unittest.mock import patch

DIRECTORY = tempfile.mkdtemp(prefix="um-operator-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(DIRECTORY, "t.db")
os.environ["UM_LOCK_DIR"] = os.path.join(DIRECTORY, "locks")
os.makedirs(os.environ["UM_LOCK_DIR"], mode=0o700)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _no_network
_no_network.install([])

from fastapi import HTTPException

from app import models, models_provisioning as mp, schemas
from app.database import SessionLocal, engine
from app.routers import bot as bot_router
from app.services import bot_auth, provisioning_contracts as contracts, provisioning_durable as durable
from app.services import provisioning_installation, provisioning_operator as operator
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
HOST = HostIdentity("f1" * 32, str(uuid.uuid4()))
durable.read_identity = lambda: HOST
with SessionLocal() as db:
    host = db.get(mp.ProvisioningRuntimeState, 1)
    host.owner_state, host.owner_host_id, host.owner_boot_id = "active", HOST.host_id, HOST.boot_id
    host.owner_claimed_at = host.owner_heartbeat_at = host.lock_verified_at = dt.datetime.utcnow()
    host.ownership_epoch, host.gate_mode, host.lock_backend = 1, "enforced", "flock"
    db.add(models.PanelSettings(id=1))
    root = models.AdminUser(username="root", hashed_password="x", is_superadmin=True)
    reseller = models.AdminUser(username="res", hashed_password="x", balance=1000)
    db.add_all([root, reseller])
    db.flush()
    node = models.Node(name="mt", type=models.NodeType.mikrotik, enabled=True, mt_host="10.17.0.1",
                       mt_wireguard_interface="wg0", mt_client_subnet="10.71.0.0/24")
    db.add(node)
    db.flush()
    db.add(mp.ProvisioningNodeContract(node_id=node.id, backend="mikrotik_wg", state="ready",
        adapter_version=contracts.adapter_version("mikrotik_wg"), config_fingerprint=contracts.config_fingerprint(node),
        server_fingerprint="b" * 64, contract='{"not_exist":[]}', verified_by_admin_id=root.id,
        verified_at=dt.datetime.utcnow()))
    db.add(models.AdminNodeAccess(admin_id=reseller.id, node_id=node.id))
    package = models.Package(name="P", quota_gb=5, duration_days=30, price=500, cooperation_price=300,
                             owner_admin_id=reseller.id)
    db.add(package)
    db.flush()
    db.add(models.PackageConnection(package_id=package.id, node_id=node.id, protocol="wireguard"))
    wallet_accounts.create_user_with_wallet(db, username="cust", telegram_id=950, owner_admin_id=reseller.id)
    db.commit()
    ROOT, RESELLER, PACKAGE, INSTALLATION = root.id, reseller.id, package.id, host.installation_uuid
    db.get(mp.ProvisioningTypeMode, "purchase").mode = "durable"
    db.commit()
PRINCIPAL = bot_auth.BotPrincipal.internal(None, scope_enforced=False)
NO_SHARE = patch.object(user_ops, "get_connection_share", side_effect=RuntimeError("no node"))


def conflict(dto):
    if dto.action_type is action.ActionType.WG_ENSURE_ABSENT:
        return action.RemoteActionResult(dto.action_id, action.Outcome.ABSENT_VERIFIED, write_attempted=True,
                                         remote_outcome="verified_absent")
    return action.RemoteActionResult(dto.action_id, action.Outcome.CONFLICT, write_attempted=False,
                                     error_code="identity_conflict")


def found(dto):
    if dto.action_type is action.ActionType.WG_ENSURE_ABSENT:
        return conflict(dto)
    if (dto.params or {}).get("recovery_read"):
        return action.RemoteActionResult(dto.action_id, action.Outcome.ALREADY_PRESENT_VERIFIED, write_attempted=False)
    return action.RemoteActionResult(dto.action_id, action.Outcome.SUCCEEDED, write_attempted=True)


def buy():
    with SessionLocal() as db, NO_SHARE, patch.object(execute.remote_runner, "run_action", side_effect=conflict):
        try:
            bot_router.purchase_package("cust", schemas.BotPurchasePackageRequest(package_id=PACKAGE, paid_amount=500,
                                        payment_method="card"), db=db, owner_admin_id=None, principal=PRINCIPAL,
                                        x_idempotency_key=str(uuid.uuid4()))
            return "ok"
        except HTTPException as exc:
            return exc.detail.get("state") if isinstance(exc.detail, dict) else exc.detail


def latest():
    with SessionLocal() as db:
        return db.query(mp.ProvisioningOperation).order_by(mp.ProvisioningOperation.id.desc()).first().id


def recover(runner):
    with NO_SHARE, patch.object(execute.remote_runner, "run_action", side_effect=runner):
        for _ in range(6):
            durable.recover_due(SessionLocal)


def flat(value):
    if isinstance(value, dict):
        return set(value) | set().union(*(flat(v) for v in value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*(flat(v) for v in value)) if value else set()
    return set()


# ------------------------------------------------------------------ conflict -> view -> retry
check("a forward identity conflict: the operation waits for an operator (cleanup_required)", buy(), "cleanup_required")
first = latest()
recover(found)
with SessionLocal() as db:
    shown = operator.view(db, first)
check("...the worker never compensates or completes it on its own", shown["state"], "cleanup_required")
check("GET operations/{id}: state and steps, no credential field",
      (shown["steps"][0]["state"], any(key.startswith("staged_") or key.endswith("private_key") for key in flat(shown))),
      ("cleanup_required", False))
try:
    operator.retry(SessionLocal, ROOT, first, "  ")
    empty = "accepted"
except HTTPException as exc:
    empty = exc.detail
check("retry without a reason is refused", empty, "reason_required")
with SessionLocal() as db:
    before = db.get(models.AdminUser, RESELLER).balance
operator.retry(SessionLocal, ROOT, first, "peer checked on the router")
with SessionLocal() as db:
    check("retry: the safe side - the step is removed by its own identity (no forward without its credential)",
          (operator.view(db, first)["state"], operator.view(db, first)["steps"][0]["state"]),
          ("compensating", "compensating"))
recover(found)
with SessionLocal() as db:
    check("...verified absent: compensated, the credit released, nothing created",
          (operator.view(db, first)["state"], db.query(models.Connection).count(),
           db.get(models.AdminUser, RESELLER).balance - before), ("compensated", 0, 300))

# ------------------------------------------------------------------ force-step
with SessionLocal() as db:
    before = db.get(models.AdminUser, RESELLER).balance
check("a second conflict", buy(), "cleanup_required")
second = latest()
with SessionLocal() as db:
    step_id = operator.view(db, second)["steps"][0]["id"]
operator.force_step(SessionLocal, ROOT, second, step_id, "peer belongs to another panel")
recover(found)
with SessionLocal() as db:
    reservation = db.query(mp.PaymentReservation).filter_by(operation_id=second).one()
    after = db.get(models.AdminUser, RESELLER).balance
    listed = operator.orphans(db)
check("force-step: compensated, the reseller's credit released, nothing created",
      (operator.view(SessionLocal(), second)["state"], reservation.state, after - before),
      ("compensated", "released", 0))
check("...the abandoned step is the orphan's audit: who, why, which node; no secret",
      ([(o["step_id"], o["forced_by_admin_id"], o["force_reason"], o["backend"]) for o in listed],
       any(key.startswith("staged_") for o in listed for key in o)),
      ([(step_id, ROOT, "peer belongs to another panel", "mikrotik_wg")], False))

# ------------------------------------------------------------------ rotate / fork-reset
try:
    operator.rotate(SessionLocal, ROOT, reason="new namespace")
    inert = "rotated"
except HTTPException as exc:
    inert = exc.detail
check("rotate refuses while a type is durable / the gate is on", inert, "installation_rotate_not_inert")
try:
    operator.rotate(SessionLocal, ROOT, reason="clone", fork=True, confirmation="yes")
    confirm = "forked"
except HTTPException as exc:
    confirm = exc.detail
check("fork-reset needs the exact confirmation sentence", confirm, "fork_confirmation_mismatch")
try:
    with patch.object(operator, "read_identity", return_value=HOST):
        operator.rotate(SessionLocal, ROOT, reason="clone", fork=True, confirmation=operator.FORK_CONFIRMATION)
    original = "forked"
except HTTPException as exc:
    original = exc.detail
check("fork-reset is refused on the original owner host", original, "fork_on_original_owner_host")
CLONE = HostIdentity("f2" * 32, str(uuid.uuid4()))
with patch.object(operator, "read_identity", return_value=CLONE):
    result = operator.rotate(SessionLocal, ROOT, reason="independent clone", fork=True,
                             confirmation=operator.FORK_CONFIRMATION)
with SessionLocal() as db:
    row = db.get(mp.ProvisioningRuntimeState, 1)
    event = db.query(mp.ProvisioningOwnershipEvent).filter_by(event_type="installation_fork_reset").one()
    check("fork-reset in one commit: types legacy, gate off, no owner, contracts invalidated, new namespace",
          (result["event"], {m.mode for m in db.query(mp.ProvisioningTypeMode)}, row.gate_mode, row.owner_state,
           row.owner_host_id, {c.state for c in db.query(mp.ProvisioningNodeContract)},
           row.installation_uuid != INSTALLATION),
          ("installation_fork_reset", {"legacy"}, "off", "none", None, {"invalidated"}, True))
    check("...the file names the same new namespace; one audited event with its reason",
          (provisioning_installation.read_file() == row.installation_uuid, event.reason, event.actor_admin_id),
          (True, "independent clone", ROOT))

check("no network", _no_network.attempts, [])
engine.dispose()
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("all checks passed")
