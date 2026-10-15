"""The approval half of T_final and T_comp, and the superadmin's
approval_recovery decision (Lifecycle 7.6, 7.7, 11.5).

T_final (record): after the business rows of provisioning_finalization -
every manifest effect is recorded in the SAME transaction, the optional
benefits (discount, referral, card payment) are applied from the frozen
manifest and their outcome kept in operation.optional_effects, then the
approval is completed:

  registered_under_mode 'required': any effect failure, or a missing /
      unexpected effect, raises ApprovalEffectFailed - the caller rolls the
      whole T_final back and starts compensation (final_tx_failed_permanent).
  'shadow': an effect failure rolls back only its savepoint and logs a
      shadow event; a manifest that does not validate leaves the approval
      'mutating' and opens one approval_recovery - in the same commit as the
      completed operation, so no window exists.

T_comp (fail): mutating, no effect -> failed, with the release of money.
"""
from __future__ import annotations

import datetime as dt
import json

from fastapi import HTTPException
from sqlalchemy import func, select

from .. import models, models_receipt_void as rv
from . import provisioning_approval_exec as approval_exec, receipt_approval_effects as effects
from . import receipt_approval_intent as ri, receipt_approval_runtime as runtime


# Lifecycle 18 decision 12: an open recovery warns after 7 days; "continue"
# closes after 30.
WARN_AFTER_DAYS, CONTINUE_CLOSES_AFTER_DAYS = 7, 30


class ApprovalEffectFailed(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _rows(db, approval_uuid):
    return {(r.effect_type, r.effect_key): r for r in approval_exec._manifest(db, approval_uuid)}


def _written(db, approval_uuid):
    table = rv.receipt_approval_effects
    return {(r.effect_type, r.effect_key) for r in db.execute(select(table.c.effect_type, table.c.effect_key).where(
        table.c.approval_uuid == approval_uuid))}


class _Recorder:
    def __init__(self, db, row, manifest):
        self.db, self.row, self.manifest = db, row, manifest
        self.strict = row["registered_under_mode"] == "required"

    def __call__(self, effect_type, effect_key, resource, evidence=None):
        if (effect_type, effect_key) not in self.manifest:
            if self.strict:
                raise ApprovalEffectFailed("effect_not_in_manifest")
            runtime.record_shadow_event(self.db, pending_source_instance_id=self.row["pending_source_instance_id"],
                                        pending_local_id=self.row["pending_local_id"], stage="effect",
                                        error_code=effects.NOT_IN_MANIFEST, approval_uuid=self.row["approval_uuid"])
            return False
        if not self.strict:
            return effects.record_effect_shadow(self.db, self.row["approval_uuid"], effect_type, effect_key,
                                                resource, evidence)
        try:
            effects.record_effect(self.db, self.row["approval_uuid"], effect_type, effect_key, resource, evidence)
        except effects.EffectRejected as exc:
            raise ApprovalEffectFailed(exc.code) from None
        return True


def _discount(db, record, intent, user, manifest):
    """Optional: validated without side effects first; skipped, never failed."""
    from . import user_ops
    terms = intent.approval
    keys = [key for kind, key in manifest if kind == "discount_redeemed"]
    if not terms.discount_code:
        return None
    if not keys:
        return {"outcome": "skipped", "reason": "کد تخفیف نامعتبر است", "discount_amount": 0}
    owner = intent.user.owner_admin_id
    valid, reason, _amount = user_ops.validate_discount_code(db, terms.discount_code, terms.list_price,
                                                            username=user.username, owner_admin_id=owner)
    if not valid:
        return {"outcome": "skipped", "reason": reason, "discount_amount": 0}
    sink: list = []
    ok, reason, amount = user_ops.redeem_discount_code_core(db, terms.discount_code, user.username, terms.list_price,
                                                            owner_admin_id=owner, evidence_sink=sink)
    if not ok:
        return {"outcome": "skipped", "reason": reason, "discount_amount": 0}
    redemption, evidence = sink[0]
    record("discount_redeemed", f"discount:{redemption.code_id}", redemption, evidence)
    return {"outcome": "applied", "reason": None, "discount_amount": int(amount)}


def _referral(db, record, intent, user, purchase, manifest):
    """Referral rewards exactly as FROZEN in the manifest at registration
    (amounts, the referrer, the quota destination) - never the live panel
    settings or a destination re-resolved now. No referral row in the
    manifest (a code that named nobody, the customer's own code, zero
    rewards, an existing customer): nothing at all is written - not even
    the referral link. A row that cannot be applied as frozen is simply not
    recorded: 'required' then rolls T_final back, 'shadow' opens a recovery."""
    from . import quota_rewards, wallet_service
    terms = intent.approval
    if not terms.referral_code:
        return None
    rows = {key: row.expected for (kind, key), row in manifest.items()
            if key.startswith(("credit:referral_reward:", "quota:referral:"))}
    if not rows:
        return {"outcome": "skipped", "reason": "not_in_manifest"}
    if user.referred_by_id or user.referral_reward_granted:
        return {"outcome": "skipped", "reason": "already_referred"}
    referrer = db.query(models.User).filter(models.User.referral_code == terms.referral_code,
                                            models.User.id != user.id).with_for_update().first()
    frozen_referrer = (rows.get("credit:referral_reward:referrer") or {}).get("referrer_user_id")
    if referrer is None or (frozen_referrer is not None and referrer.id != frozen_referrer):
        return {"outcome": "skipped", "reason": "referrer_changed"}
    user.referred_by_id = referrer.id
    user.referral_reward_granted = True
    for key in sorted(rows):
        expected = rows[key]
        if key.startswith("credit:"):
            target = referrer if key.endswith(":referrer") else user
            before = int(target.balance or 0)
            wallet_service.adjust_in_session(db, target, int(expected["amount"]),
                                             source_kind=wallet_service.REFERRAL_REWARD)
            db.flush()
            record("wallet_credit_source_created", key, target,
                   effects.WalletCreditEvidence(before, int(target.balance or 0)))
            continue
        binding = expected["target"]
        if binding == ri.USER_REF:
            target = user
        elif binding == ri.PURCHASE_REF:
            target = purchase
        else:
            model = models.Purchase if binding["type"] == "Purchase" else models.User
            target = db.execute(select(model).where(model.id == binding["id"]).with_for_update()).scalar_one_or_none()
        if target is None:
            continue
        before = quota_rewards.quota(target)
        if quota_rewards.grant(target, int(expected["bytes"])) <= 0:
            continue                                  # unlimited or gone: the frozen reward cannot be applied
        db.flush()
        kind = "Purchase" if isinstance(target, models.Purchase) else "User"
        record("quota_reward_granted", key, target,
               effects.QuotaRewardEvidence(kind, target.id, before, quota_rewards.quota(target), False))
    return {"outcome": "applied", "reason": None}


def _card_payment(db, record, intent, approval_uuid, manifest):
    from . import payment_cards
    sale = intent.sale
    if sale is None or sale.payment_card_id is None or sale.amount <= 0:
        return None
    written = payment_cards.advance_after_payment_core(db, sale.payment_card_id, sale.amount,
                                                       approval_uuid=approval_uuid)
    event = db.execute(select(rv.payment_card_pool_events).where(
        rv.payment_card_pool_events.c.approval_uuid == approval_uuid)).mappings().first()
    if event is not None:
        record("card_payment_recorded", "card_payment", dict(event))
    return {"outcome": "applied" if written else "skipped"}


def record(db, operation, intent, row, *, user, purchase, connections, entry, credit_entries, renewal_evidence):
    """Stage every effect, the optional benefits, and approval completion.
    Returns the optional_effects document for the completed operation."""
    approval_uuid = row["approval_uuid"]
    manifest = _rows(db, approval_uuid)
    rec = _Recorder(db, row, manifest)
    if operation.operation_type == "create_user":
        db.flush()
        rec("user_created", "user", user, effects.UserCreatedEvidence(
            intent.package.id, int(user.total_quota_bytes or 0), intent.duration_days or None))
    if purchase is not None and operation.operation_type in ("create_user", "purchase"):
        rec("purchase_created", "purchase", purchase,
            effects.PurchaseCreatedEvidence(days=intent.package.duration_days or None))
    for slot_key, connection in connections:
        rec("connection_created", f"conn:{slot_key}", connection, effects.ConnectionCreatedEvidence(f"conn:{slot_key}"))
    if renewal_evidence:
        rec("purchase_renewed", "renew", purchase if purchase is not None else user, renewal_evidence[0])
    if entry is not None:
        entry.approval_uuid = approval_uuid
        db.flush()
        rec("ledger_sale", "sale", entry)
    for credit in credit_entries:
        rec("ledger_credit_spend", "credit_spend", credit)
    optional = {}
    for name, outcome in (("card_payment", _card_payment(db, rec, intent, approval_uuid, manifest)),
                          ("discount", _discount(db, rec, intent, user, manifest)),
                          ("referral", _referral(db, rec, intent, user, purchase, manifest))):
        if outcome is not None:
            optional[name] = outcome
    written = _written(db, approval_uuid)
    missing = sorted(f"{k[0]}:{k[1]}" for k, r in manifest.items() if r.requirement == ri.REQUIRED and k not in written)
    unexpected = sorted(f"{t}:{k}" for t, k in written - set(manifest))
    if missing or unexpected:
        if rec.strict:
            raise ApprovalEffectFailed("effect_manifest_mismatch")
        runtime.record_shadow_event(db, pending_source_instance_id=row["pending_source_instance_id"],
                                    pending_local_id=row["pending_local_id"], stage="finalize",
                                    error_code="missing_effects" if missing else "unexpected_effects",
                                    approval_uuid=approval_uuid)
        epoch = db.scalar(select(rv.wallet_runtime_state.c.epoch).where(rv.wallet_runtime_state.c.id == 1))
        approval_exec.open_recovery(db, approval_uuid, operation.tenant_scope_key, epoch=epoch)
    else:
        updated = db.execute(rv.receipt_approvals.update().where(
            rv.receipt_approvals.c.approval_uuid == approval_uuid,
            rv.receipt_approvals.c.version == operation.approval_execution_version,
            rv.receipt_approvals.c.state == "mutating").values(state="completed", completed_at=func.now())).rowcount
        if updated != 1:
            raise approval_exec.ApprovalBindingLost("approval_binding_lost")
    close_recovery(db, approval_uuid, "completed")
    return json.dumps(optional, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def fail(db, operation, row) -> None:
    """T_comp: the approval had no effect and fails with the operation."""
    approval_exec.check(db, operation, row)
    has_effect = db.execute(select(rv.receipt_approval_effects.c.id).where(
        rv.receipt_approval_effects.c.approval_uuid == operation.approval_uuid).limit(1)).first()
    if row["state"] != "mutating" or has_effect is not None:
        raise approval_exec.ApprovalBindingLost("approval_binding_lost")
    updated = db.execute(rv.receipt_approvals.update().where(
        rv.receipt_approvals.c.approval_uuid == operation.approval_uuid,
        rv.receipt_approvals.c.version == operation.approval_execution_version,
        rv.receipt_approvals.c.state == "mutating").values(state="failed", failed_at=func.now())).rowcount
    if updated != 1:
        raise approval_exec.ApprovalBindingLost("approval_binding_lost")
    close_recovery(db, operation.approval_uuid, "completed")


def close_recovery(db, approval_uuid: str, state: str) -> None:
    """A running approval_recovery ends with the transaction that resolved it."""
    ops = rv.wallet_operations
    db.execute(ops.update().where(ops.c.operation_type == "approval_recovery", ops.c.business_key == approval_uuid,
                                  ops.c.state == "running").values(
        state=state, completed_at=dt.datetime.utcnow(), version=ops.c.version + 1))


# ------------------------------------------------------------------ superadmin decision
def decide(session_factory, approval_uuid: str, decision: str, admin_id: int, reason: str) -> dict:
    """continue / cancel an approval_recovery (superadmin, password checked
    by the route). One commit: the recovery 'running' with actor and reason,
    the operation back to its forward state or into compensation. The key
    check is then replaced by this recovery until it closes."""
    from .. import models_provisioning as mp
    from . import provisioning_transitions as transitions, resource_leases
    if decision not in ("continue", "cancel") or not (reason or "").strip():
        raise HTTPException(422, "approval_recovery_request_invalid")
    ops = rv.wallet_operations
    with session_factory() as db:
        resource_leases.begin_business(db)
        db.execute(select(rv.wallet_runtime_state.c.id).with_for_update(read=True)).all()
        runtime.read_state(db, lock=True, shared_lock=True)
        row = approval_exec._approval(db, approval_uuid, lock=True)
        recovery = db.execute(select(ops).where(ops.c.operation_type == "approval_recovery",
                                                ops.c.business_key == approval_uuid).with_for_update()
                              ).mappings().one_or_none()
        if row is None or recovery is None or recovery["state"] != "cleanup_required":
            raise HTTPException(409, "approval_recovery_not_open")
        if decision == "continue" and recovery["created_at"] is not None and (
                dt.datetime.utcnow() - recovery["created_at"]).days >= CONTINUE_CLOSES_AFTER_DAYS:
            # Lifecycle 18 decision 12: delivering a service a month later is
            # no longer offered; only cancel remains.
            raise HTTPException(409, "approval_recovery_continue_expired")
        operation = db.execute(select(mp.ProvisioningOperation).where(
            mp.ProvisioningOperation.approval_uuid == approval_uuid,
            mp.ProvisioningOperation.approval_execution_version == row["version"]).with_for_update()
        ).scalar_one_or_none()
        if operation is None:
            raise HTTPException(409, "approval_recovery_without_operation")
        values = dict(state="running", actor_kind="admin", actor_id=admin_id, reason=reason.strip()[:2000],
                      started_at=dt.datetime.utcnow(), version=ops.c.version + 1)
        if operation.state == "completed":
            # A shadow approval whose sale completed but whose manifest did
            # not validate: the superadmin accepts what was recorded.
            if decision == "cancel":
                raise HTTPException(409, "approval_recovery_cancel_requires_void")
            db.execute(rv.receipt_approvals.update().where(
                rv.receipt_approvals.c.approval_uuid == approval_uuid,
                rv.receipt_approvals.c.state == "mutating").values(state="completed", completed_at=func.now()))
            values.update(state="completed", completed_at=dt.datetime.utcnow(),
                          result_snapshot=json.dumps({"decision": decision, "operation_state": "completed"}))
            db.execute(ops.update().where(ops.c.id == recovery["id"]).values(**values))
            db.commit()
            return {"approval_uuid": approval_uuid, "decision": decision, "operation_state": "completed"}
        if operation.state != "cleanup_required" or operation.error_code not in approval_exec.LOST_CODES:
            raise HTTPException(409, "approval_recovery_operation_state")
        if decision == "cancel":
            transitions.begin_compensation(db, operation.id, "cancelled_by_recovery")
        else:
            steps = db.execute(select(mp.ProvisioningStep).where(
                mp.ProvisioningStep.operation_id == operation.id, mp.ProvisioningStep.direction == "create")
            ).scalars().all()
            done = all(step.state == ("staged" if step.backend == "radius_ppp" else "remote_created") for step in steps)
            transitions._write_operation(db, operation, "remote_complete" if done and steps else "provisioning",
                                         error_code=None, next_retry_at=None)
        values["result_snapshot"] = json.dumps({"decision": decision})
        db.execute(ops.update().where(ops.c.id == recovery["id"]).values(**values))
        db.commit()
        return {"approval_uuid": approval_uuid, "decision": decision, "operation_id": operation.id}
