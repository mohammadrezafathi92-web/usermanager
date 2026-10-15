"""Control plane of durable provisioning (Lifecycle 10.4, 10.6, 16).

What must hold:
  * gate-mode changes follow the 10.6 table exactly, wait for writers that
    hold the shared mode lock (and time out without changing anything),
    write one audited event, and never skip a mode;
  * 'enforced' is refused while node_gate cannot run legacy writers in the
    runner, and while uninstrumented-writer / mode-lock-miss counters are
    not zero;
  * lowering the gate requires every type legacy, no unfinished operation
    and no held node lock;
  * a type becomes durable only when its callers are wired, the gate is
    enforced, this host owns it and every enabled node contract is ready;
    it returns to legacy only with no unfinished operation of that type;
  * the HTTP routes are superadmin-only and need the admin's own password;
  * on Linux, verify claims an unowned installation (writing the
    installation file from the column first) and a second verify from the
    same host/boot keeps the epoch.
"""
import datetime as dt
import os
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DATABASE_URL", "sqlite://")
import _mariadb_scratch as scratch
import _no_network
_no_network.install([os.environ.get("MARIADB_TEST_URL", "")])

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import deps, models, models_provisioning as mp
from app.routers import provisioning as router
from app.security import hash_password
from app.services import gate_locks, node_gate, provisioning_control as control, provisioning_contracts as contracts
from app.services import provisioning_readiness as readiness, provisioning_schema, receipt_void_schema, wallet_accounts
from app.services.provisioning_host import HostIdentity

failures = []


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def refusal(call):
    try:
        return ("returned", call())
    except HTTPException as refused:
        detail = refused.detail
        return (refused.status_code, detail.get("code") if isinstance(detail, dict) else detail)


def scenario(label, engine, base_dir):
    L = f"[{label}] "
    os.environ["UM_LOCK_DIR"] = base_dir          # the HTTP routes use the configured lock directory
    models.Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine, autoflush=False)
    check(L + "schemas ready", (receipt_void_schema.bootstrap(engine, Factory)["ready"],
                                provisioning_schema.bootstrap(engine, Factory)["ready"]), (True, True))
    identity = HostIdentity("d" * 64, str(uuid.uuid4()))
    control.read_identity = lambda: identity
    with Factory() as db:
        db.add_all([models.PanelSettings(id=1),
                    models.AdminUser(id=1, username="root", hashed_password=hash_password("pw-root"), is_superadmin=True),
                    models.AdminUser(id=2, username="seller", hashed_password=hash_password("pw-s"), parent_admin_id=1),
                    models.Node(id=7, name="mt", type=models.NodeType.mikrotik, enabled=True, mt_host="10.0.0.1",
                                mt_wireguard_interface="wg0")])
        installation = db.get(mp.ProvisioningRuntimeState, 1).installation_uuid
        db.commit()
    path = os.path.join(base_dir, "installation.id")
    with open(path, "w") as fh:
        fh.write(installation + "\n")
    os.chmod(path, 0o600)

    def runtime():
        with Factory() as db:
            return db.get(mp.ProvisioningRuntimeState, 1)

    def set_owned():
        with Factory() as db:
            row = db.get(mp.ProvisioningRuntimeState, 1)
            now = dt.datetime.utcnow()
            row.owner_state, row.owner_host_id, row.owner_boot_id = "active", identity.host_id, identity.boot_id
            row.owner_claimed_at = row.owner_heartbeat_at = row.lock_verified_at = now
            row.ownership_epoch, row.lock_backend = 1, "flock"
            db.commit()

    gate = lambda target, **kw: refusal(lambda: control.change_gate_mode(Factory, 1, target, base_dir=base_dir, **kw))

    # ------------------------------------------------------------------ gate mode
    check(L + "off -> shadow refused without a verified lock backend / owner", gate("shadow"),
          (409, "ownership_lock_not_ready"))
    check(L + "off -> enforced refused: no skipping a mode", gate("enforced"), (409, "gate_mode_transition_invalid"))
    set_owned()
    with Factory() as db:
        db.add(mp.ProvisioningGateStat(node_id=0, mode_lock_miss_count=2, updated_at=dt.datetime.utcnow()))
        db.commit()
    check(L + "off -> shadow refused while mode-lock misses are counted", gate("shadow"),
          (409, "mode_lock_miss_count_nonzero"))
    check(L + "the counter reset clears the blockers", control.reset_gate_stats(Factory, 1), {"reset": True})

    writer = gate_locks.FileLock(gate_locks.mode_lock_path(installation, base_dir)).acquire(shared=True, timeout=0)
    before = runtime().gate_mode_epoch
    check(L + "a change waits for a writer of the old mode and times out without changing anything",
          (gate("shadow", timeout=0.3), runtime().gate_mode, runtime().gate_mode_epoch),
          ((409, "gate_mode_change_timeout"), "off", before))
    writer.release()
    result = gate("shadow")
    check(L + "off -> shadow once the writer is gone", (result[0], result[1]["gate_mode"], runtime().gate_mode_epoch),
          ("returned", "shadow", before + 1))
    with Factory() as db:
        events = [(e.event_type, e.gate_mode_from, e.gate_mode_to, e.actor_admin_id)
                  for e in db.query(mp.ProvisioningOwnershipEvent).order_by(mp.ProvisioningOwnershipEvent.id)]
    check(L + "one audited gate_mode_change event", events, [("gate_mode_change", "off", "shadow", 1)])
    check(L + "the same target again is a no-op", (gate("shadow")[0], runtime().gate_mode_epoch),
          ("returned", before + 1))

    with Factory() as db:
        db.query(mp.ProvisioningGateStat).filter_by(node_id=0).update({"writer_not_instrumented_count": 1})
        db.commit()
    check(L + "shadow -> enforced refused while an uninstrumented writer was seen", gate("enforced"),
          (409, "writer_not_instrumented_count_nonzero"))
    control.reset_gate_stats(Factory, 1)
    check(L + "this build runs legacy writers in the runner (enforced supported)", node_gate.ENFORCED_SUPPORTED, True)
    node_gate.ENFORCED_SUPPORTED = False
    try:
        check(L + "a build that cannot run legacy writers in the runner refuses shadow -> enforced",
              (gate("enforced"), runtime().gate_mode), ((409, "gate_enforced_not_implemented"), "shadow"))
    finally:
        node_gate.ENFORCED_SUPPORTED = True

    # Lowering.
    with Factory() as db:
        db.get(mp.ProvisioningTypeMode, "add_connection").mode = "durable"
        db.commit()
    check(L + "shadow -> off refused while a type is durable", gate("off"), (409, "operation_types_durable"))
    with Factory() as db:
        db.get(mp.ProvisioningTypeMode, "add_connection").mode = "legacy"
        db.add(mp.ProvisioningOperation(operation_type="add_connection", business_key="k1", request_hash="0" * 64,
            tenant_scope_key="shared", actor_kind="system", intent="{}", state="prepared",
            forward_deadline=dt.datetime.utcnow(), wallet_epoch_at_start=0))
        db.commit()
    check(L + "shadow -> off refused with an unfinished operation", gate("off"), (409, "provisioning_operations_pending"))
    with Factory() as db:
        db.query(mp.ProvisioningOperation).update({"state": "compensated", "completed_at": dt.datetime.utcnow()})
        db.commit()
    node_lock = gate_locks.FileLock(gate_locks.node_lock_path(installation, 7, base_dir)).acquire(shared=False, timeout=0)
    check(L + "shadow -> off refused while a node lock is held", gate("off"), (409, "node_lock_held"))
    node_lock.release()
    check(L + "shadow -> off when quiescent", (gate("off")[0], runtime().gate_mode), ("returned", "off"))

    # ------------------------------------------------------------------ type mode
    kind = lambda op, mode: refusal(lambda: control.change_type_mode(Factory, 1, op, mode))
    check(L + "unknown operation type rejected", kind("teleport", "durable"), (422, "operation_type_mode_invalid"))
    original = control.DURABLE_WIRED
    check(L + "every operation type's callers are wired (P6)", original, frozenset(mp.OPERATION_TYPES))
    control.DURABLE_WIRED = frozenset()
    check(L + "a type whose callers are not wired cannot become durable", kind("add_connection", "durable"),
          (409, "durable_callers_not_wired"))
    control.DURABLE_WIRED = frozenset({"add_connection", "purchase"})
    try:
        check(L + "wired, but the gate is not enforced", kind("add_connection", "durable"), (409, "gate_not_enforced"))
        with Factory() as db:   # Pretend enforced to reach the node checks (the gate move itself is refused above).
            db.get(mp.ProvisioningRuntimeState, 1).gate_mode = "enforced"
            db.commit()
        check(L + "enforced, but node 7 has no ready contract", kind("add_connection", "durable"),
              (409, "node_contract_not_ready"))
        with Factory() as db:
            node = db.get(models.Node, 7)
            db.add(mp.ProvisioningNodeContract(node_id=7, backend="mikrotik_wg", state="ready",
                adapter_version=contracts.adapter_version(), server_fingerprint="s" * 64,
                config_fingerprint=contracts.config_fingerprint(node), contract='{"not_exist": []}',
                verified_by_admin_id=1, verified_at=dt.datetime.utcnow()))
            db.get(models.PanelSettings, 1).ha_enabled = True
            db.commit()
        check(L + "HA without a coordinator refuses durable", kind("add_connection", "durable"),
              (409, "provisioning_ha_unsupported"))
        with Factory() as db:
            db.get(models.PanelSettings, 1).ha_enabled = False
            db.commit()
        check(L + "every precondition met: durable", kind("add_connection", "durable"),
              ("returned", {"operation_type": "add_connection", "mode": "durable"}))
        # L6: switching 'purchase' backfills the one-time claims of purchases
        # made before, in the same transaction (Lifecycle 5.5, 16).
        with Factory() as db:
            once = models.Package(name="Once", quota_gb=1, duration_days=1, price=1, one_time_per_user=True)
            buyer = wallet_accounts.create_user_with_wallet(db, username="once-" + L.strip("[] "), telegram_id=4242)
            db.add(once)
            db.flush()
            db.add(models.Purchase(user_id=buyer.id, package_id=once.id, quota_bytes=1,
                                   status=models.UserStatus.active))
            db.commit()
            once_id = once.id
        check(L + "purchase -> durable", kind("purchase", "durable")[0], "returned")
        with Factory() as db:
            claims = sorted(c.claim_kind for c in db.query(mp.OneTimePackageClaim).filter_by(package_id=once_id))
        check(L + "...the earlier one-time purchase now holds its user and Telegram claims", claims,
              ["telegram", "user"])
        check(L + "purchase back to legacy", kind("purchase", "legacy")[0], "returned")
        with Factory() as db:
            db.add(mp.ProvisioningOperation(operation_type="add_connection", business_key="k2", request_hash="0" * 64,
                tenant_scope_key="shared", actor_kind="system", intent="{}", state="provisioning",
                forward_deadline=dt.datetime.utcnow(), wallet_epoch_at_start=0))
            db.commit()
        check(L + "back to legacy refused while its operation is unfinished", kind("add_connection", "legacy"),
              (409, "provisioning_operations_pending"))
        with Factory() as db:
            db.query(mp.ProvisioningOperation).filter_by(business_key="k2").update(
                {"state": "completed", "completed_at": dt.datetime.utcnow()})
            db.commit()
        check(L + "back to legacy once drained", kind("add_connection", "legacy")[0], "returned")
    finally:
        control.DURABLE_WIRED = original
        with Factory() as db:
            db.get(mp.ProvisioningRuntimeState, 1).gate_mode = "off"
            db.commit()

    # ------------------------------------------------------------------ HTTP
    current = {"admin": None}
    app = FastAPI()
    app.include_router(router.router)
    app.dependency_overrides[deps.get_current_admin] = lambda: current["admin"]
    app.dependency_overrides[router.get_session_factory] = lambda: Factory
    client = TestClient(app)
    with Factory() as db:
        root, seller = db.get(models.AdminUser, 1), db.get(models.AdminUser, 2)
        db.expunge_all()
    current["admin"] = seller
    check(L + "a seller cannot change the gate", client.post("/api/provisioning/gate-mode", json={"target": "shadow"},
          headers={"X-Confirm-Password": "pw-s"}).status_code, 403)
    current["admin"] = root
    check(L + "the superadmin needs the password", client.post("/api/provisioning/gate-mode",
          json={"target": "shadow"}).status_code, 403)
    response = client.post("/api/provisioning/gate-mode", json={"target": "shadow"}, headers={"X-Confirm-Password": "pw-root"})
    check(L + "with the password the change goes through", (response.status_code, response.json().get("gate_mode")),
          (200, "shadow"))
    check(L + "the response carries no host or boot identity",
          identity.host_id in response.text or identity.boot_id in response.text, False)
    control.DURABLE_WIRED = frozenset()
    response = client.post("/api/provisioning/type-modes/purchase", json={"mode": "durable"},
                           headers={"X-Confirm-Password": "pw-root"})
    control.DURABLE_WIRED = original
    check(L + "type route reports the unwired type", (response.status_code, response.json()["detail"]["code"]),
          (409, "durable_callers_not_wired"))
    client.post("/api/provisioning/gate-mode", json={"target": "off"}, headers={"X-Confirm-Password": "pw-root"})

    # ------------------------------------------------------------------ verify / claim (Linux only)
    if sys.platform.startswith("linux"):
        with Factory() as db:
            row = db.get(mp.ProvisioningRuntimeState, 1)
            row.owner_state, row.owner_host_id, row.owner_boot_id = "none", None, None
            row.owner_claimed_at = row.owner_heartbeat_at = None
            row.lock_backend, row.lock_verified_at = "none", None
            db.commit()
        os.unlink(path)
        result = refusal(lambda: control.verify_claim(Factory, 1, base_dir=base_dir))
        check(L + "verify claims an unowned, inert installation (file written from the column)",
              (result[0], result[1].get("owner_state") if result[0] == "returned" else result,
               os.path.exists(path)), ("returned", "active", True))
        epoch = runtime().ownership_epoch
        check(L + "verify again from the same host and boot keeps the epoch",
              (refusal(lambda: control.verify_claim(Factory, 1, base_dir=base_dir))[0], runtime().ownership_epoch),
              ("returned", epoch))
    else:
        print("SKIP  verify/claim self-tests need Linux (flock on a local filesystem, prctl)")


with tempfile.TemporaryDirectory(prefix="um-control-") as directory:
    locks = os.path.join(directory, "locks")
    os.mkdir(locks, 0o700)
    engine = create_engine("sqlite:///" + os.path.join(directory, "t.db"), connect_args={"check_same_thread": False})
    scenario("sqlite", engine, locks)
    engine.dispose()

url = os.environ.get("MARIADB_TEST_URL", "").strip()
if url:
    maria = scratch.claim(url)
    try:
        with tempfile.TemporaryDirectory(prefix="um-control-m-") as directory:
            locks = os.path.join(directory, "locks")
            os.mkdir(locks, 0o700)
            scenario("mariadb", maria, locks)
    finally:
        scratch.release(maria)
elif os.environ.get("CI"):
    check("CI must provide MARIADB_TEST_URL (real MariaDB is mandatory in CI, never skipped)", False)
else:
    print("SKIP  real MariaDB execution (MARIADB_TEST_URL not set, not in CI)")

check("no network", _no_network.attempts, [])
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("all checks passed")
