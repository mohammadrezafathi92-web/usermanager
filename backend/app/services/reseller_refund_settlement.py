"""Transactional settlement, not a public remote-delete API.

Remote orchestration must fence all writers and verify final consumption
before calling verify_stopped(). No HTTP payload can supply that evidence.
The opt-in WireGuard worker is its only production caller.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .. import models
from ..models_reseller_refund import ResellerRefundOperation as Operation
from ..models_reseller_refund import ResellerRefundStep as Step, ResellerSaleBasis as Basis
from . import accounting, hierarchy
from .reseller_refund import preview


def _begin(db: Session):
    if db.new or db.dirty or db.deleted:
        raise RuntimeError("settlement requires a clean session")
    if db.bind.dialect.name == "sqlite":
        db.execute(text("BEGIN IMMEDIATE"))


def _locked(db, model, ident):
    return db.execute(select(model).where(model.id == ident).with_for_update()
                      .execution_options(populate_existing=True)).scalar_one_or_none()


def _identity(connection):
    # No raw credential is stored in the operation or logged. A recreated
    # row/renamed credential/retargeted node cannot inherit old evidence.
    fields = ("id", "user_id", "purchase_id", "node_id", "type", "wg_peer_name",
              "wg_public_key", "wg_client_address", "ppp_username", "ppp_password",
              "xr_email", "xr_uuid", "xr_flow")
    node_fields = ("type", "mt_host", "mt_port", "mt_api_ssl_port", "mt_use_ssl",
                   "mt_wireguard_interface", "xr_panel_mode", "xr_ssh_host",
                   "xr_ssh_port", "xr_inbound_tag", "xr_config_path",
                   "xr_panel_base_url", "xr_panel_inbound_id", "se_host",
                   "se_port", "se_hub_name")
    value = {key: str(getattr(connection, key, None)) for key in fields}
    value["node"] = {key: str(getattr(connection.node, key, None)) for key in node_fields}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def find_operation(db: Session, purchase_id: int, purchase, *, lock: bool = False):
    """The operation answering for this purchase id: the live one, or - only
    while no service holds the id - the completed one that retired it."""
    query = select(Operation).where(Operation.purchase_id == purchase_id)
    op = db.execute(query.with_for_update() if lock else query).scalar_one_or_none()
    if op is None and purchase is None:
        query = (select(Operation).where(Operation.retired_purchase_id == purchase_id)
                 .order_by(Operation.id.desc()).limit(1))
        op = db.execute(query.with_for_update() if lock else query).scalar_one_or_none()
    return op


def prepare(db: Session, *, purchase_id: int, actor: models.AdminUser) -> int:
    """Persist the exact set of connections. Retrying returns the same ID.

    Internal only: creating an intent alone does NOT stop a service, and
    is not sufficient to refund it. This deliberately leaves funds alone.
    """
    try:
        _begin(db)
        purchase = _locked(db, models.Purchase, purchase_id)
        existing = find_operation(db, purchase_id, purchase, lock=True)
        user_id = existing.user_id if existing is not None else (purchase.user_id if purchase else None)
        user = db.get(models.User, user_id) if user_id is not None else None
        owned = hierarchy.owned_admin_ids(db, actor)
        if user is None or (owned is not None and user.owner_admin_id not in owned):
            raise HTTPException(404, "service_not_found")
        if existing is not None:
            db.commit()
            return existing.id
        # Payment/renewal/operation holds again, inside this writer
        # transaction (SQLite BEGIN IMMEDIATE / row locks): a renewal or a
        # durable operation that committed after the caller's availability
        # check cannot slip in before the operation row exists. From then on
        # the pending operation itself refuses the customer's mutations.
        from . import reseller_refund_holds
        held = reseller_refund_holds.reason(db, purchase, user)
        if held:
            raise HTTPException(409, held)
        preview(db, purchase)  # refuses missing source / changed entitlement
        basis = db.query(Basis).filter_by(purchase_id=purchase_id).one()
        op = Operation(basis_id=basis.id, purchase_id=purchase.id, user_id=user.id,
                       requested_by=actor.id, baseline_used_bytes=int(purchase.used_bytes or 0),
                       purchase_key=purchase.history_key)
        db.add(op)
        db.flush()
        for connection in purchase.connections:
            db.add(Step(operation_id=op.id, connection_id=connection.id,
                        identity_digest=_identity(connection),
                        baseline_total_bytes=int(connection.total_bytes or 0)))
        ident = op.id
        db.commit()
        return ident
    except Exception:
        db.rollback()
        raise


def capture_counters(db: Session, *, operation_id: int, connection_id: int,
                     download_bytes: int, upload_bytes: int) -> dict:
    """Save stopped raw counters BEFORE deletion, outside any remote call.

    A crash after remote delete can then retry using this durable evidence.
    This does not mark absence verified and never makes an operation payable.
    """
    try:
        _begin(db)
        op = _locked(db, Operation, operation_id)
        if op is None or op.state != "pending":
            raise HTTPException(409, "refund_not_pending")
        connection = _locked(db, models.Connection, connection_id)
        step = db.query(Step).filter_by(operation_id=op.id, connection_id=connection_id).one_or_none()
        if step is None or connection is None or _identity(connection) != step.identity_digest:
            raise HTTPException(409, "refund_resource_changed")
        if any(type(v) is not int or v < 0 for v in (download_bytes, upload_bytes)):
            raise HTTPException(409, "refund_usage_unverified")
        if step.captured_at is not None:
            if (step.final_download_counter, step.final_upload_counter) != (download_bytes, upload_bytes):
                raise HTTPException(409, "refund_usage_changed")
        else:
            # Unlike ordinary best-effort polling, a refund may not guess
            # at traffic lost during a counter reset/reboot.
            previous = (int(connection.last_rx_bytes or 0), int(connection.last_tx_bytes or 0))
            if download_bytes < previous[0] or upload_bytes < previous[1]:
                raise HTTPException(409, "refund_counter_reset")
            delta = download_bytes - previous[0] + upload_bytes - previous[1]
            step.final_total_bytes = int(connection.total_bytes or 0) + delta
            step.final_download_counter, step.final_upload_counter = download_bytes, upload_bytes
            step.captured_at = datetime.utcnow()
        result = {"download_bytes": step.final_download_counter,
                  "upload_bytes": step.final_upload_counter, "total_bytes": step.final_total_bytes}
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


def _ppp_step(db, operation_id, connection_id):
    op = _locked(db, Operation, operation_id)
    if op is None or op.state != "pending":
        raise HTTPException(409, "refund_not_pending")
    connection = _locked(db, models.Connection, connection_id)
    step = db.execute(select(Step).where(Step.operation_id == op.id, Step.connection_id == connection_id)
                      .with_for_update()).scalar_one_or_none()
    if step is None or connection is None or _identity(connection) != step.identity_digest:
        raise HTTPException(409, "refund_resource_changed")
    return op, connection, step


# How long block_ppp waits for the RADIUS decisions in flight to be answered.
PPP_BLOCK_WAIT_SECONDS = 30.0


def block_ppp(db: Session, *, operation_id: int, connection_id: int) -> None:
    """From now on RADIUS refuses this PPP connection's logins (radius_server,
    reseller_refund_block). Idempotent; only settlement ends it.

    Committed under session_cap_mode's lock taken EXCLUSIVELY: every
    Access-Request holds it shared from before it reads anything until its
    reply is sent, so this waits for each decision in flight. An Accept
    decided before the block is therefore sent - and its RadiusAuthGrant
    committed - before blocked_at, and every later request sees the block.
    The grant then keeps the operation pending until that session's own
    Class-bound Stop is counted (ppp_stop_missing)."""
    from . import session_cap_mode
    from .gate_locks import FileLock, LockTimeout, LockUnavailable
    lock = FileLock(session_cap_mode.lock_path())
    try:
        lock.acquire(shared=False, timeout=PPP_BLOCK_WAIT_SECONDS)
    except (LockTimeout, LockUnavailable):
        raise HTTPException(409, "ppp_block_unconfirmed") from None
    try:
        _begin(db)
        _op, _connection, step = _ppp_step(db, operation_id, connection_id)
        step.blocked_at = step.blocked_at or datetime.utcnow()
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        lock.release()


def observe_ppp(db: Session, *, operation_id: int, connection_id: int, session_ids, at: datetime) -> None:
    """Persist (before they are ended) WHICH router sessions it had: each is
    final only with its own Stop after `at`, RADIUS row or not."""
    from .reseller_refund_block import session_key
    try:
        _begin(db)
        _op, _connection, step = _ppp_step(db, operation_id, connection_id)
        if step.blocked_at is None:
            raise HTTPException(409, "ppp_not_blocked")
        seen = json.loads(step.ppp_seen_sessions or "{}")
        for sid in session_ids:
            seen.setdefault(session_key(sid), at.isoformat())
        step.ppp_seen_sessions = json.dumps(seen, sort_keys=True)
        db.commit()
    except Exception:
        db.rollback()
        raise


def current_ppp_grants(db: Session, connection):
    """Only this credential generation's grants, never a recycled row id's.

    No digest on a historical grant means unproven, not a wildcard match.
    """
    return db.query(models.RadiusAuthGrant).filter(
        models.RadiusAuthGrant.connection_id == connection.id,
        models.RadiusAuthGrant.connection_history_key == connection.history_key,
        models.RadiusAuthGrant.connection_history_key.isnot(None))


def _unbound_ppp_grant(db, connection_id):
    # Pre-upgrade grants carry no generation. They cannot be safely assigned
    # to this row, or silently treated as if their accepted session ended.
    return db.query(models.RadiusAuthGrant.id).filter(
        models.RadiusAuthGrant.connection_id == connection_id,
        models.RadiusAuthGrant.connection_history_key.is_(None),
        models.RadiusAuthGrant.state.in_(("granted", "open"))).first() is not None


def ppp_stop_missing(db: Session, step):
    """Why the PPP connection's final usage is not known yet, or None. Only
    Class-bound sessions (models.RadiusAuthGrant) can ever prove it:
      ppp_session_unverified       a session without our Class cookie - an
                                   open session row without a grant, router
                                   session no grant names, or such accounting
                                   after the block (step.ppp_unverified_at);
      ppp_session_foreign_node     an unended grant from another NAS than the
                                   connection's node (its router is not read);
      ppp_accounting_lost          a session open when its NAS restarted and
                                   not heard of since (usage maybe lost);
      ppp_accounting_stop_missing  a grant's session is open (no Stop);
      ppp_login_unresolved         an Accept whose session has not reported
                                   at all - it may still come up.
    No elapsed time resolves any of them (fail closed: the operation stays
    pending, unpaid)."""
    from .reseller_refund_block import session_key
    cid = step.connection_id
    connection = db.get(models.Connection, cid)
    node_id = connection.node_id if connection is not None else None
    if step.ppp_unverified_at is not None:
        return "ppp_session_unverified"
    if _unbound_ppp_grant(db, cid):
        return "ppp_session_unverified"
    if db.query(models.RadiusActiveSession.id).filter(models.RadiusActiveSession.connection_id == cid,
                                                      models.RadiusActiveSession.auth_nonce.is_(None)).first():
        return "ppp_session_unverified"
    if connection is None:
        return "ppp_session_unverified"
    grants = current_ppp_grants(db, connection).all()
    if any(g.accounting_unverified for g in grants):
        return "ppp_session_unverified"
    unended = [g for g in grants if g.state in ("granted", "open")]
    if any(g.node_id is None or g.node_id != node_id for g in unended):
        return "ppp_session_foreign_node"
    if any(g.state == "open" and g.nas_restart_at is not None for g in unended):
        return "ppp_accounting_lost"
    for state, reason in (("open", "ppp_accounting_stop_missing"), ("granted", "ppp_login_unresolved")):
        if any(g.state == state for g in unended):
            return reason
    seen = json.loads(step.ppp_seen_sessions or "{}")
    for sid, first_seen in seen.items():
        mine = [g for g in grants if g.node_id == node_id and session_key(g.acct_session_id) == sid]
        if not mine:
            return "ppp_session_unverified"     # a router session no grant of ours names
        # whole seconds: MariaDB DATETIME keeps no fraction of stopped_at
        seen_at = datetime.fromisoformat(first_seen).replace(microsecond=0)
        if not any(g.state == "stopped" and g.stopped_at and g.stopped_at >= seen_at for g in mine):
            return "ppp_accounting_stop_missing"
    return None


def ppp_sessions_unverified(db: Session, connection, session_ids) -> bool:
    """Read-only, before anything is touched (so a service is never blocked
    into an operation that could not end): does the router show a session no
    unended grant of this connection on that node names - or is there an
    unended grant (an Accept that never came up, a lost Stop, another
    node's) the router does not show?"""
    from .reseller_refund_block import session_key
    if _unbound_ppp_grant(db, connection.id):
        return True
    unended = current_ppp_grants(db, connection).filter(
        models.RadiusAuthGrant.state.in_(("granted", "open"))).all()
    shown = {session_key(sid) for sid in session_ids}
    if any(sid is None for sid in session_ids):
        return True
    if any(g.node_id is None or g.node_id != connection.node_id or g.nas_restart_at is not None or g.accounting_unverified
           or session_key(g.acct_session_id) not in shown for g in unended):
        return True
    return not shown <= {session_key(g.acct_session_id) for g in unended}


def mark_waiting(db: Session, *, operation_id: int, connection_id: int, reason: str) -> None:
    """Record what the step waits for (shown to the operator); keeps the first time."""
    try:
        _begin(db)
        step = db.execute(select(Step).where(Step.operation_id == operation_id, Step.connection_id == connection_id)
                          .with_for_update()).scalar_one_or_none()
        if step is not None and step.verified_at is None:
            if step.wait_reason != reason:
                step.wait_reason, step.wait_since = reason, datetime.utcnow()
        db.commit()
    except Exception:
        db.rollback()
        raise


def capture_ppp(db: Session, *, operation_id: int, connection_id: int) -> int:
    """The PPP connection's final usage: its total once it is blocked and
    every RADIUS session of it has ended with an Accounting Stop (a pruned
    or reconciled-away row never counts - both skip a blocked connection),
    including each session the router showed without a RADIUS row
    (ppp_stop_missing). Refused while any is missing. Never rewritten later."""
    try:
        _begin(db)
        _op, connection, step = _ppp_step(db, operation_id, connection_id)
        if step.blocked_at is None:
            raise HTTPException(409, "ppp_not_blocked")
        missing = ppp_stop_missing(db, step)
        if missing:
            raise HTTPException(409, missing)
        total = int(connection.total_bytes or 0)
        if step.captured_at is not None:
            if total != step.final_total_bytes:
                raise HTTPException(409, "refund_usage_changed")
        else:
            if total < step.baseline_total_bytes:
                raise HTTPException(409, "refund_counter_reset")
            step.final_total_bytes, step.captured_at = total, datetime.utcnow()
        step.wait_reason = step.wait_since = None
        result = step.final_total_bytes
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


def verify_stopped(db: Session, *, operation_id: int, connection_id: int,
                   final_total_bytes: int) -> None:
    """INTERNAL worker hook: already stopped, absence verified, counters final.

    The worker must retain a non-expiring writer fence until settle commits.
    This function cannot establish remote absence itself. It is never routed
    through FastAPI or accepted from a customer/reseller request.
    """
    try:
        _begin(db)
        op = _locked(db, Operation, operation_id)
        if op is None:
            raise HTTPException(404, "refund_not_found")
        if op.state == "completed":
            db.commit()
            return
        step = db.execute(select(Step).where(Step.operation_id == op.id,
                          Step.connection_id == connection_id).with_for_update()).scalar_one_or_none()
        if step is None:
            raise HTTPException(409, "refund_connection_unexpected")
        connection = _locked(db, models.Connection, connection_id)
        if connection is None or _identity(connection) != step.identity_digest:
            raise HTTPException(409, "refund_resource_changed")
        if type(final_total_bytes) is not int or final_total_bytes < step.baseline_total_bytes:
            raise HTTPException(409, "refund_usage_unverified")
        if step.captured_at is not None and final_total_bytes != step.final_total_bytes:
            raise HTTPException(409, "refund_usage_changed")
        # Evidence is monotonic; retry must never replace it with an older read.
        step.final_total_bytes = max(final_total_bytes, step.final_total_bytes or 0)
        step.captured_at = step.captured_at or datetime.utcnow()
        step.verified_at = datetime.utcnow()
        db.commit()
    except Exception:
        db.rollback()
        raise


def settle(db: Session, *, operation_id: int) -> dict:
    """Credit, audit result, and DB removal commit or roll back together."""
    try:
        _begin(db)
        op = _locked(db, Operation, operation_id)
        if op is None:
            raise HTTPException(404, "refund_not_found")
        if op.state == "completed":
            result = {"operation_id": op.id, "refund_amount": op.refund_amount, "state": "completed",
                      "purchase_id": op.retired_purchase_id or op.purchase_id}
            db.commit()
            return result
        basis = _locked(db, Basis, op.basis_id)
        purchase = _locked(db, models.Purchase, op.purchase_id)
        if basis is None or purchase is None or purchase.user_id != op.user_id:
            raise HTTPException(409, "refund_resource_changed")
        steps = db.query(Step).filter_by(operation_id=op.id).all()
        connections = list(purchase.connections)
        if (not steps or {s.connection_id for s in steps} != {c.id for c in connections}
                or any(s.verified_at is None for s in steps)):
            raise HTTPException(409, "refund_remote_unverified")
        if any(_identity(c) != next(s.identity_digest for s in steps if s.connection_id == c.id)
               for c in connections):
            raise HTTPException(409, "refund_resource_changed")
        # Capture/remote verification commit separately. Accounting may have
        # committed new unresolved evidence since either one; only this final
        # writer transaction can bind that evidence to the financial commit.
        # It also serializes a concurrent accounting writer on supported SQLite.
        for connection in connections:
            if connection.type in (models.ConnectionType.openvpn, models.ConnectionType.l2tp):
                step = next(s for s in steps if s.connection_id == connection.id)
                missing = ppp_stop_missing(db, step)
                if missing:
                    raise HTTPException(409, missing)
        if any(int(c.total_bytes or 0) > next(s.final_total_bytes for s in steps
                                             if s.connection_id == c.id) for c in connections):
            raise HTTPException(409, "refund_usage_changed")
        if db.query(models.RadiusActiveSession).filter(
                models.RadiusActiveSession.connection_id.in_([c.id for c in connections])).first():
            raise HTTPException(409, "refund_sessions_active")
        user = db.get(models.User, op.user_id)
        if user is None:
            raise HTTPException(409, "refund_resource_changed")
        # Counter resets are not supported by this single-sale allocation.
        if int(purchase.used_bytes or 0) < op.baseline_used_bytes:
            raise HTTPException(409, "refund_usage_changed")
        final_used = max(int(purchase.used_bytes or 0), op.baseline_used_bytes +
                         sum(s.final_total_bytes - s.baseline_total_bytes for s in steps))
        # Read-only projection with final, NOT cached, usage. The temporary
        # ORM assignment is rolled back on failure, or deleted on success.
        purchase.used_bytes = final_used
        result = preview(db, purchase, now=max(s.captured_at for s in steps))
        target = _locked(db, models.AdminUser, basis.charged_admin_id)
        if target is None:
            raise HTTPException(409, "reseller_refund_account_missing")
        amount = result["refund_amount"]
        db.execute(models.AdminUser.__table__.update().where(models.AdminUser.id == target.id)
                   .values(balance=models.AdminUser.balance + amount))
        entry = accounting.record(db, "admin_credit_refund", amount, user=user,
                                  admin_id=target.id, actor_admin_id=op.requested_by,
                                  purchase_id=purchase.id, payment_method="admin_credit",
                                  note=f"Unpaid service cancellation #{op.id}; debit #{basis.debit_entry_id}")
        db.flush()
        op.state, op.refund_amount, op.refund_entry_id = "completed", amount, entry.id
        op.completed_at = datetime.utcnow()
        if basis.sale_entry_id:
            sale = _locked(db, models.LedgerEntry, basis.sale_entry_id)
            if sale is None or sale.voided_at is not None:
                raise HTTPException(409, "reseller_sale_unproven")
            sale.voided_at = op.completed_at
        if basis.purchase_count_delta:
            counted = db.execute(models.User.__table__.update().where(
                models.User.id == user.id,
                models.User.purchase_count >= basis.purchase_count_delta)
                       .values(purchase_count=models.User.purchase_count - basis.purchase_count_delta))
            if counted.rowcount != 1:
                raise HTTPException(409, "reseller_purchase_count_unproven")
        # All connections were verified absent before this transaction.
        # Use the same DB-only primitive as the future Receipt Void step.
        from .user_ops import finalize_purchase_deletion_after_deprovision
        finalize_purchase_deletion_after_deprovision(db, purchase)
        # Free the id for a future service (see the models' retired_purchase_id).
        original = op.purchase_id
        op.retired_purchase_id, op.purchase_id = original, -op.id
        basis.retired_purchase_id, basis.purchase_id = basis.purchase_id, -basis.id
        db.commit()
        return {"operation_id": op.id, "refund_amount": amount, "state": "completed", "purchase_id": original}
    except Exception:
        db.rollback()
        raise
