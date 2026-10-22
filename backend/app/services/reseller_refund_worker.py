"""Opt-in single-host SQLite unpaid-service cancellation: WireGuard and
PPP (OpenVPN / L2TP / PPTP over RADIUS) connections of ONE Purchase.

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
from .reseller_refund_settlement import block_ppp, capture_ppp, mark_waiting


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


PPP_TYPES = (models.ConnectionType.openvpn, models.ConnectionType.l2tp, models.ConnectionType.pptp)
SUPPORTED_TYPES = (models.ConnectionType.wireguard,) + PPP_TYPES
# How long one attempt waits for the Accounting Stop of the sessions it just
# ended (RouterOS sends it at once; a few seconds covers the round trip).
PPP_STOP_WAIT_SECONDS = 20
PPP_KICK_ATTEMPTS = 3


def _peer(connection):
    identity = _identity(connection)
    return dict(interface=identity.interface, peer_name=identity.peer_name,
                public_key=identity.public_key, client_address=identity.client_address)


def _ppp_sessions(connection) -> int:
    """/ppp/active entries for the connection now (a read, in this process)."""
    try:
        with MikrotikClient.for_node(connection.node) as mt:
            return mt.count_ppp_sessions(connection.ppp_username)
    except MikrotikError:
        raise AdapterError("ppp_sessions_unreadable") from None


def _ppp_final(db, op_id, connection):
    step = db.query(Step).filter_by(operation_id=op_id, connection_id=connection.id).one()
    if step.captured_at is not None:
        capture_ppp(db, operation_id=op_id, connection_id=connection.id)   # re-checks, never rewrites
        return
    node, username = connection.node, connection.ppp_username
    for _ in range(PPP_KICK_ATTEMPTS):
        db.rollback()
        if not _ppp_sessions(connection):
            break
        # the login is refused (blocked): a kicked session cannot come back
        _remote(node, "ppp_kick", username=username, default="ppp_kick_failed")
    else:
        if _ppp_sessions(connection):
            mark_waiting(db, operation_id=op_id, connection_id=connection.id, reason="ppp_kick_unconfirmed")
            raise AdapterError("ppp_kick_unconfirmed")
    deadline = time.monotonic() + PPP_STOP_WAIT_SECONDS
    while True:
        db.expire_all()
        open_ = db.query(models.RadiusActiveSession.id).filter_by(connection_id=connection.id).first()
        if open_ is None:
            break
        if time.monotonic() >= deadline:
            # No proof of the final usage: the operation waits; nothing is
            # paid and no prune/timeout ever stands in for the Stop.
            mark_waiting(db, operation_id=op_id, connection_id=connection.id,
                         reason="ppp_accounting_stop_missing")
            raise AdapterError("ppp_accounting_stop_missing")
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
        reason = availability(db, purchase, starting=existing is None)
        if reason:
            if existing is not None:
                raise HTTPException(409, {"code": "reseller_cancellation_pending",
                                          "operation_id": existing.id, "reason": reason})
            raise HTTPException(409, reason)
        preview(db, purchase)
        op_id = prepare(db, purchase_id=purchase_id, actor=actor)
        connections = list(purchase.connections)
        wgs = [c for c in connections if c.type == models.ConnectionType.wireguard]
        ppps = [c for c in connections if c.type in PPP_TYPES]
        basis = db.query(Basis).filter_by(purchase_id=purchase_id).one()
        try:
            # 1. Validate ALL identities before touching anything.
            for connection in wgs:
                if not all((connection.wg_peer_name, connection.wg_public_key, connection.wg_client_address)):
                    raise HTTPException(409, "refund_resource_changed")
                with MikrotikClient.for_node(connection.node) as mt:
                    found = wg.read(mt, _identity(connection))
                    step = db.query(Step).filter_by(operation_id=op_id, connection_id=connection.id).one()
                    if found.state is wg.ReadState.ABSENT and step.captured_at is not None:
                        continue
                    if found.state is not wg.ReadState.PRESENT_MATCH:
                        raise AdapterError("wg_refund_identity_unverified")
                    _prove_no_reboot(mt, connection, basis)
            # 2. PPP: no new login from here on (RADIUS refuses a blocked step).
            for connection in ppps:
                block_ppp(db, operation_id=op_id, connection_id=connection.id)
            # 3. WireGuard: stop and make the final counters durable.
            stopped_usage = {}
            for connection in wgs:
                step = db.query(Step).filter_by(operation_id=op_id, connection_id=connection.id).one()
                prior = ([step.final_download_counter, step.final_upload_counter]
                         if step.captured_at is not None else None)
                peer, node = _peer(connection), connection.node
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
                if _ppp_sessions(connection) or db.query(models.RadiusActiveSession.id).filter_by(
                        connection_id=connection.id).first():
                    raise AdapterError("ppp_session_reappeared")
                step = db.query(Step).filter_by(operation_id=op_id, connection_id=connection.id).one()
                verify_stopped(db, operation_id=op_id, connection_id=connection.id,
                               final_total_bytes=step.final_total_bytes)
            # 7. Only now: credit, ledger and deletion in one transaction.
            return settle(db, operation_id=op_id)
        except AdapterError as exc:
            db.rollback()
            raise HTTPException(409, {"code": "reseller_cancellation_pending",
                                      "operation_id": op_id, "reason": exc.code}) from None
        except HTTPException as exc:
            db.rollback()
            if isinstance(exc.detail, dict):
                raise
            # The operation exists from prepare() on: report it as what it
            # is - pending, with the reason - not as a plain failure.
            raise HTTPException(409, {"code": "reseller_cancellation_pending",
                                      "operation_id": op_id, "reason": exc.detail}) from None
        except Exception:
            db.rollback()
            raise HTTPException(503, {"code": "reseller_cancellation_pending",
                                     "operation_id": op_id, "reason": "node_or_database_unavailable"}) from None
