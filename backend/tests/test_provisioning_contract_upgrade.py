"""Reviewed adapter-version upgrade of node contracts (42d -> c22), test first.

c22 changed the global adapter digest only because the WireGuard refund
helper was split; probe/read/create/absence/matcher semantics did not change.
Startup therefore invalidated every verified mikrotik_wg / threexui /
softether contract with reason adapter_version_changed. The proposed
app.services.provisioning_contract_upgrade recovers EXACTLY those rows,
EXACTLY that one reviewed source -> target digest pair, and nothing else:

  * only the real current digest == TARGET_VERSION, only rows whose stored
    adapter_version == SOURCE_VERSION, state 'invalidated', reason
    'adapter_version_changed', backend in (mikrotik_wg, threexui, softether),
    the node enabled and its live backend/configuration unchanged, a valid
    contract, a 64-lower-hex server fingerprint, original verifier/verified_at;
  * CAS on every observed field, version + 1, verified facts preserved,
    adapter_version -> TARGET, state ready, invalidation cleared;
  * one audit row per recovery in the SAME transaction (separate metadata,
    not one of the ten frozen provisioning tables), with a before-image and
    no secret; audit failure rolls the recovery back; a lost CAS writes
    nothing; a second run does nothing; a schema failure recovers nothing;
  * the existing guards are untouched: require_ready, p6 readiness and the
    real ChildGuard accept a recovered row only because it is now a normal
    current 'ready' row, and an in-flight version change is still refused.

SQLite always, real MariaDB when MARIADB_TEST_URL is set (mandatory in CI).
No node is contacted. Every check runs on its own, and a missing new module
is a FAIL per check (placeholder), never an import crash.
"""
import datetime as dt
import json
import os
import sys
import tempfile
import uuid
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DATABASE_URL", "sqlite://")
import _mariadb_scratch as scratch
import _no_network
_no_network.install([os.environ.get("MARIADB_TEST_URL", "")])

from fastapi import HTTPException
from sqlalchemy import create_engine, event, inspect, select, text
from sqlalchemy.orm import sessionmaker

from app import main, models, models_provisioning as mp
from app.services import gate_locks, provisioning_contract_rules as contract_rules
from app.services import provisioning_contracts as contracts, provisioning_readiness as readiness
from app.services import provisioning_schema, receipt_void_schema, resource_leases
from app.services import provisioning_installation as installation
from app.services import provisioning_dispatch_binding as fence
from app.services.provisioning_child_database import ChildDatabase
from app.services.provisioning_child_guard import ChildGuard, ChildGuardUnavailable
from app.services.provisioning_host import HostIdentity

try:
    from app.services import provisioning_contract_upgrade as up
except ImportError:          # base source: the recovery checks FAIL, the baseline guards still run
    up = None

SOURCE = "742e58935c84e385565c1468b06c6bdd3218e984"     # 42d digest stored in the invalidated rows
TARGET = "8342748d18b4c80457d28d1ef038d7389fef3d39"     # actual c22 global digest
SECRETS = ("SECRET-mt-password-never-audited", "SECRET-xr-password-never-audited",
           "SECRET-se-password-never-audited", "SECRET-ssh-password-never-audited")
failures = []


def check(label, got, expected=True):
    """`got` may be a callable: evaluated here, an exception is this check's
    FAIL (e.g. the not-yet-existing module), never an abort."""
    if callable(got):
        try:
            got = got()
        except Exception as exc:  # noqa: BLE001
            got = f"raised {type(exc).__name__}: {exc}"
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def recover(db):
    if up is None:
        raise RuntimeError("provisioning_contract_upgrade missing")
    return up.recover_reviewed(db)


def audit_table():
    if up is None:
        raise RuntimeError("provisioning_contract_upgrade missing")
    return up.UpgradeAudit.__table__


def audit_rows(db):
    if up is None:
        return []
    db.expire_all()
    return [dict(row) for row in db.execute(select(audit_table())).mappings().all()]


def row_of(db, node_id, backend):
    db.expire_all()
    return db.get(mp.ProvisioningNodeContract, (node_id, backend))


def facts(row):
    return (row.state, row.adapter_version, row.server_fingerprint, row.config_fingerprint, row.contract,
            row.verified_by_admin_id, row.verified_at, row.invalidated_at, row.invalidation_reason, row.version)


def secret_fields(node):
    for name, value in (("mt_password", SECRETS[0]), ("xr_panel_password", SECRETS[1]),
                        ("se_password", SECRETS[2]), ("xr_ssh_password", SECRETS[3])):
        if hasattr(node, name):
            setattr(node, name, value)


def make_node(db, kind, name, **fields):
    node_type = {"wg": models.NodeType.mikrotik, "se": models.NodeType.softether}.get(kind, models.NodeType.xray)
    values = dict(name=name, type=node_type, enabled=True)
    if kind == "wg":
        values.update(mt_host="192.0.2.10", mt_use_ssl=False, mt_wireguard_interface="wg0")
    elif kind == "se":
        values.update(se_host="192.0.2.12", se_hub_name="hub")
    else:
        values.update(xr_panel_mode=kind, xr_panel_base_url="https://example.invalid/", xr_inbound_tag="in",
                      xr_panel_inbound_id=1, xr_ssh_host="192.0.2.11")
    values.update(fields)
    node = models.Node(**values)
    secret_fields(node)
    db.add(node)
    db.flush()
    return node


def invalidated(db, node, backend, *, adapter=SOURCE, reason="adapter_version_changed", state="invalidated",
                server="b" * 64, contract='{"not_exist":[]}', verifier=7, config=None):
    now = dt.datetime(2026, 10, 1, 12, 0, 0)
    row = mp.ProvisioningNodeContract(
        node_id=node.id, backend=backend, state=state, adapter_version=adapter, server_fingerprint=server,
        config_fingerprint=config or contracts.config_fingerprint(node), contract=contract,
        verified_by_admin_id=verifier, verified_at=now,
        invalidated_at=now + dt.timedelta(days=1) if state == "invalidated" else None,
        invalidation_reason=reason if state == "invalidated" else None, version=3)
    db.add(row)
    db.flush()
    return row


def guard_fixture(engine, Factory, directory, node_id):
    """The real ChildGuard prerequisites for ONE WireGuard node, exactly as
    test_provisioning_dispatch_binding.py builds them (unit fixture)."""
    db = Factory()
    installation_uuid = db.get(mp.ProvisioningRuntimeState, 1).installation_uuid
    db.rollback()
    exclusive = gate_locks.FileLock(gate_locks.mode_lock_path(installation_uuid, directory)).acquire(shared=False, timeout=0)
    installation.initialize_off(engine, installation_uuid, exclusive, base_dir=directory)
    exclusive.release()
    mode_hold = gate_locks.FileLock(gate_locks.mode_lock_path(installation_uuid, directory)).acquire(shared=True, timeout=0)
    identity = HostIdentity("a" * 64, str(uuid.uuid4()))
    runtime = db.get(mp.ProvisioningRuntimeState, 1)
    runtime.owner_state, runtime.owner_host_id, runtime.owner_boot_id = "active", identity.host_id, identity.boot_id
    runtime.owner_claimed_at = runtime.owner_heartbeat_at = dt.datetime.utcnow()
    runtime.ownership_epoch, runtime.gate_mode, runtime.gate_mode_epoch = 1, "enforced", 1
    runtime.lock_backend = "flock" if engine.dialect.name == "sqlite" else "flock+get_lock"
    runtime.lock_verified_at = dt.datetime.utcnow()
    db.get(mp.ProvisioningTypeMode, "create_user").mode = "durable"
    db.commit()
    node = db.get(models.Node, node_id)
    saved = dict(schema_version=1, user=dict(username="upgrade-customer", owner_admin_id=None, telegram_id=42),
                 node_fingerprints={str(node.id): contracts.config_fingerprint(node)})
    op = mp.ProvisioningOperation(operation_type="create_user", business_key="upgrade-binding", request_hash="a" * 64,
        tenant_scope_key="global", actor_kind="system", intent=json.dumps(saved), username_claim="upgrade-customer",
        state="provisioning", forward_deadline=dt.datetime.utcnow() + dt.timedelta(minutes=5), wallet_epoch_at_start=0)
    db.add(op)
    db.flush()
    step = mp.ProvisioningStep(operation_id=op.id, slot_key="main", direction="create", step_order=0,
        backend="mikrotik_wg", node_id=node.id, protocol="wireguard", state="remote_calling", remote_attempted=True,
        wg_interface="wg0", staged_wg_private_key="private-never-selected")
    db.add(step)
    db.commit()
    lease = resource_leases.acquire(db, f"provisioning_op:{op.id}", f"op:{op.id}:1", ttl=300)
    db.commit()
    request = fence.DispatchBinding(installation_uuid, 1, 1, op.id, op.version, step.id, step.version,
                                    node.id, "mikrotik_wg", lease.owner, lease.epoch)
    node_hold = gate_locks.FileLock(gate_locks.node_lock_path(installation_uuid, node.id, directory)).acquire(
        shared=False, timeout=0)
    db.close()
    return ChildDatabase(engine.url), request, identity, mode_hold, node_hold


def guard_enters(args, directory):
    reader, request, identity, mode_hold, node_hold = args
    try:
        with ChildGuard(reader, request, identity, mode_hold, node_hold, base_dir=directory) as guard:
            guard.check()
        return True
    except ChildGuardUnavailable:
        return False


def require_ready_outcome(db, node_id, backend):
    db.expire_all()
    try:
        contracts.require_ready(db, db.get(models.Node, node_id), backend)
        return "ready"
    except HTTPException as exc:
        return exc.detail
    finally:
        db.rollback()


def scenario(label, engine, directory):
    L = f"[{label}] "
    models.Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine, autoflush=False)
    check(L + "receipt void schema ready", lambda: receipt_void_schema.bootstrap(engine, Factory)["ready"])
    check(L + "provisioning schema ready", lambda: provisioning_schema.bootstrap(engine, Factory)["ready"])

    # ---- baseline guards (pass on the base source too)
    check(L + "actual current global adapter digest is the reviewed c22 target",
          contract_rules.adapter_version(), TARGET)
    check(L + "the eight hashed adapter files are unchanged",
          contract_rules.ADAPTER_FILES, ("adapter_base.py", "adapter_mikrotik_wg.py", "adapter_radius_ppp.py",
           "adapter_softether.py", "adapter_xray.py", "adapter_xray_panels.py", "provisioning_contracts.py",
           "provisioning_contract_rules.py"))
    check(L + "the ten frozen provisioning tables are unchanged", len(mp.PROVISIONING_TABLES), 10)

    # ---- module surface
    check(L + "SOURCE_VERSION is exactly the reviewed 42d digest", lambda: up.SOURCE_VERSION, SOURCE)
    check(L + "TARGET_VERSION is exactly the c22 digest", lambda: up.TARGET_VERSION, TARGET)
    check(L + "the recovery module is not one of the hashed adapter files",
          lambda: os.path.basename(up.__file__) in contract_rules.ADAPTER_FILES, False)
    check(L + "audit table on its own metadata, not a frozen provisioning table",
          lambda: (audit_table().metadata is not mp.PROVISIONING_METADATA,
                   audit_table().metadata is not models.Base.metadata,
                   audit_table() not in mp.PROVISIONING_TABLES), (True, True, True))
    check(L + "bootstrap creates and verifies the additive audit table (no problems)",
          lambda: up.bootstrap(engine), [])
    check(L + "...the live audit table inspects clean",
          lambda: provisioning_schema.inspect_schema(engine, tables=[audit_table()]), [])
    check(L + "...a second bootstrap is idempotent", lambda: up.bootstrap(engine), [])

    # ---- fixture: eligible rows on the three allowed backends + every refusal case
    with Factory() as db:
        good = {"wg": make_node(db, "wg", "good-wg"), "3xui": make_node(db, "3xui", "good-3xui"),
                "se": make_node(db, "se", "good-se")}
        invalidated(db, good["wg"], "mikrotik_wg")
        invalidated(db, good["3xui"], "threexui")
        invalidated(db, good["se"], "softether")
        bad = {}

        def bad_case(name, kind, backend, **row):
            node = make_node(db, kind, "bad-" + name)
            bad[name] = (node.id, backend)
            invalidated(db, node, backend, **row)

        bad_case("unknown_source", "wg", "mikrotik_wg", adapter="1" * 40)
        bad_case("current_source", "wg", "mikrotik_wg", adapter=TARGET)
        bad_case("manual_reason", "wg", "mikrotik_wg", reason="manual")
        bad_case("config_reason", "se", "softether", reason="config_changed")
        bad_case("invalid_contract", "3xui", "threexui", contract="[]")
        bad_case("invalid_matcher", "wg", "mikrotik_wg",
                 contract='{"not_exist":[{"kind":"http","status":404,"json_path":"detail","equals":"x"}]}')
        bad_case("upper_server_hex", "wg", "mikrotik_wg", server="B" * 64)
        bad_case("short_server", "se", "softether", server="b" * 63 + " ")
        bad_case("zero_verifier", "3xui", "threexui", verifier=0)
        bad_case("negative_verifier", "wg", "mikrotik_wg", verifier=-1)
        bad_case("unsupported_marzban", "marzban", "marzban")
        bad_case("unsupported_ssh", "ssh", "xray_ssh")
        bad_case("stale_config", "wg", "mikrotik_wg", config="0" * 64)
        bad_case("ready_old", "wg", "mikrotik_wg", state="ready")
        disabled = make_node(db, "wg", "bad-disabled", enabled=False)
        bad["disabled"] = (disabled.id, "mikrotik_wg")
        invalidated(db, disabled, "mikrotik_wg")
        moved = make_node(db, "3xui", "bad-backend-moved")
        bad["backend_moved"] = (moved.id, "threexui")
        invalidated(db, moved, "threexui")
        moved.xr_panel_mode = "ssh"                       # live backend is now xray_ssh
        orphan = make_node(db, "wg", "bad-orphan")
        bad["orphan"] = (orphan.id, "mikrotik_wg")
        invalidated(db, orphan, "mikrotik_wg")
        db.delete(orphan)
        db.commit()
        good_ids = {key: (node.id, backend) for (key, node), backend in
                    zip(good.items(), ("mikrotik_wg", "threexui", "softether"))}
        before = {key: facts(row_of(db, *value)) for key, value in {**good_ids, **bad}.items()}

    wg_id = good_ids["wg"][0]
    args = guard_fixture(engine, Factory, directory, wg_id)

    # ---- before recovery: every existing guard refuses
    with Factory() as db:
        check(L + "before: require_ready refuses the invalidated WG row",
              require_ready_outcome(db, wg_id, "mikrotik_wg"), "node_contract_not_ready")
        check(L + "before: readiness reports it invalidated",
              lambda: [b for b in readiness.node_contract_blockers(db) if b["node_id"] == wg_id],
              [{"node_id": wg_id, "backend": "mikrotik_wg", "reason": "invalidated"}])
    check(L + "before: the real ChildGuard refuses", guard_enters(args, directory), False)

    # ---- a future/unknown CURRENT digest recovers nothing
    with Factory() as db, patch.object(contract_rules, "adapter_version", lambda: "f" * 40), \
            patch.object(contracts, "adapter_version", lambda: "f" * 40):
        check(L + "current digest not the reviewed target: nothing recovered", lambda: recover(db), [])
        db.rollback()
        check(L + "...no row changed", {k: facts(row_of(db, *v)) for k, v in good_ids.items()},
              {k: before[k] for k in good_ids})
        check(L + "...no audit row", len(audit_rows(db)), 0)

    # ---- the caller commits: an uncommitted recovery rolls back
    with Factory() as db:
        check(L + "recover returns the three eligible rows in node order",
              lambda: recover(db), sorted(good_ids.values()))
        db.rollback()
        check(L + "rolled back by the caller: still invalidated",
              {k: facts(row_of(db, *v)) for k, v in good_ids.items()}, {k: before[k] for k in good_ids})
        check(L + "...and no audit row", len(audit_rows(db)), 0)

    # ---- an audit insert failure rolls the recovery back
    audit_attempts = {"count": 0}

    def fail_audit(conn, cursor, statement, parameters, context, many):
        if up is not None and statement.lstrip().upper().startswith("INSERT") and audit_table().name in statement:
            audit_attempts["count"] += 1
            if audit_attempts["count"] == 2:
                raise RuntimeError("injected second audit failure")

    event.listen(engine, "before_cursor_execute", fail_audit)
    with Factory() as db:
        outcome = "no error"
        try:
            recover(db)
            db.commit()
        except Exception as exc:  # noqa: BLE001
            outcome = "raised"
            db.rollback()
        check(L + "audit failure surfaces (no silent recovery)", outcome, "raised")
        check(L + "audit failure occurs after an earlier recovery and audit", audit_attempts["count"], 2)
    event.remove(engine, "before_cursor_execute", fail_audit)
    with Factory() as db:
        check(L + "...and nothing was recovered", {k: facts(row_of(db, *v)) for k, v in good_ids.items()},
              {k: before[k] for k in good_ids})
        check(L + "...no audit row", len(audit_rows(db)), 0)

    # ---- a lost CAS: a concurrent writer changes the softether row right
    # before ITS own CAS UPDATE (same connection, so no lock can hide it).
    bumped = {"done": False}

    def concurrent_writer(conn, cursor, statement, parameters, context, many):
        values = list(parameters.values()) if isinstance(parameters, dict) else list(parameters or ())
        if (not bumped["done"] and statement.lstrip().upper().startswith("UPDATE")
                and "provisioning_node_contracts" in statement
                and ("softether" in values or b"softether" in values or "'softether'" in statement)):
            bumped["done"] = True
            cursor.execute(f"UPDATE provisioning_node_contracts SET version = version + 7 "
                           f"WHERE node_id = {int(good_ids['se'][0])} AND backend = 'softether'")

    with Factory() as db:
        event.listen(engine, "before_cursor_execute", concurrent_writer)
        try:
            result = "no result"
            try:
                result = recover(db)
                db.commit()
            except Exception as exc:  # noqa: BLE001
                result = f"raised {type(exc).__name__}"
                db.rollback()
        finally:
            event.remove(engine, "before_cursor_execute", concurrent_writer)
        check(L + "lost CAS: the injected concurrent write really happened", bumped["done"], True)
        check(L + "lost CAS: only the untouched rows are reported recovered",
              result, sorted(v for k, v in good_ids.items() if k != "se"))
    with Factory() as db:
        se = row_of(db, *good_ids["se"])
        check(L + "lost CAS: the changed row was not updated (still invalidated, only the concurrent bump)",
              facts(se), before["se"][:9] + (before["se"][9] + 7,))
        check(L + "lost CAS: no audit row for it",
              [(a.get("node_id"), a.get("backend")) for a in audit_rows(db)].count(good_ids["se"]), 0)
        check(L + "lost CAS: the others are recovered with their audit",
              sorted((a.get("node_id"), a.get("backend")) for a in audit_rows(db)),
              sorted(v for k, v in good_ids.items() if k != "se"))
        before["se"] = facts(se)        # its new committed state is what the next recovery sees

    # ---- the real recovery: the row whose CAS was lost is eligible again
    with Factory() as db:
        check(L + "recovery: the remaining eligible row", lambda: recover(db), [good_ids["se"]])
        db.commit()
    with Factory() as db:
        for key, (node_id, backend) in good_ids.items():
            row = row_of(db, node_id, backend)
            old = before[key]
            check(L + f"{backend}: ready, target digest, invalidation cleared, version + 1",
                  (row.state, row.adapter_version, row.invalidated_at, row.invalidation_reason, row.version),
                  ("ready", TARGET, None, None, old[9] + 1))
            check(L + f"{backend}: verified facts, contract and fingerprints preserved",
                  (row.server_fingerprint, row.config_fingerprint, row.contract, row.verified_by_admin_id,
                   row.verified_at), (old[2], old[3], old[4], old[5], old[6]))
        for key, value in bad.items():
            if key == "backend_moved" or key == "ready_old":
                continue
            check(L + f"refused stays exactly as it was: {key}", facts(row_of(db, *value)), before[key])
        check(L + "refused: an old-digest READY row is not touched by recovery",
              facts(row_of(db, *bad["ready_old"])), before["ready_old"])
        check(L + "refused: a node whose live backend moved stays invalidated",
              row_of(db, *bad["backend_moved"]).state, "invalidated")

        audits = audit_rows(db)
        check(L + "one audit row per recovered row", sorted((a.get("node_id"), a.get("backend")) for a in audits),
              sorted(good_ids.values()))
        for audit in audits:
            key = next(k for k, v in good_ids.items() if v == (audit.get("node_id"), audit.get("backend")))
            check(L + f"audit {audit.get('backend')}: versions and row versions",
                  (audit.get("source_version"), audit.get("target_version"), audit.get("row_version_before"),
                   audit.get("row_version_after"), audit.get("recovered_at") is not None),
                  (SOURCE, TARGET, before[key][9], before[key][9] + 1, True))
            image = json.loads(audit.get("before_image") or "{}")
            check(L + f"audit {audit.get('backend')}: before-image of the invalidated row",
                  (image.get("state"), image.get("adapter_version"), image.get("invalidation_reason"),
                   image.get("version"), image.get("server_fingerprint"), image.get("config_fingerprint")),
                  ("invalidated", SOURCE, "adapter_version_changed", before[key][9], before[key][2], before[key][3]))
            check(L + f"audit {audit.get('backend')}: no secret anywhere",
                  any(secret in json.dumps(audit, default=str) for secret in SECRETS), False)
            check(L + f"audit {audit.get('backend')}: full original verification evidence",
                  (image.get("contract"), image.get("verified_by_admin_id"),
                   dt.datetime.fromisoformat(image.get("verified_at", "")),
                   dt.datetime.fromisoformat(image.get("invalidated_at", ""))),
                  (before[key][4], before[key][5], before[key][6], before[key][7]))

    # ---- the unchanged guards now accept the recovered rows, and only them
    with Factory() as db:
        check(L + "after: require_ready accepts the recovered WG row", require_ready_outcome(db, wg_id, "mikrotik_wg"),
              "ready")
        check(L + "after: require_ready still refuses a refused row",
              require_ready_outcome(db, *bad["manual_reason"]), "node_contract_not_ready")
        blockers = readiness.node_contract_blockers(db)
        check(L + "after: readiness has no blocker for the recovered nodes",
              [b for b in blockers if b["node_id"] in {v[0] for v in good_ids.values()}], [])
        check(L + "after: readiness still blocks the refused enabled nodes",
              {b["node_id"] for b in blockers} >= {bad[k][0] for k in ("manual_reason", "unknown_source")}, True)
    check(L + "after: the real ChildGuard accepts the recovered row", guard_enters(args, directory), True)
    reader, request, identity, mode_hold, node_hold = args
    with Factory() as db:
        try:
            with ChildGuard(reader, request, identity, mode_hold, node_hold, base_dir=directory) as guard:
                guard.check()
                db.execute(text("UPDATE provisioning_node_contracts SET version = version + 1 "
                                "WHERE node_id = :n AND backend = 'mikrotik_wg'"), dict(n=wg_id))
                db.commit()
                guard.check()
            refused = False
        except ChildGuardUnavailable:
            refused = True
        check(L + "in-flight contract version change is still refused by the real ChildGuard", refused, True)

    # ---- idempotent: a second run does nothing, no new audit
    with Factory() as db:
        count = len(audit_rows(db))
        check(L + "second recovery: nothing", lambda: recover(db), [])
        db.commit()
        check(L + "...no new audit row", len(audit_rows(db)), count)

    # A version-only CAS is insufficient: another legitimate writer can
    # change provenance without bumping the version. Compare every fact.
    with Factory() as db:
        extra = make_node(db, "wg", "provenance-cas", id=1000)
        extra_id = extra.id
        invalidated(db, extra, "mikrotik_wg")
        db.commit()
        original = facts(row_of(db, extra_id, "mikrotik_wg"))
    changed_provenance = {"done": False}

    def provenance_writer(conn, cursor, statement, parameters, context, many):
        if (not changed_provenance["done"] and statement.lstrip().upper().startswith("UPDATE")
                and "provisioning_node_contracts" in statement):
            changed_provenance["done"] = True
            cursor.execute("UPDATE provisioning_node_contracts SET verified_by_admin_id = 8 "
                           f"WHERE node_id = {int(extra_id)} AND backend = 'mikrotik_wg'")

    with Factory() as db:
        event.listen(engine, "before_cursor_execute", provenance_writer)
        try:
            check(L + "CAS on provenance: no recovery without a version bump", lambda: recover(db), [])
            db.commit()
        finally:
            event.remove(engine, "before_cursor_execute", provenance_writer)
        check(L + "CAS on provenance: competing write occurred", changed_provenance["done"])
        check(L + "CAS on provenance: preserve competing write, remain invalidated",
              facts(row_of(db, extra_id, "mikrotik_wg")), original[:5] + (8,) + original[6:])
        check(L + "CAS on provenance: no audit for unrecovered row",
              [a for a in audit_rows(db) if a["node_id"] == extra_id], [])
        # Retire this local fixture so the next fault targets only text CAS.
        db.execute(text("UPDATE provisioning_node_contracts SET invalidation_reason = 'manual' "
                        "WHERE node_id = :n"), {"n": extra_id})
        db.commit()

    # MariaDB commonly uses a case-insensitive collation. Matcher values
    # still have case-sensitive meaning: the CAS must compare exact bytes.
    original_contract = json.dumps({"not_exist": [{"kind": "http", "op": "read", "status": 404,
        "json_path": "detail", "equals": "missing"}]}, separators=(",", ":"))
    replacement_contract = original_contract.replace('"missing"', '"MISSING"')
    with Factory() as db:
        extra = make_node(db, "3xui", "text-cas", id=1001)
        invalidated(db, extra, "threexui", contract=original_contract)
        db.commit()
        text_before = facts(row_of(db, 1001, "threexui"))
    changed_text = {"done": False}

    def text_writer(conn, cursor, statement, parameters, context, many):
        if (not changed_text["done"] and statement.lstrip().upper().startswith("UPDATE")
                and "provisioning_node_contracts" in statement):
            changed_text["done"] = True
            # The values are fixed, local non-secret test fixtures.
            cursor.execute("UPDATE provisioning_node_contracts SET contract = '" + replacement_contract
                           + "' WHERE node_id = 1001 AND backend = 'threexui'")

    with Factory() as db:
        event.listen(engine, "before_cursor_execute", text_writer)
        try:
            check(L + "CAS on text: case-only matcher change is not equivalent", lambda: recover(db), [])
            db.commit()
        finally:
            event.remove(engine, "before_cursor_execute", text_writer)
        check(L + "CAS on text: competing write occurred", changed_text["done"])
        check(L + "CAS on text: preserve changed matcher, remain invalidated",
              facts(row_of(db, 1001, "threexui")), text_before[:4] + (replacement_contract,) + text_before[5:])
        check(L + "CAS on text: no audit for unrecovered row",
              [a for a in audit_rows(db) if a["node_id"] == 1001], [])

    for hold in args[3:]:
        hold.release()
    args[0]._engine.dispose()


def startup_scenario(label, engine, *, broken_audit):
    """The real startup hook (main._bootstrap_provisioning_schema): audit
    bootstrap, then invalidate_stale and recover_reviewed in ONE transaction.
    A broken audit schema recovers nothing and never raises."""
    L = f"[{label} startup{' broken-audit' if broken_audit else ''}] "
    models.Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine, autoflush=False)
    provisioning_schema.bootstrap(engine, Factory)
    if broken_audit and up is not None:
        with engine.begin() as conn:
            conn.execute(text(f"CREATE TABLE {audit_table().name} (id INTEGER PRIMARY KEY)"))
    with Factory() as db:
        node = make_node(db, "wg", "startup-wg")
        # Stored READY under the old digest: startup must first invalidate it,
        # then recover it in the same transaction.
        invalidated(db, node, "mikrotik_wg", state="ready")
        db.commit()
        node_id = node.id
    saved = (main.engine, main.SessionLocal)
    main.engine, main.SessionLocal = engine, Factory
    raised = None
    try:
        for _ in range(2):           # a repeated startup is idempotent
            try:
                main._bootstrap_provisioning_schema()
            except Exception as exc:  # noqa: BLE001
                raised = repr(exc)
    finally:
        main.engine, main.SessionLocal = saved
    check(L + "startup never raises", raised, None)
    with Factory() as db:
        row = row_of(db, node_id, "mikrotik_wg")
        if broken_audit:
            check(L + "audit schema problem: bootstrap reports it", lambda: up.bootstrap(engine) != [], True)
            check(L + "...and nothing is recovered (still invalidated by the existing check)",
                  (row.state, row.adapter_version, row.invalidation_reason),
                  ("invalidated", SOURCE, "adapter_version_changed"))
        else:
            check(L + "old-digest ready row: invalidated then recovered at startup",
                  (row.state, row.adapter_version, row.invalidated_at, row.invalidation_reason),
                  ("ready", TARGET, None, None))
            check(L + "...exactly one audit row across two startups",
                  [(a.get("node_id"), a.get("backend")) for a in audit_rows(db)], [(node_id, "mikrotik_wg")])
            check(L + "...and require_ready accepts it", require_ready_outcome(db, node_id, "mikrotik_wg"), "ready")


with tempfile.TemporaryDirectory(prefix="um-contract-upgrade-") as directory:
    sqlite = create_engine("sqlite:///" + os.path.join(directory, "t.db"), connect_args={"check_same_thread": False})
    scenario("sqlite", sqlite, directory)
    sqlite.dispose()
    for broken in (False, True):
        startup = create_engine("sqlite:///" + os.path.join(directory, f"s{int(broken)}.db"),
                                connect_args={"check_same_thread": False})
        startup_scenario("sqlite", startup, broken_audit=broken)
        startup.dispose()

    url = os.environ.get("MARIADB_TEST_URL", "").strip()
    if url:
        maria = scratch.claim(url)
        try:
            maria_directory = os.path.join(directory, "mariadb-locks")
            os.mkdir(maria_directory)
            scenario("mariadb", maria, maria_directory)
        finally:
            scratch.release(maria)
        for broken in (False, True):
            maria = scratch.claim(url)
            try:
                startup_scenario("mariadb", maria, broken_audit=broken)
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
