"""The joint activation of phase P9a (Receipt Void 5.5, 14.2): approval
registration 'required' AND loyalty 'payment_event', one UPDATE of one row,
in ONE transaction - and its narrow rollback.

Preconditions, all checked again inside the transaction (any failure:
rollback, 409 with the precondition's code and the list):
  wallet_not_enforced          P9 done (wallet_runtime_state.phase)
  key_identity_not_ready       fresh readiness (recheck before the transaction)
  ha_requires_external_fencing HA off (no coordinator exists)
  loyalty_cutover_blocked      no User / Purchase with a reserved renewal
  approval_protocol_outdated   every enabled managed remote key reported the protocol
  provisioning_not_ready       p6_ready(), every type durable
Lock order: wallet_runtime_state (shared) -> receipt_approval_runtime_state
(exclusive). Already active -> 200 without change (no second generation).

deactivate(): only while this generation has neither an approval nor a
loyalty purchase event (both counted under the exclusive lock); otherwise
409 activation_irreversible.
"""
from __future__ import annotations

import datetime as dt

from fastapi import HTTPException
from sqlalchemy import func, or_, select, text

from .. import models, models_receipt_void as rv
from . import bot_auth, loyalty_service, receipt_void_schema

LIST_LIMIT = 50


def _begin(db):
    db.rollback()
    if db.get_bind().dialect.name == "sqlite":
        db.execute(text("BEGIN IMMEDIATE"))


def _lock(db):
    sqlite = db.get_bind().dialect.name == "sqlite"
    wallet = select(rv.wallet_runtime_state).where(rv.wallet_runtime_state.c.id == 1)
    state = select(rv.receipt_approval_runtime_state).where(rv.receipt_approval_runtime_state.c.id == 1)
    if not sqlite:
        wallet, state = wallet.with_for_update(read=True), state.with_for_update()
    return db.execute(wallet).mappings().one(), db.execute(state).mappings().one()


def _refuse(code, items=None):
    detail = {"error": code}
    if items is not None:
        detail["items"] = items
    raise HTTPException(409, detail)


def _preconditions(db, wallet, state) -> None:
    from . import provisioning_readiness
    from .. import models_provisioning as mp
    if wallet["phase"] != "enforced":
        _refuse("wallet_not_enforced")
    if not state["key_identity_ready"]:
        _refuse("key_identity_not_ready", [state["key_identity_failure"]])
    panel = db.get(models.PanelSettings, 1)
    if panel is not None and panel.ha_enabled and wallet["external_fencing"] != "coordinator":
        _refuse("ha_requires_external_fencing")
    reserved_users = db.query(models.User.id, models.User.username).filter(or_(
        models.User.reserved_quota_bytes > 0, models.User.reserved_duration_days > 0,
        models.User.reserved_package_id.isnot(None))).limit(LIST_LIMIT).all()
    reserved_purchases = db.query(models.Purchase.id, models.User.username).join(
        models.User, models.User.id == models.Purchase.user_id).filter(or_(
            models.Purchase.reserved_quota_bytes > 0, models.Purchase.reserved_duration_days > 0,
            models.Purchase.reserved_package_id.isnot(None))).limit(LIST_LIMIT).all()
    if reserved_users or reserved_purchases:
        _refuse("loyalty_cutover_blocked",
                [{"type": "User", "id": uid, "username": name} for uid, name in reserved_users]
                + [{"type": "Purchase", "id": pid, "username": name} for pid, name in reserved_purchases])
    from .receipt_approval_registration import APPROVAL_PROTOCOL_VERSION
    outdated = db.query(models.ApiKey.id, models.ApiKey.label).filter(
        models.ApiKey.enabled.is_(True), models.ApiKey.key_type == bot_auth.KeyType.REMOTE_SHARED_BOT,
        or_(models.ApiKey.approval_protocol_version.is_(None),
            models.ApiKey.approval_protocol_version < APPROVAL_PROTOCOL_VERSION)).limit(LIST_LIMIT).all()
    if outdated:
        _refuse("approval_protocol_outdated", [{"key_id": kid, "label": label} for kid, label in outdated])
    try:
        ready, readiness = provisioning_readiness.p6_ready(db)
    except Exception:  # noqa: BLE001
        ready, readiness = False, [{"code": "provisioning_status_unavailable"}]
    legacy = sorted(r.operation_type for r in db.query(mp.ProvisioningTypeMode).filter(
        mp.ProvisioningTypeMode.mode != "durable"))
    if not ready or legacy:
        _refuse("provisioning_not_ready", {"readiness": readiness, "legacy_types": legacy})


def activate(db, actor_admin_id) -> dict:
    receipt_void_schema.assert_ready()
    _begin(db)
    try:
        wallet, state = _lock(db)
        if state["registration_mode"] == "required" and state["loyalty_timing_mode"] == "payment_event":
            db.rollback()
            return {"activated": False, "already_active": True,
                    "completeness_generation": int(state["completeness_generation"])}
        _preconditions(db, wallet, state)
        generation = int(state["completeness_generation"]) + 1
        settings = db.get(models.PanelSettings, 1) or models.PanelSettings(id=1)
        threshold, credit, quota = loyalty_service._policy_values(settings)
        epoch = loyalty_service._new_epoch(db, threshold=threshold, credit=credit, quota_bytes=quota,
                                           generation=generation, actor_id=actor_admin_id)
        baselines = loyalty_service.capture_baselines(db, generation, epoch, threshold)
        table = rv.receipt_approval_runtime_state
        now = dt.datetime.utcnow()
        changed = db.execute(table.update().where(table.c.id == 1, table.c.version == state["version"]).values(
            registration_mode="required", loyalty_timing_mode="payment_event", completeness_generation=generation,
            activated_at=now, updated_at=now, version=table.c.version + 1)).rowcount
        if changed != 1:
            raise HTTPException(409, "receipt_approval_runtime_changed")
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return {"activated": True, "completeness_generation": generation, "epoch_id": epoch, "baselines": baselines}


def deactivate(db) -> dict:
    receipt_void_schema.assert_ready()
    _begin(db)
    try:
        _wallet, state = _lock(db)
        if state["registration_mode"] != "required":
            _refuse("not_active")
        generation = int(state["completeness_generation"])
        approvals = db.execute(select(func.count()).select_from(rv.receipt_approvals).where(
            rv.receipt_approvals.c.completeness_generation == generation)).scalar_one()
        events = db.execute(select(func.count()).select_from(rv.loyalty_purchase_events).where(
            rv.loyalty_purchase_events.c.completeness_generation == generation)).scalar_one()
        if approvals or events:
            _refuse("activation_irreversible", {"approvals": int(approvals), "loyalty_purchases": int(events)})
        epochs = rv.loyalty_policy_epochs
        now = dt.datetime.utcnow()
        db.execute(epochs.update().where(epochs.c.closed_at.is_(None)).values(closed_at=now))
        table = rv.receipt_approval_runtime_state
        rate = "shadow" if state["auto_rate_limit_mode"] != "off" else "off"
        db.execute(table.update().where(table.c.id == 1).values(
            registration_mode="shadow", loyalty_timing_mode="legacy_activation", auto_rate_limit_mode=rate,
            updated_at=now, version=table.c.version + 1))
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return {"deactivated": True, "completeness_generation": generation}
