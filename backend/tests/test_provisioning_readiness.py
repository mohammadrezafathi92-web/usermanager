"""p6_ready() (Lifecycle 7.7) is read-only and true only when every condition
holds; the status endpoint never reports P6 ready while a known
implementation gap remains.

Covers LP-108 (shadow approval left mutating with a completed operation, an
open approval_recovery of a shadow approval, a non-terminal operation of a
shadow approval: each false with the items, true after closing them) and
LP-125 (one enabled node of each of the six Xray panel modes, a MikroTik and
a SoftEther node, all 'ready': true; one 'unverified': false with that node).
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

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app import models, models_provisioning as mp, models_receipt_void as rv
from app.services import bot_auth, provisioning_contracts as contracts, provisioning_readiness as readiness
from app.services import provisioning_schema, receipt_approval_intent as ri, receipt_approval_registration as registration
from app.services import receipt_void_schema
from app.services.provisioning_host import HostIdentity

failures = []


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def codes(blockers):
    return sorted(item["code"] for item in blockers)


def scenario(label, engine):
    L = f"[{label}] "
    models.Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine, autoflush=False)
    check(L + "receipt void schema ready", receipt_void_schema.bootstrap(engine, Factory)["ready"])
    check(L + "provisioning schema ready", provisioning_schema.bootstrap(engine, Factory)["ready"])
    identity = HostIdentity("b" * 64, str(uuid.uuid4()))
    other = HostIdentity("c" * 64, str(uuid.uuid4()))

    with Factory() as db:
        ready, blockers = readiness.p6_ready(db, identity)
        check(L + "fresh install: not ready", ready, False)
        check(L + "fresh install: every type legacy, gate off, no owner",
              codes(blockers), ["gate_not_enforced", "operation_types_not_durable", "owner_not_this_host",
                                "ownership_not_active"])
        check(L + "the legacy list names all eight operation types",
              next(item["items"] for item in blockers if item["code"] == "operation_types_not_durable"),
              sorted(mp.OPERATION_TYPES))

    statements = []

    def record(conn, cursor, statement, parameters, context, many):
        statements.append(statement)

    with Factory() as db:
        db.add(models.PanelSettings(id=1))
        nodes = [models.Node(name="mt", type=models.NodeType.mikrotik, enabled=True, mt_host="10.0.0.1",
                             mt_wireguard_interface="wg0"),
                 models.Node(name="se", type=models.NodeType.softether, enabled=True, se_host="10.0.0.2",
                             se_hub_name="HUB")]
        for mode in ("ssh", "3xui", "marzban", "hiddify", "marzneshin", "sui"):
            nodes.append(models.Node(name=f"x-{mode}", type=models.NodeType.xray, enabled=True,
                                     xr_panel_mode=mode, xr_inbound_tag="in", xr_panel_inbound_id=1))
        nodes.append(models.Node(name="off", type=models.NodeType.xray, enabled=False, xr_panel_mode="ssh"))
        db.add_all(nodes)
        db.flush()
        now = dt.datetime.utcnow()
        for node in nodes[:-1]:
            backend = readiness._node_backend(node)
            db.add(mp.ProvisioningNodeContract(node_id=node.id, backend=backend, state="ready",
                adapter_version=contracts.adapter_version(), server_fingerprint="s" * 64,
                config_fingerprint=contracts.config_fingerprint(node), contract='{"not_exist": []}',
                verified_by_admin_id=1, verified_at=now))
        for row in db.query(mp.ProvisioningTypeMode):
            row.mode = "durable"
        host = db.get(mp.ProvisioningRuntimeState, 1)
        host.owner_state, host.owner_host_id, host.owner_boot_id = "active", identity.host_id, identity.boot_id
        host.owner_claimed_at = host.owner_heartbeat_at = now
        host.ownership_epoch, host.lock_backend, host.lock_verified_at, host.gate_mode = 1, "flock", now, "enforced"
        db.commit()
        node_ids = {node.name: node.id for node in nodes}

    event.listen(engine, "before_cursor_execute", record)
    with Factory() as db:
        ready, blockers = readiness.p6_ready(db, identity)
    event.remove(engine, "before_cursor_execute", record)
    check(L + "LP-125 all six Xray modes, MikroTik and SoftEther ready (disabled node ignored): ready",
          (ready, blockers), (True, []))
    check(L + "the predicate only reads", all(s.lstrip().upper().startswith("SELECT") for s in statements) and
          bool(statements), True)
    with Factory() as db:
        ready, blockers = readiness.p6_ready(db, other)
        check(L + "another host is not the owner", (ready, codes(blockers)), (False, ["owner_not_this_host"]))

    # One node becomes unverified; another's configuration changes after verification.
    with Factory() as db:
        db.query(mp.ProvisioningNodeContract).filter_by(node_id=node_ids["x-marzban"]).delete()
        db.get(models.Node, node_ids["x-sui"]).xr_inbound_tag = "changed"
        db.commit()
        ready, blockers = readiness.p6_ready(db, identity)
        check(L + "LP-125 an unverified node and a changed node block, by name",
              (ready, codes(blockers), next(item["items"] for item in blockers)),
              (False, ["node_contract_not_ready"],
               [{"node_id": node_ids["x-marzban"], "backend": "marzban", "reason": "unverified"},
                {"node_id": node_ids["x-sui"], "backend": "sui", "reason": "config_changed"}]))
        db.get(models.Node, node_ids["x-marzban"]).enabled = False
        db.get(models.Node, node_ids["x-sui"]).enabled = False
        db.commit()
        check(L + "disabling those two nodes clears the blocker", readiness.p6_ready(db, identity), (True, []))

    # LP-108: shadow approvals.
    principal = bot_auth.BotPrincipal.internal(None)
    with Factory() as db:
        package = models.Package(name="P", quota_gb=1, duration_days=30, price=100)
        db.add(package)
        db.commit()
        approvals = [registration.register(db, principal, ri.ApprovalIntent(
            pending_source_instance_id=f"ready-{label}", pending_local_id=n, kind="new",
            target_username=f"ready-{n}", amount=100, package_id=package.id, claimed_telegram_id=20000 + n),
            approval_mode="auto") for n in (1, 2, 3)]
        uuids = [approval.approval_uuid for approval in approvals]
        for value in uuids:
            db.execute(rv.receipt_approvals.update().where(rv.receipt_approvals.c.approval_uuid == value)
                       .values(registered_under_mode="shadow", state="mutating", mutating_at=dt.datetime.utcnow()))

        def operation(value, state):
            done = state in ("completed", "compensated")
            db.add(mp.ProvisioningOperation(operation_type="create_user", business_key=f"approval:{value}:v:0",
                request_hash="0" * 64, tenant_scope_key="shared", actor_kind="system", intent="{}", state=state,
                approval_uuid=value, approval_execution_version=0, approval_key_bound=False,
                forward_deadline=dt.datetime.utcnow(), wallet_epoch_at_start=0,
                completed_at=dt.datetime.utcnow() if done else None,
                username_claim=None if done else f"claim-{value[:8]}"))

        operation(uuids[0], "completed")
        operation(uuids[1], "provisioning")
        db.execute(rv.wallet_operations.insert().values(
            operation_type="approval_recovery", business_key=uuids[2], request_hash="0" * 64,
            tenant_scope_key="shared", actor_kind="system", state="cleanup_required", epoch_at_start=0,
            result_snapshot="{}", created_at=dt.datetime.utcnow()))
        db.commit()
        ready, blockers = readiness.p6_ready(db, identity)
        check(L + "LP-108 three shadow leftovers: not ready", (ready, codes(blockers)),
              (False, ["shadow_approval_mutating", "shadow_approval_recovery_open", "shadow_operation_open"]))
        by_code = {item["code"]: item["items"] for item in blockers}
        check(L + "each blocker names its approval",
              (by_code["shadow_approval_mutating"], [i["approval_uuid"] for i in by_code["shadow_operation_open"]],
               [i["approval_uuid"] for i in by_code["shadow_approval_recovery_open"]]),
              ([uuids[0]], [uuids[1]], [uuids[2]]))
        # Close them the way approval_recovery would.
        db.execute(rv.receipt_approvals.update().where(rv.receipt_approvals.c.approval_uuid == uuids[0])
                   .values(state="completed", completed_at=dt.datetime.utcnow()))
        db.query(mp.ProvisioningOperation).filter_by(approval_uuid=uuids[1]).update(
            {"state": "compensated", "completed_at": dt.datetime.utcnow(), "username_claim": None})
        db.execute(rv.wallet_operations.update().values(state="completed"))
        db.commit()
        check(L + "LP-108 after recovery closes all three: ready", readiness.p6_ready(db, identity), (True, []))


with tempfile.TemporaryDirectory(prefix="um-readiness-") as directory:
    sqlite = create_engine("sqlite:///" + os.path.join(directory, "t.db"), connect_args={"check_same_thread": False})
    scenario("sqlite", sqlite)
    sqlite.dispose()

url = os.environ.get("MARIADB_TEST_URL", "").strip()
if url:
    maria = scratch.claim(url)
    try:
        scenario("mariadb", maria)
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
