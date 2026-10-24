"""Licence cuts: real temporary DB, fake gated runner only, no network.

Logical customer state is preserved. Restoration is explicit and must
never override exhausted/disabled/session-limited service state.
"""
import datetime as dt
import contextlib
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from unittest.mock import MagicMock, patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("DATABASE_URL", "sqlite://")
import _no_network
_no_network.install([])
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app import models, models_provisioning as mp
from app.services import license_guard, license_fence, licensing, quota_manager as qm
from app.services import provisioning_schema, receipt_void_schema
from app.services import radius_server as radius

checks = 0


def check(label, actual, expected=True):
    global checks
    assert actual == expected, f"{label}: {actual!r} != {expected!r}"
    checks += 1
    print("PASS", label)


with tempfile.TemporaryDirectory(prefix="license-cuts-") as temp:
    path = Path(temp) / "test.db"
    engine = create_engine("sqlite:///" + str(path))
    models.Base.metadata.create_all(engine)
    mp.ProvisioningOperation.__table__.create(engine)
    Session = sessionmaker(bind=engine)
    os.environ["UM_LOCK_DIR"] = str(Path(temp) / "locks")
    license_fence.install(engine)
    with Session() as db:
        user = models.User(username="test", status=models.UserStatus.active, total_quota_bytes=10000,
                           used_bytes=100, balance=123)
        nodes = [models.Node(name=str(i), type=kind, enabled=True)
                 for i, kind in enumerate((models.NodeType.mikrotik, models.NodeType.xray, models.NodeType.softether))]
        db.add_all([user, *nodes]); db.flush()
        types = [(models.ConnectionType.wireguard, nodes[0]), (models.ConnectionType.openvpn, nodes[0]),
                 (models.ConnectionType.xray, nodes[1]), (models.ConnectionType.softether, nodes[2])]
        ids = []
        for kind, node in types:
            c = models.Connection(user_id=user.id, node_id=node.id, type=kind, enabled=True,
                                  ppp_username="fixture", ppp_password="fixture", wg_peer_name="fixture",
                                  xr_email="fixture", xr_uuid="fixture")
            db.add(c); db.flush(); ids.append(c.id)
        disabled = models.Connection(user_id=user.id, node_id=nodes[0].id, type=models.ConnectionType.wireguard, enabled=False)
        db.add(disabled); db.commit()
        disabled_id, uid = disabled.id, user.id

    cut = licensing.Enforcement(lock_panel=True, lock_bot=True, cut_services=True, reason=licensing.REASON_EXPIRED)
    active = licensing.Enforcement()
    verdict = [cut]
    calls, fail = [], [False]
    # Short licence actions must use the killable child, never an inline
    # fallback. Ordinary callers keep their existing 120-second default.
    from app.services import legacy_remote, node_gate, remote_runner
    from app.services.remote_action import Outcome
    from app.services.transport_guard import GateViolation
    with Session() as db:
        node = db.get(models.Node, db.get(models.Connection, ids[0]).node_id)
        db.expunge(node)
    owner = {"installation_uuid": "12345678-1234-1234-1234-123456789abc", "ownership_epoch": 1, "gate_mode_epoch": 1}
    passage = node_gate.EnforcedPassage(node.id, owner["installation_uuid"], 1)
    with patch.object(node_gate, "writer_passage", side_effect=lambda n: contextlib.nullcontext(passage)), \
            patch.object(legacy_remote, "_owner_snapshot", return_value=owner), \
            patch.object(remote_runner, "run_action", return_value=SimpleNamespace(
                outcome=Outcome.SUCCEEDED, observed={"value": True})) as runner:
        legacy_remote.run(node, "wg_set_enabled", peer_name="fake", enabled=True, hard_deadline_seconds=3)
        check("licence child deadline is bounded", runner.call_args.args[0].timeouts["hard_deadline"], 3)
        legacy_remote.run(node, "wg_set_enabled", peer_name="fake", enabled=True)
        check("ordinary child deadline unchanged", runner.call_args.args[0].timeouts["hard_deadline"], 120)
    with patch.object(node_gate, "writer_passage", side_effect=lambda n: contextlib.nullcontext(None)):
        try:
            legacy_remote.run(node, "wg_set_enabled", peer_name="fake", enabled=True, hard_deadline_seconds=3)
            raise AssertionError("bounded action fell back inline")
        except GateViolation:
            check("bounded action refuses inline fallback", True)
    # Deny PPP admission before querying/renewing a customer's service.
    server = object.__new__(radius.UserManagerRadiusServer)
    reply = SimpleNamespace(code=None)
    server.CreateReplyPacket = lambda pkt: reply
    db_stub = MagicMock()
    db_stub.query.side_effect = AssertionError("licence rejection queried customer")
    with patch.object(radius, "SessionLocal", return_value=db_stub), patch.object(
            license_guard, "current", return_value=cut), patch.object(radius, "_send_reply") as send:
        server._handle_auth_packet({}, "enforced")
        check("RADIUS refuses before queue/customer mutation", reply.code, radius.packet.AccessReject)
        check("RADIUS rejection actually sent", send.call_count, 1)

    def remote(node, block, **kwargs):
        # A new SQLite writer can start here: no request/session read or
        # write transaction is held while contacting a node.
        with sqlite3.connect(path, timeout=0) as probe:
            probe.execute("BEGIN IMMEDIATE")
            probe.rollback()
        calls.append((block, kwargs.get("enabled")))
        if fail[0]:
            raise RuntimeError("unknown remote outcome")
        return True

    def row(cid):
        with Session() as db:
            c = db.get(models.Connection, cid)
            return c.enabled, c.license_suspended, c.license_cut_applied

    with patch.object(qm, "SessionLocal", Session), patch.object(license_guard, "current", side_effect=lambda: verdict[0]), \
            patch.object(provisioning_schema, "is_ready", return_value=True), \
            patch.object(receipt_void_schema, "is_ready", return_value=True), \
            patch.object(qm.legacy_remote, "run", side_effect=remote):
        result = qm.enforce_license_services()
        check("all four protocols cut", result["applied"], 4)
        check("logical enabled preserved", [row(cid) for cid in ids], [(True, True, True)] * 4)
        check("admin disabled untouched", row(disabled_id), (False, False, False))
        check("all mutations use existing runner blocks", {b for b, _ in calls},
              {"wg_set_enabled", "ppp_kick", "xray_set_enabled", "softether_set_enabled"})
        with Session() as db:
            u = db.get(models.User, uid)
            check("money quota usage status preserved", (u.balance, u.total_quota_bytes, u.used_bytes, u.status),
                  (123, 10000, 100, models.UserStatus.active))
            qm._set_connection_enabled(db, db.get(models.Connection, ids[0]), True)
        check("normal quota pass cannot reopen", len(calls), 4)
        qm.enforce_license_services()
        check("successful cut not repeatedly sent", len(calls), 4)
        verdict[0] = active
        with Session() as db:
            db.get(models.Connection, ids[2]).session_limited = True
            db.get(models.Connection, ids[3]).enabled = False
            db.commit()
        qm.enforce_license_services()
        check("PPP resumes with fresh RADIUS checks", row(ids[1]), (True, False, False))
        check("eligible nonPPP automatically restored", row(ids[0]), (True, False, False))
        check("renewal only enables eligible remote", len(calls), 5)
        check("repeat explicit restore is a no-op", qm.restore_license_connection(ids[0])["restored"], False)
        check("restore clears only its marker", row(ids[0]), (True, False, False))
        check("repeat restore no second remote", qm.restore_license_connection(ids[0])["restored"], False)
        check("one remote enable", calls.count(("wg_set_enabled", True)), 1)
        with Session() as db:
            db.get(models.Connection, ids[2]).session_limited = True
            db.commit()
        before = len(calls)
        try:
            qm.restore_license_connection(ids[2])
            raise AssertionError("session-limited restore accepted")
        except HTTPException as exc:
            check("session limit remains authoritative", exc.detail, "license_restore_not_eligible")
        check("denied restoration no remote call", len(calls), before)
        with Session() as db:
            db.get(models.Connection, ids[2]).session_limited = False
            db.get(models.User, uid).status = models.UserStatus.disabled
            db.commit()
        try:
            qm.restore_license_connection(ids[2])
            raise AssertionError("disabled customer restored")
        except HTTPException as exc:
            check("disabled customer remains disabled", exc.status_code, 409)
        with Session() as db:
            db.get(models.User, uid).status = models.UserStatus.active
            db.commit()
        # Concurrent operator disable during the explicit remote operation.
        def raced_remote(conn, enabled):
            if enabled:
                with Session() as db:
                    db.get(models.User, uid).status = models.UserStatus.disabled
                    db.commit()
            calls.append(("race", enabled))
            return True
        with patch.object(qm, "_license_remote_state", side_effect=raced_remote):
            try:
                qm.restore_license_connection(ids[2])
                raise AssertionError("stale restore accepted")
            except HTTPException as exc:
                check("concurrent disable prevents success", exc.detail, "license_restore_state_changed")
            check("stale enable compensated by recut", calls[-2:], [("race", True), ("race", False)])
        check("uncertain/conflicting restore retains marker", row(ids[2]), (True, True, True))
        # Unknown cuts are retried rather than falsely confirmed.
        with Session() as db:
            db.get(models.User, uid).status = models.UserStatus.active
            c = db.get(models.Connection, ids[3]); c.license_cut_applied = False; c.enabled = True
            db.commit()
        verdict[0] = cut; fail[0] = True
        qm.enforce_license_services()
        check("unknown cut not acknowledged", row(ids[3]), (True, True, False))
        fail[0] = False
        qm._license_node_retry_after.clear()  # simulate the next eligible retry window
        qm.enforce_license_services()
        check("next poll retries unknown cut", row(ids[3]), (True, True, True))
        # Renewal does not override current limits. An exhausted service
        # stays marked, but a later legitimate renewal can restore it.
        verdict[0] = active
        with Session() as db:
            u = db.get(models.User, uid); u.used_bytes = u.total_quota_bytes
            db.commit()
        before = len(calls)
        qm.enforce_license_services()
        check("exhausted services are not restored", len(calls), before)
        check("exhausted service keeps recovery marker", row(ids[0]), (True, True, True))
        with Session() as db:
            u = db.get(models.User, uid); u.used_bytes = 0
            u.expire_at = dt.datetime.utcnow() - dt.timedelta(days=1)
            db.commit()
        qm.enforce_license_services()
        check("expired services are not restored", len(calls), before)
        with Session() as db:
            u = db.get(models.User, uid); u.expire_at = dt.datetime.utcnow() + dt.timedelta(days=30)
            c = db.get(models.Connection, ids[0]); c.enabled = False
            db.commit()
        qm.enforce_license_services()
        check("manually disabled service remains suspended", row(ids[0]), (False, True, True))
        check("eligible SoftEther restores automatically", row(ids[3]), (True, False, False))
        with Session() as db:
            db.get(models.Connection, ids[0]).enabled = True
            db.commit()
        qm.enforce_license_services()
        check("legitimate renewal can restore later", row(ids[0]), (True, False, False))
        # An unknown enable may have reached the node: recut it and keep
        # the marker, then recover once the transport is verifiable again.
        verdict[0] = cut
        with Session() as db:
            db.get(models.Connection, ids[2]).session_limited = False
            db.commit()
        qm.enforce_license_services()
        verdict[0] = active; fail[0] = True
        qm.enforce_license_services()
        check("unknown enable retains suspension", row(ids[2]), (True, True, False))
        fail[0] = False
        qm._license_node_retry_after.clear()
        qm.enforce_license_services()
        check("Xray automatically recovers after retry", row(ids[2]), (True, False, False))
        # In-flight durable work may be between SQL transactions while its
        # child contacts a node. Its committed operation row is a barrier.
        from app import models_provisioning as mp
        from app.services import provisioning_schema
        with Session() as db:
            c = db.get(models.Connection, ids[0]); c.license_suspended = True
            c.license_target_hash = qm._license_target_hash(qm._license_connection(db, c.id))
            db.add(mp.ProvisioningOperation(operation_type="renew_purchase", business_key="test-operation",
                request_hash="a" * 64, tenant_scope_key="shared", actor_kind="system",
                intent="{}", target_user_id=uid, state="prepared",
                forward_deadline=dt.datetime.utcnow() + dt.timedelta(minutes=5), wallet_epoch_at_start=0))
            db.commit()
        before = len(calls)
        with patch.object(provisioning_schema, "is_ready", return_value=True):
            try:
                qm.restore_license_connection(ids[0])
                raise AssertionError("pending operation ignored")
            except HTTPException as exc:
                check("pending durable work blocks restore", exc.detail, "license_restore_operation_pending")
        check("pending durable work causes no node call", len(calls), before)
        with Session() as db:
            operation = db.query(mp.ProvisioningOperation).one()
            operation.state = "completed"
            operation.completed_at = dt.datetime.utcnow()
            db.commit()
        # The user's account purchase lock must not block existing VPNs.
        # A financial cancellation, in contrast, owns this customer's service.
        from app.config import settings
        from app.models_reseller_refund import ResellerRefundOperation
        ResellerRefundOperation.__table__.create(engine, checkfirst=True)
        with Session() as db:
            db.add(ResellerRefundOperation(basis_id=1, purchase_id=1, user_id=uid,
                requested_by=1, baseline_used_bytes=0, mutation_started_at=dt.datetime.utcnow()))
            db.commit()
        with patch.object(settings, "reseller_cancellation_enabled", True):
            try:
                qm.restore_license_connection(ids[0])
                raise AssertionError("pending cancellation ignored")
            except HTTPException as exc:
                check("pending refund blocks restore", exc.detail, "reseller_cancellation_pending")
        with Session() as db:
            db.add(models.PanelSettings(id=1, ha_enabled=True)); db.commit()
        try:
            qm.enforce_license_services()
            raise AssertionError("HA licence control accepted")
        except HTTPException as exc:
            check("full VPN control refuses HA", exc.detail, "license_runtime_requires_single_host")
        with Session() as db:
            db.get(models.PanelSettings, 1).ha_enabled = False
            db.commit()
        with patch.object(provisioning_schema, "is_ready", return_value=False):
            try:
                qm.restore_license_connection(ids[0])
                raise AssertionError("unknown operation schema ignored")
            except HTTPException as exc:
                check("unready schema fails closed", exc.detail, "license_restore_schema_not_ready")
        from app import models_reservation_transition as rt
        rt.purchase_void_holds.create(engine)
        with Session() as db:
            purchase = models.Purchase(user_id=uid, quota_bytes=10000, used_bytes=0)
            db.add(purchase); db.flush()
            c = db.get(models.Connection, ids[0]); c.purchase_id = purchase.id
            db.flush()
            db.expire(c, ["purchase"])
            c.license_target_hash = qm._license_target_hash(qm._license_connection(db, c.id))
            db.execute(rt.purchase_void_holds.insert().values(purchase_key=purchase.history_key,
                approval_uuid="b" * 36, void_operation_id=1, state="active"))
            db.commit()
        before = len(calls)
        try:
            qm.restore_license_connection(ids[0])
            raise AssertionError("void hold ignored")
        except HTTPException as exc:
            check("renewal void hold blocks restore", exc.detail, "purchase_void_in_progress")
        check("void hold causes no node call", len(calls), before)
        # Budgeting must make durable progress, not revisit confirmed cuts
        # forever or let ineligible lower IDs starve eligible services.
        with Session() as db:
            db.query(models.Connection).update({"license_suspended": False})
            batch_ids = []
            for index in range(20):
                item = models.Connection(user_id=uid, node_id=node.id, type=models.ConnectionType.wireguard,
                    wg_peer_name=f"batch-{index}", enabled=True, license_suspended=True)
                db.add(item); db.flush(); batch_ids.append(item.id)
            db.commit()

        def batch_step(items):
            with Session() as db:
                db.get(models.Connection, items[0]).license_suspended = False
                db.commit()
            return {"applied": 1, "restoration_pending": 0, "remote_attempted": 1}

        with patch.object(qm, "_enforce_license_services", side_effect=batch_step):
            qm._license_node_retry_after.clear()
            check("first pass respects remote budget", qm.enforce_license_services()["applied"], 16)
            check("second pass advances past completed batch", qm.enforce_license_services()["applied"], 4)
        with Session() as db:
            check("batch markers eventually clear", db.query(models.Connection).filter_by(license_suspended=True).count(), 0)
            for cid in batch_ids:
                db.get(models.Connection, cid).license_suspended = True
            db.commit()

        def partly_blocked(items):
            if items[0] in batch_ids[:16]:
                return {"applied": 0, "restoration_pending": 1, "remote_attempted": 0}
            return batch_step(items)

        with patch.object(qm, "_enforce_license_services", side_effect=partly_blocked):
            check("ineligible services do not starve later targets", qm.enforce_license_services()["applied"], 4)
    engine.dispose()
check("no network", _no_network.attempts, [])
print(f"all {checks} checks passed")
