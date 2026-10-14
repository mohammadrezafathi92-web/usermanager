"""Receipt-approved durable execution: request, T1 binding, cancel, and the
binding check every later transaction repeats (Lifecycle 7.6, 11.4).

An approval's production call (bot create_user / purchase_package /
renew_service with approval_uuid + execution_token) on a durable type runs
exactly one operation with business_key approval:{uuid}:v:{version}:

  intent_of()  rebuilds the registration's ApprovalIntent from the request
               and the approval's immutable snapshots; T1 compares its hash
               with immutable_intent_hash, so nothing here is trusted.
  build()      the Preparation, taken from the FROZEN manifest (slots, sale,
               reseller charge, renewal), never from live prices.
  lock()       inside T1 (prepare): the binding (wallet/receipt runtime ->
               approval row, owner-approved lock order), mode, replay.
  preflight()  inside T1 after the leases: live target + manifest compare;
               a mismatch is ApprovalCancel - the caller rolls T1 back and
               cancel() commits registered -> cancelled on its own.
  check()      T_final, T_comp and every worker tick: version, key instance,
               live key. A lost binding is ApprovalBindingLost: no T_final,
               no T_comp, no new remote call - mark_binding_lost() moves the
               operation to cleanup_required with one approval_recovery.

No secret, username or Telegram id is logged or put in an error.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import func, select

from .. import models, models_provisioning as mp, models_receipt_void as rv
from . import bot_auth, receipt_approval_intent as ri, receipt_approval_runtime as runtime

SHAPE_OF = {"create_user": "new_user", "purchase": "existing_user", "renew_purchase": "renew_purchase"}
LOST_CODES = ("execution_key_revoked", "approval_binding_lost")


class ApprovalCancel(Exception):
    """T1 found the registration no longer executable: roll T1 back, then
    cancel() (registered -> cancelled) and answer 409 with this code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class ApprovalBindingLost(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ApprovalExecution:
    approval_uuid: str
    version: int
    principal: bot_auth.BotPrincipal
    intent: ri.ApprovalIntent


def _approval(db, approval_uuid, *, lock=False):
    query = select(rv.receipt_approvals).where(rv.receipt_approvals.c.approval_uuid == approval_uuid)
    if lock:
        query = query.with_for_update()
    row = db.execute(query).mappings().one_or_none()
    return dict(row) if row else None


def _manifest(db, approval_uuid) -> list[ri.ManifestRow]:
    expected = rv.receipt_approval_expected_effects
    rows = db.execute(select(expected.c.effect_type, expected.c.effect_key, expected.c.requirement,
                             expected.c.expected).where(expected.c.approval_uuid == approval_uuid)).all()
    return [ri.ManifestRow(r.effect_type, r.effect_key, r.requirement, json.loads(r.expected)) for r in rows]


def require_token(payload) -> int:
    token = getattr(payload, "execution_token", None)
    if token is None:
        # A durable approval runs only with the token its registration
        # issued; an old caller that sends none is refused before any write.
        raise HTTPException(422, "approval_execution_token_required")
    if type(token) is not int or token < 0:
        raise HTTPException(422, "approval_execution_token_invalid")
    return token


def intent_of(db, approval_uuid: str, payload, *, package, renew_purchase_id=None) -> ri.ApprovalIntent:
    """The registration's intent, rebuilt from this request + the approval's
    immutable snapshots. Connections are the request's own slots only for a
    package without a bundle - exactly what the bot registered."""
    row = _approval(db, approval_uuid)
    if row is None:
        raise HTTPException(404, "receipt_approval_not_found")
    bundled = package is not None and db.execute(select(models.PackageConnection.id).where(
        models.PackageConnection.package_id == package.id).limit(1)).first() is not None
    specs = () if bundled else tuple(
        ri.ConnectionSpec(int(c.node_id), str(getattr(c.protocol, "value", c.protocol)), c.flow or "")
        for c in (getattr(payload, "connections", None) or ()))
    amount = payload.paid_amount if payload.paid_amount is not None else row["amount_snapshot"]
    list_price = getattr(payload, "list_price", None)
    return ri.ApprovalIntent(
        pending_source_instance_id=row["pending_source_instance_id"], pending_local_id=int(row["pending_local_id"]),
        kind=row["kind"], target_username=row["target_username_snapshot"], amount=int(amount),
        package_id=package.id if package is not None else None, renew_purchase_id=renew_purchase_id,
        payment_card_id=payload.payment_card_id, receipt_file_id=row["receipt_file_id_snapshot"],
        discount_code=payload.discount_code, referral_code=getattr(payload, "referral_code", None),
        list_price=int(list_price) if list_price is not None else int(amount), connections=specs)


def _slot_order(key):
    kind, _, index = key.rpartition(":")
    return kind, int(index)


def build(db, execution: ApprovalExecution, operation_type: str, *, user=None, full_name=None, comment=None,
          payment_method=None, discount_amount=None):
    """The trusted Preparation of one approval, from its frozen manifest."""
    import uuid as _uuid
    from . import provisioning_finalization as final, provisioning_preparation as preparation
    from . import provisioning_requests as requests
    row = _approval(db, execution.approval_uuid)
    if row is None:
        raise HTTPException(404, "receipt_approval_not_found")
    if SHAPE_OF.get(operation_type) != row["target_shape"]:
        # renew_user registrations expect a user-level renewal, which the
        # current service model refuses (routers/bot.py renew); never guessed.
        raise HTTPException(409, "approval_shape_not_executable")
    rows = {(r.effect_type, r.effect_key): r for r in _manifest(db, execution.approval_uuid)}
    package = db.get(models.Package, execution.intent.package_id) if execution.intent.package_id else None
    if package is None:
        raise HTTPException(409, "manifest_precondition_failed")
    sale_row = rows.get(("ledger_sale", "sale"))
    if sale_row is None:
        raise HTTPException(409, "manifest_precondition_failed")
    card = sale_row.expected["payment_card_id"]
    sale = final.SaleSnapshot(amount=int(sale_row.expected["amount"]), admin_id=row["owner_admin_id_snapshot"],
                              payment_method=payment_method or ("card" if card is not None else None),
                              payment_card_id=card, discount_code=execution.intent.discount_code,
                              discount_amount=discount_amount)
    spend = rows.get(("ledger_credit_spend", "credit_spend"))
    payers = [preparation.Payer(kind="reseller_credit", id=int(spend.expected["admin_id"]),
                                amount=int(spend.expected["amount"]))] if spend else []
    terms = final.ApprovalTerms(list_price=int(execution.intent.list_price or 0),
                                discount_code=(execution.intent.discount_code or "").strip().upper() or None,
                                referral_code=(execution.intent.referral_code or "").strip().upper() or None)
    speed = getattr(package, "speed_limit_mbps", None)
    connection_rows = sorted(((key, r) for (kind, key), r in rows.items() if kind == "connection_created"),
                             key=lambda item: _slot_order(item[0]))
    slots = [preparation.Slot(slot_key=key[len("conn:"):], node_id=int(r.expected["node_id"]),
                              protocol=r.expected["protocol"], flow=r.expected["flow"], max_concurrent_sessions=1,
                              speed_limit_mbps=speed)
             for key, r in connection_rows]
    common = dict(business_key=f"approval:{execution.approval_uuid}:v:{execution.version}",
                  tenant_scope_key=row["tenant_scope_key"], payers=payers)
    if operation_type == "create_user":
        created = rows.get(("user_created", "user"))
        if created is None:
            raise HTTPException(409, "manifest_precondition_failed")
        expected = created.expected
        intent = final.FinalIntent(
            user=final.UserSnapshot(username=expected["username"], full_name=full_name,
                                    telegram_id=expected["telegram_id"], owner_admin_id=expected["owner_admin_id"]),
            package=requests.package_snapshot(package), sale=sale, comment=comment,
            purchase_batch=_uuid.uuid4().hex if slots else None,
            # The new customer carries the package's limits on the user row
            # (what the manifest's user_created describes, as the legacy
            # approval path does); a Purchase exists only with connections.
            quota_bytes=int(expected["quota_bytes"]), duration_days=int(expected["days"] or 0),
            reseller_count_delta=1 if slots else 0, created_via="bot", approval=terms)
        return preparation.Preparation(operation_type="create_user", intent=intent, slots=slots, **common)
    if user is None or user.id != row["target_user_id_snapshot"]:
        raise HTTPException(409, "approval_intent_changed")
    if operation_type == "purchase":
        intent = final.FinalIntent(user=requests.user_snapshot(user), package=requests.package_snapshot(package),
                                   sale=sale, comment=comment, purchase_batch=_uuid.uuid4().hex,
                                   reseller_count_delta=1, approval=terms)
        return preparation.Preparation(operation_type="purchase", target_user_id=user.id, intent=intent,
                                       slots=slots, **common)
    renewed = rows.get(("purchase_renewed", "renew"))
    if renewed is None or renewed.expected["purchase_id"] is None:
        raise HTTPException(409, "manifest_precondition_failed")
    intent = final.FinalIntent(user=requests.user_snapshot(user), package=requests.package_snapshot(package),
                               sale=sale, purchase_id=int(renewed.expected["purchase_id"]),
                               quota_bytes=int(renewed.expected["add_quota_bytes"]),
                               duration_days=int(renewed.expected["add_days"]), approval=terms)
    return preparation.Preparation(operation_type="renew_purchase", target_user_id=user.id, intent=intent, **common)


# ------------------------------------------------------------------ inside T1
def lock(db, request, execution: ApprovalExecution, digest: str):
    """The approval half of T1, in prepare()'s fenced transaction, before any
    lease: binding, mode, replay. Raises on anything not executable."""
    from . import provisioning_approval_binding as binding
    if not isinstance(execution, ApprovalExecution) or request.business_key != (
            f"approval:{execution.approval_uuid}:v:{execution.version}"):
        raise HTTPException(422, "provisioning_approval_request_invalid")
    locked = binding.lock_prepare(db, execution.approval_uuid, execution.version, execution.principal,
                                  request.operation_type, execution_intent=execution.intent, request_hash=digest,
                                  tenant_scope_key=request.tenant_scope_key)
    if locked.replay_operation_id is not None:
        return locked
    if locked.wallet_phase != "normal":
        raise HTTPException(503, "provisioning_approval_mode_unavailable")
    if locked.effective_mode != locked.registered_under_mode:
        raise ApprovalCancel("approval_mode_changed")
    return locked


def preflight(db, locked, execution: ApprovalExecution, tokens, request) -> None:
    """After the leases: the live target and the manifest, and the request
    against that manifest (it was built from it - any drift cancels)."""
    from . import provisioning_approval_live as live
    result = live.compare(db, locked, execution.intent, tokens)
    if result.mismatch is not None:
        raise ApprovalCancel(result.mismatch)
    rows = {(r.effect_type, r.effect_key): r.expected for r in result.manifest_rows}
    slots = {f"conn:{slot.slot_key}": (slot.node_id, slot.protocol, slot.flow) for slot in request.slots}
    wanted = {key: (spec["node_id"], spec["protocol"], spec["flow"])
              for (kind, key), spec in rows.items() if kind == "connection_created"}
    sale = request.intent.sale
    spend = rows.get(("ledger_credit_spend", "credit_spend"))
    payers = {(p.kind, p.id, p.amount) for p in request.payers}
    if slots != wanted or sale is None or (sale.amount, sale.payment_card_id) != (
            rows[("ledger_sale", "sale")]["amount"], rows[("ledger_sale", "sale")]["payment_card_id"]) or payers != (
            {("reseller_credit", spend["admin_id"], spend["amount"])} if spend else set()):
        raise ApprovalCancel("manifest_precondition_failed")


def mark_mutating(db, execution: ApprovalExecution) -> None:
    updated = db.execute(rv.receipt_approvals.update().where(
        rv.receipt_approvals.c.approval_uuid == execution.approval_uuid,
        rv.receipt_approvals.c.version == execution.version,
        rv.receipt_approvals.c.state == "registered").values(state="mutating", mutating_at=func.now())).rowcount
    if updated != 1:
        raise HTTPException(409, "approval_precondition_failed")


def cancel(session_factory, approval_uuid: str, version: int, code: str) -> None:
    """registered -> cancelled, its own commit after T1 rolled back. Only an
    approval still registered at this version with no effect moves."""
    from . import resource_leases
    with session_factory() as db:
        resource_leases.begin_business(db)
        db.execute(select(rv.wallet_runtime_state.c.id).where(rv.wallet_runtime_state.c.id == 1)
                   .with_for_update(read=True)).all()
        runtime.read_state(db, lock=True, shared_lock=True)
        has_effect = select(rv.receipt_approval_effects.c.id).where(
            rv.receipt_approval_effects.c.approval_uuid == approval_uuid).exists()
        db.execute(rv.receipt_approvals.update().where(
            rv.receipt_approvals.c.approval_uuid == approval_uuid, rv.receipt_approvals.c.version == version,
            rv.receipt_approvals.c.state == "registered", ~has_effect).values(
                state="cancelled", cancellation_reason=code[:40]))
        db.commit()


# ------------------------------------------------------------------ every later transaction
def lock_for(db, operation_id: int):
    """The approval runtime (shared) and the approval row (exclusive) of an
    approval operation, in the owner-approved order: after the wallet/type/
    host runtime reads, before any resource lease. None without an approval."""
    approval_uuid = db.scalar(select(mp.ProvisioningOperation.approval_uuid).where(
        mp.ProvisioningOperation.id == operation_id))
    if approval_uuid is None:
        return None
    state = runtime.read_state(db, lock=True, shared_lock=True)
    if state is None or runtime.effective_registration_mode(state) == runtime.BLOCKED:
        raise HTTPException(503, "receipt_approval_blocked")
    return _approval(db, approval_uuid, lock=True) or {"approval_uuid": approval_uuid, "missing": True}


def recovery_override(db, approval_uuid: str) -> bool:
    """An approval_recovery a superadmin set running replaces the key check."""
    ops = rv.wallet_operations
    return db.execute(select(ops.c.id).where(ops.c.operation_type == "approval_recovery",
                                             ops.c.business_key == approval_uuid, ops.c.state == "running")
                      .with_for_update()).first() is not None


def check(db, operation, row) -> None:
    """Version, key instance and live key (Lifecycle 7.6). The state rules
    belong to each transaction (T_final: mutating; T_comp: mutating, no effect)."""
    if row is None or row.get("missing") or row["version"] != operation.approval_execution_version or (
            row["execution_key_instance_uuid"] != operation.execution_key_instance_uuid):
        raise ApprovalBindingLost("approval_binding_lost")
    if operation.approval_key_bound and not recovery_override(db, operation.approval_uuid):
        key = db.execute(select(models.ApiKey).where(
            models.ApiKey.key_instance_uuid == operation.execution_key_instance_uuid).with_for_update()
        ).scalar_one_or_none()
        if key is None or not key.enabled or key.key_type != bot_auth.KeyType.REMOTE_SHARED_BOT:
            raise ApprovalBindingLost("execution_key_revoked")


def open_recovery(db, approval_uuid: str, tenant_scope_key: str, *, epoch: int) -> int:
    """One approval_recovery per approval (business_key = approval_uuid);
    a repeat returns the same row."""
    ops = rv.wallet_operations
    existing = db.execute(select(ops.c.id).where(ops.c.operation_type == "approval_recovery",
                                                 ops.c.business_key == approval_uuid).with_for_update()).scalar()
    if existing is not None:
        return int(existing)
    return int(db.execute(ops.insert().values(
        operation_type="approval_recovery", business_key=approval_uuid,
        request_hash=hashlib.sha256(approval_uuid.encode()).hexdigest(), tenant_scope_key=tenant_scope_key,
        actor_kind="system", state="cleanup_required", epoch_at_start=int(epoch), result_snapshot="{}",
        created_at=dt.datetime.utcnow())).inserted_primary_key[0])


def mark_binding_lost(session_factory, operation_id: int, code: str) -> None:
    """cleanup_required + approval_recovery in one small commit; the money
    stays reserved and every created remote stays recorded (Lifecycle 7.6)."""
    from . import provisioning_transitions as transitions, resource_leases
    if code not in LOST_CODES:
        code = "approval_binding_lost"
    with session_factory() as db:
        resource_leases.begin_business(db)
        epoch = db.execute(select(rv.wallet_runtime_state.c.epoch).where(rv.wallet_runtime_state.c.id == 1)
                           .with_for_update(read=True)).scalar_one()
        runtime.read_state(db, lock=True, shared_lock=True)
        approval_uuid = db.scalar(select(mp.ProvisioningOperation.approval_uuid).where(
            mp.ProvisioningOperation.id == operation_id))
        if approval_uuid is None:
            return
        _approval(db, approval_uuid, lock=True)
        operation = transitions._operation(db, operation_id)
        if operation.state in transitions.TERMINAL or operation.state == "cleanup_required":
            open_recovery(db, approval_uuid, operation.tenant_scope_key, epoch=epoch)
            db.commit()
            return
        transitions._write_operation(db, operation, "cleanup_required", error_code=code)
        open_recovery(db, approval_uuid, operation.tenant_scope_key, epoch=epoch)
        db.commit()


# ------------------------------------------------------------------ the bot's separate calls
def operation_of(db, approval_uuid: str):
    """The durable operation of this approval's CURRENT version, if any."""
    version = db.scalar(select(rv.receipt_approvals.c.version).where(
        rv.receipt_approvals.c.approval_uuid == approval_uuid))
    if version is None:
        return None
    return db.execute(select(mp.ProvisioningOperation).where(
        mp.ProvisioningOperation.approval_uuid == approval_uuid,
        mp.ProvisioningOperation.approval_execution_version == version)).scalar_one_or_none()


def optional_answer(db, approval_uuid: str, effect: str):
    """apply_referral / redeem_discount / record_card_payment with an
    approval whose operation exists: the answer comes from that operation's
    optional_effects, and nothing is mutated again (Lifecycle 7.6). None =
    no durable operation (the legacy endpoint runs)."""
    operation = operation_of(db, approval_uuid)
    if operation is None:
        return None
    if operation.state != "completed":
        raise HTTPException(409, {"error": "approval_operation_not_completed", "operation_id": operation.id,
                                  "state": operation.state})
    try:
        effects = json.loads(operation.optional_effects or "{}")
    except ValueError:
        effects = {}
    return effects.get(effect) or {"outcome": "skipped", "reason": "not_requested"}
