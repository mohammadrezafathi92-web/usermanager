"""Idempotent registration of a receipt approval (Receipt Void design 5.1,
5.6, 6.4; rollout phase P3).

register() is the transactional core: one pending receipt has at most one
live approval; asking again with the same intent returns the same one, a
different intent is refused for ever, a failed one is retried in place, and
a human can take an approval over from a bot or from another key - but only
while it has produced no effect.

begin() wraps it in the off / shadow contract: in 'off' nothing is written,
and in 'shadow' a registration that fails for ANY reason is logged and the
caller is told to carry on the way it always did.

The auto path also runs the central evaluator and the 60-minute cap
(receipt_approval_auto); in shadow both are recorded and gate nothing.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Optional

from fastapi import HTTPException
import time

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from .. import models
from . import bot_auth, hierarchy, receipt_approval_auto as auto_policy, receipt_approval_runtime as runtime
from . import receipt_approval_intent as ri

log = logging.getLogger(__name__)

AUTO, MANUAL = "auto", "manual"
OPEN_STATES = ("registered", "failed")


class RegistrationRejected(Exception):
    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status = status
        self.code = code


@dataclass(frozen=True)
class Registration:
    created: bool                    # True -> 201, False -> 200
    approval_uuid: str
    execution_token: int             # the approval row's version
    registration_seq: int
    state: str


@dataclass(frozen=True)
class Approver:
    telegram_id: int
    evidence_kind: str               # linked_admin | owner_seller | card_approver
    admin_id: Optional[int]


def _tables():
    from .. import models_receipt_void as rv
    return (rv.receipt_approvals, rv.receipt_approval_expected_effects, rv.receipt_approval_effects,
            rv.receipt_approval_authority_events)


def _now() -> dt.datetime:
    return dt.datetime.utcnow()


def caller_key_instance(db: Session, principal: bot_auth.BotPrincipal) -> Optional[str]:
    """None for the in-process bot; otherwise the caller key's instance
    uuid. A key without one cannot register or execute anything."""
    if principal.is_internal or principal.key_id is None:
        return None
    key = db.get(models.ApiKey, principal.key_id)
    if key is None or not key.enabled or not key.key_instance_uuid:
        raise RegistrationRejected(403, "key_identity_missing")
    return key.key_instance_uuid


def _key_is_live(db: Session, key_instance_uuid: Optional[str]) -> bool:
    if key_instance_uuid is None:
        return True                  # in-process: there is no key to revoke
    key = db.query(models.ApiKey).filter(models.ApiKey.key_instance_uuid == key_instance_uuid).first()
    return bool(key and key.enabled)


def _shared_bot_admin_ids(db: Session) -> set[int]:
    """The Telegram ids in the shared bot's own admin list
    (BotSettings.admin_ids, the «آیدی عددی ادمین» field)."""
    row = db.get(models.BotSettings, 1)
    ids = set()
    for part in ((row.admin_ids if row else "") or "").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            ids.add(int(part))
    return ids


def resolve_approver(db: Session, telegram_id: Optional[int], owner_admin_id: Optional[int],
                     payment_card_id: Optional[int], principal: Optional[bot_auth.BotPrincipal] = None) -> Approver:
    """Design 6.4: the ways the bot accepts an approver today, now checked
    by the backend. Nothing matches -> 403.

    Besides the three of the design (the owner, an admin above the owner,
    the card's own approver) the SHARED bot has a fourth that the design
    text missed and production uses: a Telegram id in the bot's own admin
    list, with no panel account behind it (telegram_bot/admin_scope.py's
    "config only" admin - unscoped, handles every request of that bot). It
    is recorded as 'linked_admin' with no admin id; only a principal that
    is not bound to one owner (the shared bot) may claim it."""
    if not telegram_id:
        raise RegistrationRejected(403, "manual_approver_not_authorized")
    admin = db.query(models.AdminUser).filter(models.AdminUser.telegram_id == int(telegram_id)).first()
    if admin is not None:
        if owner_admin_id is not None and admin.id == owner_admin_id:
            return Approver(int(telegram_id), "owner_seller", admin.id)
        if owner_admin_id is None:
            if admin.is_superadmin:
                return Approver(int(telegram_id), "linked_admin", admin.id)
        elif owner_admin_id in (hierarchy.owned_admin_ids(db, admin) or set()):
            return Approver(int(telegram_id), "linked_admin", admin.id)
    if payment_card_id is not None:
        card = db.get(models.PaymentCard, payment_card_id)
        if card is not None and card.approval_telegram_id is not None and int(card.approval_telegram_id) == int(telegram_id):
            return Approver(int(telegram_id), "card_approver", None)
    if principal is not None and principal.valid and principal.owner_admin_id is None \
            and int(telegram_id) in _shared_bot_admin_ids(db):
        return Approver(int(telegram_id), "linked_admin", None)
    raise RegistrationRejected(403, "manual_approver_not_authorized")


def _response(row, created: bool = False) -> Registration:
    return Registration(created=created, approval_uuid=row["approval_uuid"], execution_token=int(row["version"]),
                        registration_seq=int(row["registration_seq"]), state=row["state"])


def _reload(db: Session, approval_uuid: str) -> dict:
    approvals = _tables()[0]
    return dict(db.execute(select(approvals).where(approvals.c.approval_uuid == approval_uuid)).mappings().one())


def _change_authority(db: Session, last: dict, *, reason: str, to_mode: str, approver: Optional[Approver],
                      to_key: Optional[str]) -> dict:
    """Retry / approver change / takeover: one guarded UPDATE that bumps the
    version (the old execution token dies) plus its append-only event."""
    approvals, _expected, effects, authority = _tables()
    values = {"state": "registered", "failed_at": None, "version": approvals.c.version + 1,
              "execution_key_instance_uuid": to_key}
    if to_mode == MANUAL:
        values.update(approval_mode=MANUAL, approved_by_telegram_id=approver.telegram_id,
                      approver_evidence_kind=approver.evidence_kind, approved_by_admin_id=approver.admin_id)
    has_effect = select(effects.c.id).where(effects.c.approval_uuid == last["approval_uuid"]).exists()
    result = db.execute(approvals.update().where(
        approvals.c.approval_uuid == last["approval_uuid"], approvals.c.state.in_(OPEN_STATES),
        approvals.c.version == last["version"], ~has_effect).values(**values))
    if result.rowcount != 1:
        raise RegistrationRejected(409, "approval_superseded")
    db.execute(authority.insert().values(
        approval_uuid=last["approval_uuid"], reason=reason, from_mode=last["approval_mode"], to_mode=to_mode,
        from_key_instance_uuid=last["execution_key_instance_uuid"], to_key_instance_uuid=to_key,
        from_approver_telegram_id=last["approved_by_telegram_id"],
        to_approver_telegram_id=approver.telegram_id if to_mode == MANUAL else None,
        from_approver_evidence_kind=last["approver_evidence_kind"],
        to_approver_evidence_kind=approver.evidence_kind if to_mode == MANUAL else None,
        from_state=last["state"], version_before=last["version"], version_after=last["version"] + 1,
        created_at=_now()))
    return _reload(db, last["approval_uuid"])


def _register_once(db: Session, principal: bot_auth.BotPrincipal, intent: ri.ApprovalIntent, approval_mode: str,
                   approved_by_telegram_id: Optional[int], registered_under_mode: str,
                   local_decision: Optional[str] = None, rate_limit_mode: str = runtime.OFF) -> Registration:
    approvals, expected, effects, _authority = _tables()
    if approval_mode == MANUAL and not ri.is_managed_principal(principal):
        raise RegistrationRejected(403, "manual_requires_managed_bot")
    key_uuid = caller_key_instance(db, principal)
    try:
        target = ri.resolve_target(db, principal, intent)
    except ri.IntentRejected as exc:
        raise RegistrationRejected(exc.status, exc.code)
    except HTTPException as exc:
        raise RegistrationRejected(exc.status_code, "out_of_scope")
    intent_hash = ri.intent_hash(intent, target)

    last = db.execute(
        select(approvals).where(approvals.c.pending_source_instance_id == intent.pending_source_instance_id,
                                approvals.c.pending_local_id == intent.pending_local_id)
        .order_by(approvals.c.registration_seq.desc()).limit(1).with_for_update()).mappings().first()
    last = dict(last) if last else None

    if last is not None and last["immutable_intent_hash"] != intent_hash:
        raise RegistrationRejected(409, "intent_changed")       # always, even after 'cancelled'

    approver = None
    if approval_mode == MANUAL:
        approver = resolve_approver(db, approved_by_telegram_id, target.owner_admin_id, intent.payment_card_id, principal)

    if last is not None and last["state"] != "cancelled":
        same_key = key_uuid == last["execution_key_instance_uuid"]
        has_effects = db.execute(select(effects.c.id).where(effects.c.approval_uuid == last["approval_uuid"])).first() is not None
        if last["state"] in OPEN_STATES and not has_effects:
            if approval_mode == AUTO:
                if last["approval_mode"] == MANUAL:
                    raise RegistrationRejected(409, "approval_is_manual")
                if not same_key:
                    raise RegistrationRejected(403, "execution_principal_mismatch")
                if last["state"] == "registered":
                    return _response(last)
                return _response(_change_authority(db, last, reason="retry", to_mode=AUTO, approver=None, to_key=key_uuid))
            if same_key and last["approval_mode"] == MANUAL:
                same_approver = last["approved_by_telegram_id"] == approver.telegram_id
                if last["state"] == "registered" and same_approver:
                    return _response(last)
                return _response(_change_authority(db, last, reason="retry" if same_approver else "approver_change",
                                                   to_mode=MANUAL, approver=approver, to_key=key_uuid))
            reason = "manual_takeover" if _key_is_live(db, last["execution_key_instance_uuid"]) else "revoked_key_takeover"
            return _response(_change_authority(db, last, reason=reason, to_mode=MANUAL, approver=approver, to_key=key_uuid))
        if same_key and last["approval_mode"] == approval_mode:
            return _response(last)                              # already running or done: report, change nothing
        raise RegistrationRejected(409, "approval_has_effects")

    if approval_mode == AUTO:
        if intent.kind not in ("new", "renew"):
            raise RegistrationRejected(403, "auto_approval_policy_denied:kind_not_auto_eligible")
        if target.telegram_id is None:
            raise RegistrationRejected(403, "auto_approval_policy_denied:no_telegram_identity")
    try:
        manifest = ri.build_manifest(db, intent, target)
    except ri.IntentRejected as exc:
        raise RegistrationRejected(exc.status, exc.code)

    now = _now()
    approval_uuid = str(uuid.uuid4())
    policy, subject, outcome, reason, retry_after = {}, None, None, None, None
    if approval_mode == AUTO:
        # The central decision and the 60-minute cap. In shadow neither gates
        # anything: the bot's local decision stays the reference and this
        # is only recorded next to it.
        settings = auto_policy.load_settings(db, target.owner_admin_id)
        returning = auto_policy.history_returning(db, target.tenant_scope_key, target.telegram_id)
        allowed, reason = auto_policy.evaluate(
            kind=intent.kind, amount=intent.amount, telegram_id=target.telegram_id, settings=settings,
            returning=returning, hour=auto_policy.local_hour(now))
        policy = auto_policy.policy_snapshot(settings, allowed, reason, returning)
        subject = auto_policy.lock_subject(db, target.tenant_scope_key, target.telegram_id)
        retry_after = auto_policy.retry_after_seconds(subject["last_granted_at"] if subject else None, now)
        if not allowed:
            outcome = "policy_denied"
        elif retry_after is not None and rate_limit_mode != runtime.OFF:
            outcome, reason = "shadow_would_limit", "within_60_minutes"
        else:
            outcome, reason = "granted", None
    db.execute(approvals.insert().values(
        approval_uuid=approval_uuid, pending_source_instance_id=intent.pending_source_instance_id,
        pending_local_id=intent.pending_local_id,
        registration_seq=(last["registration_seq"] + 1) if last else 0,
        immutable_intent_hash=intent_hash, manifest_hash=ri.manifest_hash(manifest),
        supersedes_approval_uuid=last["approval_uuid"] if last else None,
        registered_under_mode=registered_under_mode, completeness_generation=0,
        registered_by_key_instance_uuid=key_uuid, execution_key_instance_uuid=key_uuid,
        registered_by_api_key_id=principal.key_id, kind=intent.kind, target_shape=target.shape,
        approval_mode=approval_mode, auto_granted_at=now if approval_mode == AUTO else None,
        original_approval_mode=approval_mode,
        original_approved_by_telegram_id=approver.telegram_id if approver else None,
        original_approver_evidence_kind=approver.evidence_kind if approver else None,
        approved_by_admin_id=approver.admin_id if approver else None,
        approved_by_telegram_id=approver.telegram_id if approver else None,
        approver_evidence_kind=approver.evidence_kind if approver else None,
        auto_approve_policy_snapshot=runtime.canonical_json(policy), owner_admin_id_snapshot=target.owner_admin_id,
        tenant_scope_key=target.tenant_scope_key, telegram_id_snapshot=target.telegram_id,
        telegram_id_source=target.telegram_id_source, target_user_id_snapshot=target.user_id,
        target_username_snapshot=intent.target_username, receipt_file_id_snapshot=intent.receipt_file_id,
        amount_snapshot=intent.amount, payment_card_id_snapshot=intent.payment_card_id,
        package_id_snapshot=intent.package_id, state="registered", created_at=now, version=0))
    for row in manifest:
        db.execute(expected.insert().values(
            approval_uuid=approval_uuid, effect_type=row.effect_type, effect_key=row.effect_key,
            requirement=row.requirement, expected=runtime.canonical_json(row.expected), created_at=now))
    if approval_mode == AUTO:
        auto_policy.record_grant(db, target.tenant_scope_key, target.telegram_id, approval_uuid, now, subject)
        auto_policy.record_event(
            db, tenant_scope_key=target.tenant_scope_key, telegram_id=target.telegram_id, outcome=outcome,
            reason_code=reason, local_decision=local_decision, approval_uuid=approval_uuid,
            pending_source_instance_id=intent.pending_source_instance_id, pending_local_id=intent.pending_local_id,
            retry_after=retry_after if outcome == "shadow_would_limit" else None, now=now)
    return _response(_reload(db, approval_uuid), created=True)


def register(db: Session, principal: bot_auth.BotPrincipal, intent: ri.ApprovalIntent, *, approval_mode: str,
             approved_by_telegram_id: Optional[int] = None, registered_under_mode: str = runtime.SHADOW,
             local_decision: Optional[str] = None, rate_limit_mode: str = runtime.OFF) -> Registration:
    """In the caller's transaction (inside one savepoint). Two concurrent
    registrations of one pending compute the same seq; the loser's
    IntegrityError is retried exactly once and then finds the winner."""
    if approval_mode not in (AUTO, MANUAL):
        raise RegistrationRejected(422, "invalid_approval_mode")
    for attempt in (1, 2):
        try:
            with db.begin_nested():
                return _register_once(db, principal, intent, approval_mode, approved_by_telegram_id,
                                      registered_under_mode, local_decision, rate_limit_mode)
        except IntegrityError:
            if attempt == 2:
                raise RegistrationRejected(409, "registration_conflict")
    raise AssertionError("unreachable")


WRITE_ATTEMPTS = 3


def take_write_lock(db: Session) -> None:
    """SQLite only: start the transaction as a WRITE transaction (design
    5.1 / 6.5, "SQLite: BEGIN IMMEDIATE").

    A registration reads first (target, approver, last approval) and writes
    last. On SQLite a transaction that began by reading cannot be upgraded
    once any other connection has committed in between - the write fails at
    once with "database is locked", whatever the busy timeout. On a panel
    whose RADIUS accounting commits all the time that is not rare (seen on
    production 2026-10-04). Taking the write lock up front makes this
    transaction wait its turn instead, and then read current data."""
    if db.get_bind().dialect.name != "sqlite":
        return
    db.rollback()                       # end whatever read transaction is open
    db.execute(text("BEGIN IMMEDIATE"))


def describe_error(exc: BaseException) -> str:
    """A short code for a shadow event: the exception class, no values."""
    return f"registration_error:{type(getattr(exc, 'orig', None) or exc).__name__}"[:48]


def begin(db: Session, principal: bot_auth.BotPrincipal, intent: ri.ApprovalIntent, *, approval_mode: str,
          approved_by_telegram_id: Optional[int] = None, local_decision: Optional[str] = None) -> dict:
    """The structured answer of design 5.6. Commits. Only the backend
    decides the mode; nothing in the request can claim one."""
    state = runtime.read_state(db)
    mode = runtime.effective_registration_mode(state)
    if mode == runtime.OFF:
        return {"mode": "off", "approval_uuid": None, "proceed_legacy": True}
    if mode == runtime.BLOCKED:
        raise RegistrationRejected(503, "receipt_approval_blocked")
    if mode == runtime.REQUIRED:
        raise RegistrationRejected(503, "required_mode_not_available")      # exists only after phase P9a

    error = runtime.shadow_error(state)
    result = None
    for attempt in range(1, WRITE_ATTEMPTS + 1):
        if error is not None:
            break
        try:
            take_write_lock(db)
            result = register(db, principal, intent, approval_mode=approval_mode,
                              approved_by_telegram_id=approved_by_telegram_id, registered_under_mode=runtime.SHADOW,
                              local_decision=local_decision,
                              rate_limit_mode=runtime.effective_auto_rate_limit_mode(state))
            db.commit()
            break
        except RegistrationRejected as exc:
            db.rollback()
            error = exc.code
        except OperationalError as exc:              # database busy: wait and try again
            db.rollback()
            if attempt == WRITE_ATTEMPTS:
                log.warning("receipt approval registration unavailable after %s attempts: %s", attempt,
                            getattr(exc, "orig", exc))
                error = describe_error(exc)
            else:
                time.sleep(0.3 * attempt)
        except Exception as exc:
            db.rollback()
            log.exception("receipt approval shadow registration failed (%s)", describe_error(exc))
            error = describe_error(exc)
    if result is None:
        runtime.record_shadow_event(db, pending_source_instance_id=str(intent.pending_source_instance_id or "?"),
                                    pending_local_id=intent.pending_local_id if isinstance(intent.pending_local_id, int) else 0,
                                    stage="registration", error_code=error)
        db.commit()
        return {"mode": "shadow", "approval_uuid": None, "shadow_error": error, "proceed_legacy": True}
    return {"mode": "shadow", "approval_uuid": result.approval_uuid, "execution_token": result.execution_token,
            "registration_seq": result.registration_seq, "state": result.state, "created": result.created,
            "proceed_legacy": False, "central_decision": central_decision(db, result.approval_uuid)}


def central_decision(db: Session, approval_uuid: str) -> dict:
    """What the central evaluator said when the approval was registered -
    diagnostic only while in shadow. Empty for a manual approval."""
    approvals = _tables()[0]
    raw = db.execute(select(approvals.c.auto_approve_policy_snapshot)
                     .where(approvals.c.approval_uuid == approval_uuid)).scalar()
    snapshot = json.loads(raw or "{}")
    return {k: snapshot[k] for k in ("central_allowed", "central_reason", "returning") if k in snapshot}


def manifest_of(db: Session, approval_uuid: str) -> list[dict]:
    expected = _tables()[1]
    rows = db.execute(select(expected).where(expected.c.approval_uuid == approval_uuid)
                      .order_by(expected.c.effect_type, expected.c.effect_key)).mappings().all()
    return [{"effect_type": r["effect_type"], "effect_key": r["effect_key"], "requirement": r["requirement"],
             "expected": json.loads(r["expected"])} for r in rows]


# ---------------------------------------------------------------- execution state (design 6.5)
def begin_mutating(db: Session, approval_uuid: str) -> bool:
    """registered -> mutating, when the approval's first effect is about to
    be written. In the caller's transaction. In shadow this is bookkeeping
    only: no execution token and no precondition is enforced (those belong
    to 'required'). Returns whether the row moved."""
    approvals = _tables()[0]
    result = db.execute(approvals.update().where(
        approvals.c.approval_uuid == approval_uuid, approvals.c.state == "registered")
        .values(state="mutating", mutating_at=_now()))
    return result.rowcount == 1


def finalize(db: Session, principal: bot_auth.BotPrincipal, approval_uuid: str, *, reported_failure: bool = False) -> dict:
    """Closes one approval, in the caller's transaction:

      every required effect present      -> completed
      no effect at all + the bot says it
      failed                             -> failed (it can be retried)
      anything else                      -> unchanged

    Compares only the SET of (type, key) against the manifest - each
    effect's values were already checked when it was written. An approval
    that is already closed is reported as it is."""
    approvals, expected, effects, _authority = _tables()
    row = db.execute(select(approvals).where(approvals.c.approval_uuid == approval_uuid).with_for_update()).mappings().first()
    if row is None:
        raise RegistrationRejected(404, "approval_not_found")
    if caller_key_instance(db, principal) != row["execution_key_instance_uuid"]:
        raise RegistrationRejected(403, "execution_principal_mismatch")
    manifest = db.execute(select(expected.c.effect_type, expected.c.effect_key, expected.c.requirement)
                          .where(expected.c.approval_uuid == approval_uuid)).all()
    written = {(r[0], r[1]) for r in db.execute(
        select(effects.c.effect_type, effects.c.effect_key).where(effects.c.approval_uuid == approval_uuid))}
    known = {(m[0], m[1]) for m in manifest}
    missing = sorted(f"{m[0]}:{m[1]}" for m in manifest if m[2] == ri.REQUIRED and (m[0], m[1]) not in written)
    unexpected = sorted(f"{t}:{k}" for t, k in written - known)
    state = row["state"]
    if state in ("registered", "mutating"):
        if written and not missing and not unexpected:
            state = "completed"
            db.execute(approvals.update().where(approvals.c.approval_uuid == approval_uuid)
                       .values(state=state, completed_at=_now()))
        elif not written and reported_failure:
            state = "failed"
            db.execute(approvals.update().where(approvals.c.approval_uuid == approval_uuid)
                       .values(state=state, failed_at=_now()))
        elif not reported_failure:
            runtime.record_shadow_event(
                db, pending_source_instance_id=row["pending_source_instance_id"], pending_local_id=row["pending_local_id"],
                stage="finalize", error_code="missing_effects" if missing else "unexpected_effects",
                approval_uuid=approval_uuid)
    return {"state": state, "effect_count": len(written), "missing_effects": missing, "unexpected_effects": unexpected}
