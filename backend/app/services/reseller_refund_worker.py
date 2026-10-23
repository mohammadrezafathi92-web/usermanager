"""Opt-in single-host SQLite unpaid-service cancellation: WireGuard and
PPP (OpenVPN / L2TP over RADIUS) connections of ONE Purchase.

PPTP is NOT supported: this panel's RADIUS neither authenticates nor
accounts PPTP sessions (radius_server.PPP_TYPES), so nothing could prove a
PPTP connection's final usage. A service with one is refused before any
mutation - production's mixed WG/OpenVPN/L2TP/PPTP sales stay ineligible.

Each connection is a step; every step's state is committed as it moves, so
a failure leaves the operation pending with what was already done (peers
stopped or deleted, sessions ended, usage captured) and a resume continues
from there. Credit, ledger and deletion happen once, only after every step
is verified. Other transports are refused before any remote mutation. No
public payload supplies usage.
"""
import sys
import re
import time
from datetime import datetime
from types import SimpleNamespace

from fastapi import HTTPException

from .. import models
from ..models_reseller_refund import ResellerRefundOperation as Operation, ResellerRefundStep as Step
from ..models_reseller_refund import ResellerSaleBasis as Basis
from . import adapter_mikrotik_wg as wg
from .adapter_base import AdapterError
from . import legacy_remote
from .mikrotik_client import MikrotikClient, MikrotikError
from .transport_guard import GateViolation
from .reseller_refund import preview
from .reseller_refund_fence import barrier, enabled
from . import reseller_refund_holds as holds
from .reseller_refund_settlement import prepare, capture_counters, verify_stopped, settle, find_operation
from .reseller_refund_settlement import block_ppp, capture_ppp, mark_waiting, observe_ppp, ppp_stop_missing
from .reseller_refund_settlement import ppp_sessions_unverified
from .reseller_refund_settlement import current_ppp_grants


def availability(db, purchase, *, starting=True):
    """None when this purchase may be cancelled; otherwise a stable code.
    starting=False is the resume of an existing operation: payment and
    operation holds only stop a NEW cancellation (see reseller_refund_holds)."""
    if not enabled():
        return "reseller_cancellation_disabled"
    if not sys.platform.startswith("linux") or db.bind.dialect.name != "sqlite":
        return "reseller_cancellation_platform_unsupported"
    panel = db.query(models.PanelSettings).first()
    if panel and panel.ha_enabled:
        return "reseller_cancellation_ha_unsupported"
    connections = list(purchase.connections)
    if not connections or any(c.type not in SUPPORTED_TYPES for c in connections):
        return "reseller_cancellation_protocol_unsupported"
    if any(c.type in PPP_TYPES and (not c.ppp_username or c.node is None
                                    or c.node.type != models.NodeType.mikrotik) for c in connections):
        # the session is ended on its MikroTik's /ppp/active
        return "reseller_cancellation_protocol_unsupported"
    # Legacy rewards lack reversible provenance until the loyalty cutover.
    if purchase.user.referral_reward_granted or purchase.user.loyalty_rewards_given:
        return "reseller_cancellation_rewards_untracked"
    basis = db.query(Basis).filter_by(purchase_id=purchase.id).one_or_none()
    sale = db.get(models.LedgerEntry, basis.sale_entry_id) if basis and basis.sale_entry_id else None
    if sale is None or sale.kind != "sale_new" or sale.user_id != purchase.user_id:
        return "reseller_sale_unproven"
    if sale.payment_method is not None or sale.approval_uuid is not None:
        return "reseller_receipt_requires_full_void"
    if sale.discount_code or sale.discount_amount:
        return "reseller_discount_requires_full_void"
    if sale.voided_at is not None:
        return "reseller_sale_already_cancelled"
    if starting:
        return holds.reason(db, purchase, purchase.user)
    return None


def _identity(connection):
    return wg.WireguardIdentity(connection.node.mt_wireguard_interface, connection.wg_peer_name,
                                connection.wg_public_key, connection.wg_client_address)


def _uptime_seconds(value):
    """Strict RouterOS durations; unknown output cannot prove continuity."""
    if not isinstance(value, str):
        raise AdapterError("wg_uptime_unverified")
    match = re.fullmatch(r"(?:(\d+)w)?(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", value)
    if match and any(part is not None for part in match.groups()):
        return sum(int(part or 0) * unit for part, unit in zip(match.groups(), (604800, 86400, 3600, 60, 1)))
    match = re.fullmatch(r"(?:(\d+)w)?(?:(\d+)d)?(\d+):(\d{2}):(\d{2})", value)
    if match and int(match[4]) < 60 and int(match[5]) < 60:
        return sum(int(part or 0) * unit for part, unit in zip(match.groups(), (604800, 86400, 3600, 60, 1)))
    raise AdapterError("wg_uptime_unverified")


def _prove_uptime(uptime, connection, basis):
    # A reboot followed by enough traffic can overtake the old counter;
    # checking only for a decreased counter misses that lost consumption.
    source_time = min(basis.created_at, connection.created_at)
    elapsed = max(0, (datetime.utcnow() - source_time).total_seconds())
    if _uptime_seconds(uptime) <= elapsed + 2:
        raise AdapterError("wg_usage_continuity_unverified")


def _prove_no_reboot(mt, connection, basis):
    _prove_uptime(mt.get_system_resources().get("uptime"), connection, basis)


_CODE = re.compile(r"(wg|ppp|refund)_[a-z_]+")


def _remote(node, block, default="wg_refund_unreadable", **arguments):
    """One refund write through legacy_remote: inline in gate off/shadow,
    in the runner child (node lock, ownership, endpoint fencing) when the
    gate is enforced - the only place a write may run then. The child
    reports an adapter refusal as a client error carrying its code; any
    other failure (unknown outcome, gate refusal) is 'unreadable', which
    leaves the operation pending for a resume, never settled."""
    try:
        return legacy_remote.run(node, block, factory=lambda n: MikrotikClient.for_node(n), **arguments)
    except AdapterError:
        raise
    except (MikrotikError, GateViolation) as exc:
        code = str(exc).strip()
        raise AdapterError(code if _CODE.fullmatch(code) else default) from None


PPP_TYPES = (models.ConnectionType.openvpn, models.ConnectionType.l2tp)
SUPPORTED_TYPES = (models.ConnectionType.wireguard,) + PPP_TYPES
# How long one attempt waits for the Accounting Stop of the sessions it just
# ended (RouterOS sends it at once; a few seconds covers the round trip).
PPP_STOP_WAIT_SECONDS = 20
PPP_KICK_ATTEMPTS = 3
# An Access-Accept sent just before the block (block_ppp waits for it) may
# bring its session up at any later time: its RadiusAcceptGrant keeps the
# operation pending until that session reports - no elapsed time is proof.


def _peer(connection):
    identity = _identity(connection)
    return dict(interface=identity.interface, peer_name=identity.peer_name,
                public_key=identity.public_key, client_address=identity.client_address)


def _ppp_sessions(connection) -> list:
    """The session-ids of the connection's /ppp/active entries now (a read,
    in this process); None for an entry RouterOS shows no id for."""
    try:
        with MikrotikClient.for_node(connection.node) as mt:
            return list(mt.ppp_session_ids(connection.ppp_username))
    except MikrotikError:
        raise AdapterError("ppp_sessions_unreadable") from None


def _node_copy(node):
    # Freeze the loaded node values before rollback expires ORM objects.
    # Credentials stay only in memory; the copy never goes to a log/result.
    return SimpleNamespace(**{column.key: getattr(node, column.key) for column in node.__mapper__.column_attrs})


def _read_ppp_sessions(db, connection):
    frozen = SimpleNamespace(id=connection.id, node=_node_copy(connection.node), ppp_username=connection.ppp_username)
    db.rollback()
    return _ppp_sessions(frozen)


def _precheck(db, connections, basis, op_id):
    """Reads only: failures here must not create an effective hold.

    The exclusive cancellation barrier already protects ordinary writers.
    prepare() subsequently revalidates DB scope/payment under its writer
    lock; block_ppp additionally serializes the actual block with auth.
    """
    for connection in connections:
        step = (db.query(Step).filter_by(operation_id=op_id, connection_id=connection.id).one()
                if op_id is not None else None)
        if connection.type == models.ConnectionType.wireguard:
            if not all((connection.wg_peer_name, connection.wg_public_key, connection.wg_client_address)):
                raise HTTPException(409, "refund_resource_changed")
            captured = step is not None and step.captured_at is not None
            identity, node = _identity(connection), _node_copy(connection.node)
            source_time = min(basis.created_at, connection.created_at)
            db.rollback()
            with MikrotikClient.for_node(node) as mt:
                found = wg.read(mt, identity)
                if found.state is wg.ReadState.ABSENT and captured:
                    continue
                if found.state is not wg.ReadState.PRESENT_MATCH:
                    raise AdapterError("wg_refund_identity_unverified")
                elapsed = max(0, (datetime.utcnow() - source_time).total_seconds())
                if _uptime_seconds(mt.get_system_resources().get("uptime")) <= elapsed + 2:
                    raise AdapterError("wg_usage_continuity_unverified")
            continue
        if step is not None and step.blocked_at is not None:
            continue
        legacy = db.query(models.RadiusActiveSession.id).filter(
            models.RadiusActiveSession.connection_id == connection.id,
            models.RadiusActiveSession.auth_nonce.is_(None)).first() is not None
        foreign = current_ppp_grants(db, connection).filter(
            models.RadiusAuthGrant.state.in_(("granted", "open")),
            (models.RadiusAuthGrant.node_id.is_(None)) |
            (models.RadiusAuthGrant.node_id != connection.node_id)).first() is not None
        others = [_node_copy(n) for n in db.query(models.Node).filter(
            models.Node.type == models.NodeType.mikrotik, models.Node.id != connection.node_id,
            models.Node.mt_radius_secret.isnot(None), models.Node.mt_radius_secret != "").all()]
        username = connection.ppp_username
        if legacy or foreign:
            db.rollback()
            raise AdapterError("ppp_session_unverified")
        sessions = _read_ppp_sessions(db, connection)
        unverified = ppp_sessions_unverified(db, connection, sessions)
        db.rollback()
        if unverified:
            raise AdapterError("ppp_session_unverified")
        # Same credential on another configured NAS cannot be ignored merely
        # because old accounting was lost/pruned. Read without an open DB tx.
        for other in others:
            try:
                with MikrotikClient.for_node(other) as mt:
                    elsewhere = mt.ppp_session_ids(username)
            except MikrotikError:
                raise AdapterError("ppp_sessions_unreadable") from None
            if elsewhere:
                raise AdapterError("ppp_session_foreign_node")


def _start_mutation(db, op_id):
    """Crash/unknown-outcome fence, committed BEFORE the first mutation.

    A prepared-only operation is resumable but does not turn off normal
    quota enforcement. Once this marker commits, only settlement releases
    ownership; even a stop that fails before returning may have happened.
    """
    from .reseller_refund_settlement import _begin, _locked, _identity as identity_digest
    db.rollback()
    try:
        _begin(db)
        op = _locked(db, Operation, op_id)
        if op is None or op.state != "pending":
            raise HTTPException(409, "refund_not_pending")
        purchase = _locked(db, models.Purchase, op.purchase_id)
        if purchase is None or purchase.history_key != op.purchase_key:
            raise HTTPException(409, "refund_resource_changed")
        steps = db.query(Step).filter_by(operation_id=op.id).all()
        connections = list(purchase.connections)
        if ({s.connection_id for s in steps} != {c.id for c in connections}
                or any(identity_digest(c) != next(s.identity_digest for s in steps if s.connection_id == c.id)
                       for c in connections)):
            raise HTTPException(409, "refund_resource_changed")
        preview(db, purchase)
        # A prepared-only intent deliberately owns nothing: payment or another
        # operation may have appeared before this retry. Recheck inside the
        # very transaction that first acquires mutation ownership. A genuinely
        # started operation must still resume without rejudging its eligibility.
        already_started = (op.mutation_started_at is not None or any(
            s.blocked_at is not None or s.captured_at is not None or s.verified_at is not None
            for s in steps))
        if not already_started:
            reason = holds.reason(db, purchase, purchase.user)
            if reason:
                raise HTTPException(409, reason)
        op.mutation_started_at = op.mutation_started_at or datetime.utcnow()
        db.commit()
    except Exception:
        db.rollback()
        raise


def _ppp_final(db, op_id, connection):
    step = db.query(Step).filter_by(operation_id=op_id, connection_id=connection.id).one()
    if step.captured_at is not None:
        capture_ppp(db, operation_id=op_id, connection_id=connection.id)   # re-checks, never rewrites
        return
    node, username = _node_copy(connection.node), connection.ppp_username
    for _ in range(PPP_KICK_ATTEMPTS):
        db.rollback()
        seen_at = datetime.utcnow()
        sessions = _read_ppp_sessions(db, connection)
        if not sessions:
            break
        if any(sid is None for sid in sessions):
            # its Stop could not be told from another session's
            mark_waiting(db, operation_id=op_id, connection_id=connection.id, reason="ppp_session_unidentified")
            raise AdapterError("ppp_session_unidentified")
        # Recorded before the kick: no RADIUS row (Start/Interims lost) is
        # not "no usage" - each needs the Stop of its own id
        # (settlement.ppp_stop_missing).
        observe_ppp(db, operation_id=op_id, connection_id=connection.id, session_ids=sessions, at=seen_at)
        # the login is refused (blocked): a kicked session cannot come back
        db.rollback()
        _remote(node, "ppp_kick", username=username, default="ppp_kick_failed")
    else:
        if _read_ppp_sessions(db, connection):
            mark_waiting(db, operation_id=op_id, connection_id=connection.id, reason="ppp_kick_unconfirmed")
            raise AdapterError("ppp_kick_unconfirmed")
    deadline = time.monotonic() + PPP_STOP_WAIT_SECONDS
    while True:
        db.expire_all()
        step = db.query(Step).filter_by(operation_id=op_id, connection_id=connection.id).one()
        missing = ppp_stop_missing(db, step)
        if not missing:
            break
        if time.monotonic() >= deadline:
            # No proof of the final usage: the operation waits; nothing is
            # paid and no prune/timeout ever stands in for the Stop, nor
            # for the session an unresolved Access-Accept may still bring.
            mark_waiting(db, operation_id=op_id, connection_id=connection.id, reason=missing)
            raise AdapterError(missing)
        db.rollback()
        time.sleep(0.5)
    capture_ppp(db, operation_id=op_id, connection_id=connection.id)


def execute(db, *, purchase_id, actor, purchase_key=None, user_id=None):
    """purchase_key is the Purchase.history_key the operator's preview
    showed. With it, a retry is answered by ITS operation even after the
    numeric id was reused by another service, and nothing ever targets a
    different service than the one previewed."""
    with barrier(exclusive=True):
        purchase = db.get(models.Purchase, purchase_id)
        if purchase_key is not None:
            keyed = (db.query(Operation).filter(Operation.purchase_key == purchase_key)
                     .order_by(Operation.id.desc()).first())
            if keyed is not None and user_id is not None and keyed.user_id != user_id:
                keyed = None
            if keyed is not None and keyed.state == "completed":
                return settle(db, operation_id=keyed.id)  # the stored result; nothing runs again
            if purchase is None or purchase.history_key != purchase_key:
                if keyed is None and purchase is None:
                    raise HTTPException(404, "service_not_found")
                raise HTTPException(409, "reseller_service_changed")
        existing = find_operation(db, purchase_id, purchase)
        if (existing is not None and existing.state == "pending" and purchase is not None
                and existing.purchase_key is not None and existing.purchase_key != purchase.history_key):
            # A pending operation never continues against another service.
            raise HTTPException(409, "reseller_service_changed")
        # prepare() performs scope checks even when the Purchase is gone.
        if existing and existing.state == "completed":
            # Its purchase was deleted: a live row under this id (a reused
            # SQLite id, before ids were retired) is another service.
            if purchase is not None:
                raise HTTPException(409, "reseller_charge_unproven")
            op_id = prepare(db, purchase_id=purchase_id, actor=actor)
            return settle(db, operation_id=op_id)
        if purchase is None:
            raise HTTPException(404, "service_not_found")
        # Scope is checked before even read-only node prechecks; prepare()
        # checks it again under the writer lock before recording an intent.
        from . import hierarchy
        owned = hierarchy.owned_admin_ids(db, actor)
        if owned is not None and purchase.user.owner_admin_id not in owned:
            raise HTTPException(404, "service_not_found")
        reason = availability(db, purchase, starting=existing is None)
        if reason:
            if existing is not None:
                raise HTTPException(409, {"code": "reseller_cancellation_pending",
                                          "operation_id": existing.id, "reason": reason})
            raise HTTPException(409, reason)
        preview(db, purchase)
        op_id = existing.id if existing is not None else None
        connections = list(purchase.connections)
        wgs = [c for c in connections if c.type == models.ConnectionType.wireguard]
        ppps = [c for c in connections if c.type in PPP_TYPES]
        basis = db.query(Basis).filter_by(purchase_id=purchase_id).one()
        try:
            _precheck(db, connections, basis, op_id)
            # No operation/hold is created by a failed read-only precheck.
            # Revalidate payment/scope in prepare's writer transaction after
            # those reads, then durably claim the first mutation's outcome.
            op_id = prepare(db, purchase_id=purchase_id, actor=actor)
            _start_mutation(db, op_id)
            # 2. PPP: no new login from here on (RADIUS refuses a blocked step).
            for connection in ppps:
                block_ppp(db, operation_id=op_id, connection_id=connection.id)
            # 3. WireGuard: stop and make the final counters durable.
            stopped_usage = {}
            for connection in wgs:
                step = db.query(Step).filter_by(operation_id=op_id, connection_id=connection.id).one()
                prior = ([step.final_download_counter, step.final_upload_counter]
                         if step.captured_at is not None else None)
                peer, node = _peer(connection), _node_copy(connection.node)
                db.rollback()            # no DB transaction across the network
                stopped = _remote(node, "wg_refund_stop", previous=prior, **peer)
                usage = [stopped["download"], stopped["upload"]]
                if prior is None or stopped.get("present", True):
                    _prove_uptime(stopped.get("uptime"), connection, basis)
                    capture_counters(db, operation_id=op_id, connection_id=connection.id,
                                     download_bytes=usage[0], upload_bytes=usage[1])
                stopped_usage[connection.id] = (peer, node, usage)
            # 4. PPP: end every open session, then its usage is final only
            #    once each has its Accounting Stop.
            for connection in ppps:
                _ppp_final(db, op_id, connection)
            # 5. WireGuard: delete the stopped peers (usage already durable).
            for connection in wgs:
                peer, node, usage = stopped_usage[connection.id]
                db.rollback()
                _remote(node, "wg_refund_remove", download=usage[0], upload=usage[1], **peer)
                db.expire_all()
                step = db.query(Step).filter_by(operation_id=op_id, connection_id=connection.id).one()
                verify_stopped(db, operation_id=op_id, connection_id=connection.id,
                               final_total_bytes=step.final_total_bytes)
            # 6. PPP: still blocked, no session on the router or in RADIUS.
            for connection in ppps:
                db.expire_all()
                sessions = _read_ppp_sessions(db, connection)
                nonces = [g.nonce for g in current_ppp_grants(db, connection).all()]
                active = db.query(models.RadiusActiveSession.id).filter(
                    models.RadiusActiveSession.connection_id == connection.id,
                    (models.RadiusActiveSession.auth_nonce.is_(None)) |
                    (models.RadiusActiveSession.auth_nonce.in_(nonces))).first()
                if sessions or active:
                    raise AdapterError("ppp_session_reappeared")
                step = db.query(Step).filter_by(operation_id=op_id, connection_id=connection.id).one()
                verify_stopped(db, operation_id=op_id, connection_id=connection.id,
                               final_total_bytes=step.final_total_bytes)
            # 7. Only now: credit, ledger and deletion in one transaction.
            return settle(db, operation_id=op_id)
        except AdapterError as exc:
            db.rollback()
            if op_id is None:
                raise HTTPException(409, exc.code) from None
            raise HTTPException(409, {"code": "reseller_cancellation_pending",
                                      "operation_id": op_id, "reason": exc.code}) from None
        except HTTPException as exc:
            db.rollback()
            if op_id is None:
                raise
            if isinstance(exc.detail, dict):
                raise
            # The operation exists from prepare() on: report it as what it
            # is - pending, with the reason - not as a plain failure.
            raise HTTPException(409, {"code": "reseller_cancellation_pending",
                                      "operation_id": op_id, "reason": exc.detail}) from None
        except Exception:
            db.rollback()
            if op_id is None:
                raise HTTPException(503, "node_or_database_unavailable") from None
            raise HTTPException(503, {"code": "reseller_cancellation_pending",
                                     "operation_id": op_id, "reason": "node_or_database_unavailable"}) from None
