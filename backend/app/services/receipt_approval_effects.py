"""The ONE writer of receipt_approval_effects (Receipt Void design 5.2 and
5.3; rollout phase P3).

An effect is something an approval really did - a user, a purchase, a
connection, a ledger row, a card payment. record_effect() accepts it only
if the approval's manifest expected exactly that:

    the (effect_type, effect_key) row is in the manifest,
    the typed evidence of the service that did the mutation is consistent,
    and the projection built from the real resource equals `expected`.

Nothing here takes a projection, an evidence dict or a snapshot from a
request: the input is the ORM row plus a frozen evidence object, and no
connection credential is ever passed in.

record_effect_shadow() is the shadow-mode wrapper: any rejection is logged
as a shadow event and the mutation it describes is left untouched.

Not covered yet (their manifest rows do not exist yet either): referral,
quota-reward and loyalty effects.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import asdict, dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from . import receipt_approval_runtime as runtime
from .receipt_approval_runtime import canonical_json

log = logging.getLogger(__name__)

NOT_IN_MANIFEST = "effect_not_in_manifest"
EVIDENCE_INVALID = "effect_evidence_invalid"
MANIFEST_MISMATCH = "effect_manifest_mismatch"

# What a snapshot may contain. The design's list, plus the before/after
# fields of the renewal and wallet evidence (the same section requires those
# to be stored for a later void). Any other key is REFUSED, not dropped.
SNAPSHOT_KEYS = frozenset({
    "id", "username", "node_id", "node_name", "protocol", "flow", "package_id", "package_name", "amount",
    "quota_bytes", "quota_before", "quota_after", "before_was_unlimited", "days", "expire_at", "counter_before",
    "counter_after", "ledger_kind",
    "renewal_mode", "effective_now", "add_quota_bytes", "add_days", "package_id_requested",
    "q_before", "u_before", "e_before", "rq_before", "rd_before", "rp_before", "pk_before",
    "q_after", "u_after", "e_after", "rq_after", "rd_after", "rp_after", "pk_after",
    "balance_before", "balance_after", "rewards_count", "slot_key",
})


class EffectRejected(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


# ---------------------------------------------------------------- evidence
@dataclass(frozen=True)
class UserCreatedEvidence:
    package_id: int
    quota_bytes: int
    days: Optional[int]


@dataclass(frozen=True)
class PurchaseCreatedEvidence:
    days: Optional[int]


@dataclass(frozen=True)
class ConnectionCreatedEvidence:
    slot_key: str


@dataclass(frozen=True)
class DiscountEvidence:
    used_count_before: int
    used_count_after: int


@dataclass(frozen=True)
class WalletCreditEvidence:
    balance_before: int
    balance_after: int
    rewards_count: int = 1


@dataclass(frozen=True)
class PurchaseRenewedEvidence:
    renewal_mode: str                # 'reserved' | 'immediate_reset'
    effective_now: dt.datetime
    add_quota_bytes: int
    add_days: int
    package_id_requested: Optional[int]
    q_before: int
    u_before: int
    e_before: Optional[dt.datetime]
    rq_before: Optional[int]
    rd_before: Optional[int]
    rp_before: Optional[int]
    pk_before: Optional[int]
    q_after: int
    u_after: int
    e_after: Optional[dt.datetime]
    rq_after: Optional[int]
    rd_after: Optional[int]
    rp_after: Optional[int]
    pk_after: Optional[int]


def renewal_mode_of(q_before: int, u_before: int, e_before: Optional[dt.datetime], now: dt.datetime) -> str:
    has_limits = q_before > 0 or e_before is not None
    quota_ok = q_before == 0 or u_before < q_before
    expiry_ok = e_before is None or e_before > now
    return "reserved" if has_limits and quota_ok and expiry_ok else "immediate_reset"


def _validate_renewal(ev: PurchaseRenewedEvidence) -> None:
    """The state table of design 5.3, column by column."""
    if ev.add_quota_bytes < 0 or ev.add_days < 0 or (ev.add_quota_bytes == 0 and ev.add_days == 0):
        raise EffectRejected(EVIDENCE_INVALID, "a renewal must add quota or days")
    if renewal_mode_of(ev.q_before, ev.u_before, ev.e_before, ev.effective_now) != ev.renewal_mode:
        raise EffectRejected(EVIDENCE_INVALID, "renewal_mode does not follow from the before values")
    if ev.renewal_mode == "reserved":
        expected = {
            "q_after": ev.q_before, "u_after": ev.u_before, "e_after": ev.e_before,
            "rq_after": (ev.rq_before or 0) + ev.add_quota_bytes, "rd_after": (ev.rd_before or 0) + ev.add_days,
            "rp_after": ev.package_id_requested if ev.package_id_requested is not None else ev.rp_before,
            "pk_after": ev.pk_before}
    else:
        expected = {
            "q_after": ev.add_quota_bytes if ev.add_quota_bytes > 0 else ev.q_before, "u_after": 0,
            "e_after": ev.effective_now + dt.timedelta(days=ev.add_days) if ev.add_days > 0 else ev.e_before,
            "rq_after": ev.rq_before, "rd_after": ev.rd_before, "rp_after": ev.rp_before,
            "pk_after": ev.package_id_requested if ev.package_id_requested is not None else ev.pk_before}
    for column, value in expected.items():
        if getattr(ev, column) != value:
            raise EffectRejected(EVIDENCE_INVALID, f"{column} is {getattr(ev, column)!r}, the {ev.renewal_mode} "
                                                   f"branch gives {value!r}")


# ---------------------------------------------------------------- projections
def _user_binding(user_id) -> dict:
    return {"type": "User", "id": user_id}


def _protocol(value) -> str:
    return str(getattr(value, "value", value))


def _need(evidence, kind) -> None:
    if type(evidence) is not kind:
        raise EffectRejected(EVIDENCE_INVALID, f"expected {kind.__name__ if kind is not type(None) else 'no evidence'}")


def _project(effect_type: str, effect_key: str, resource, evidence) -> tuple[str, int, dict, Optional[int]]:
    """(resource_type, resource_id, actual projection, delta_value)."""
    if effect_type == "user_created":
        _need(evidence, UserCreatedEvidence)
        if int(resource.total_quota_bytes or 0) != evidence.quota_bytes:
            raise EffectRejected(EVIDENCE_INVALID, "quota on the user row differs from the evidence")
        return "User", resource.id, {
            "username": resource.username, "telegram_id": resource.telegram_id,
            "owner_admin_id": resource.owner_admin_id, "package_id": evidence.package_id,
            "quota_bytes": evidence.quota_bytes, "days": evidence.days}, None
    if effect_type == "purchase_created":
        _need(evidence, PurchaseCreatedEvidence)
        starts_on_first_use = resource.expire_days_after_first_use is not None
        if evidence.days and resource.expire_at is None and not starts_on_first_use:
            raise EffectRejected(EVIDENCE_INVALID, "the purchase has no expiry but the evidence has days")
        if not evidence.days and (resource.expire_at is not None or starts_on_first_use):
            raise EffectRejected(EVIDENCE_INVALID, "the purchase has an expiry but the evidence has no days")
        return "Purchase", resource.id, {
            "user": _user_binding(resource.user_id), "package_id": resource.package_id,
            "quota_bytes": int(resource.quota_bytes or 0), "days": evidence.days}, None
    if effect_type == "purchase_renewed":
        _need(evidence, PurchaseRenewedEvidence)
        _validate_renewal(evidence)
        is_purchase = isinstance(resource, models.Purchase)
        return ("Purchase" if is_purchase else "User"), resource.id, {
            "user_id": resource.user_id if is_purchase else resource.id,
            "purchase_id": resource.id if is_purchase else None, "package_id": evidence.package_id_requested,
            "add_quota_bytes": evidence.add_quota_bytes, "add_days": evidence.add_days}, None
    if effect_type == "connection_created":
        _need(evidence, ConnectionCreatedEvidence)
        if evidence.slot_key != effect_key:
            raise EffectRejected(EVIDENCE_INVALID, "slot_key differs from the effect key")
        return "Connection", resource.id, {
            "user": _user_binding(resource.user_id), "node_id": resource.node_id,
            "protocol": _protocol(resource.type), "flow": resource.xr_flow or ""}, None
    if effect_type == "ledger_sale":
        _need(evidence, type(None))
        return "LedgerEntry", resource.id, {
            "user": _user_binding(resource.user_id), "ledger_kind": resource.kind, "amount": int(resource.amount),
            "payment_card_id": resource.payment_card_id}, int(resource.amount)
    if effect_type == "ledger_topup":
        _need(evidence, type(None))
        return "LedgerEntry", resource.id, {
            "user_id": resource.user_id, "amount": int(resource.amount),
            "payment_card_id": resource.payment_card_id}, int(resource.amount)
    if effect_type == "ledger_credit_spend":
        _need(evidence, type(None))
        return "LedgerEntry", resource.id, {"admin_id": resource.admin_id, "amount": int(resource.amount)}, int(resource.amount)
    if effect_type == "card_payment_recorded":
        _need(evidence, type(None))                      # resource: the payment_card_pool_events row (mapping)
        return "PaymentCardPoolEvent", int(resource["id"]), {
            "card_id": int(resource["card_id_snapshot"]), "amount": int(resource["amount"])}, int(resource["amount"])
    if effect_type == "discount_redeemed":
        _need(evidence, DiscountEvidence)
        if evidence.used_count_after != evidence.used_count_before + 1:
            raise EffectRejected(EVIDENCE_INVALID, "used_count must grow by exactly one")
        return "DiscountCodeRedemption", resource.id, {
            "user": _user_binding(resource.user_id), "code_id": resource.code_id,
            "discount_amount": int(resource.discount_amount or 0)}, int(resource.discount_amount or 0)
    if effect_type == "wallet_credit_source_created" and effect_key == "credit:receipt_topup":
        # Before the wallet cutover there is no source row yet: the resource
        # is the credited User and the amount comes from the evidence.
        _need(evidence, WalletCreditEvidence)
        if evidence.balance_after <= evidence.balance_before or evidence.rewards_count != 1:
            raise EffectRejected(EVIDENCE_INVALID, "balance must grow, by one credit")
        amount = evidence.balance_after - evidence.balance_before
        return "User", resource.id, {"user_id": resource.id, "amount": amount}, amount
    raise EffectRejected(NOT_IN_MANIFEST, f"{effect_type}:{effect_key} is not a supported effect yet")


def _snapshot(resource_type: str, resource_id: int, actual: dict, evidence) -> dict:
    snapshot = {"id": resource_id}
    for key in ("username", "node_id", "protocol", "flow", "package_id", "amount", "quota_bytes", "days", "ledger_kind"):
        if key in actual:
            snapshot[key] = actual[key]
    if evidence is not None:
        for key, value in asdict(evidence).items():
            if key in ("used_count_before", "used_count_after"):
                key = key.replace("used_count", "counter")
            snapshot[key] = value.isoformat() if isinstance(value, dt.datetime) else value
    unknown = set(snapshot) - SNAPSHOT_KEYS
    if unknown:
        raise EffectRejected(EVIDENCE_INVALID, f"snapshot keys not allowed: {sorted(unknown)}")
    return snapshot


# ---------------------------------------------------------------- the writer
def _tables():
    from .. import models_receipt_void as rv
    return rv.receipt_approvals, rv.receipt_approval_expected_effects, rv.receipt_approval_effects


def _resolve_bindings(db: Session, approval_uuid: str, expected: dict) -> dict:
    """A {"ref": "effect:<type>:<key>"} binding becomes the resource that
    effect of THIS approval created. Not written yet -> mismatch, which is
    also how the execution order (user before purchase before ...) is kept."""
    _approvals, _expected, effects = _tables()
    resolved = {}
    for key, value in expected.items():
        if isinstance(value, dict) and set(value) == {"ref"}:
            _prefix, effect_type, effect_key = value["ref"].split(":", 2)
            row = db.execute(select(effects.c.resource_type, effects.c.resource_id).where(
                effects.c.approval_uuid == approval_uuid, effects.c.effect_type == effect_type,
                effects.c.effect_key == effect_key)).first()
            if row is None:
                raise EffectRejected(MANIFEST_MISMATCH, f"{value['ref']} has not been recorded yet")
            value = {"type": row[0], "id": int(row[1])}
        resolved[key] = value
    return resolved


def record_effect(db: Session, approval_uuid: str, effect_type: str, effect_key: str, resource, evidence=None) -> int:
    """In the caller's transaction - the one that did the mutation. Raises
    EffectRejected; the caller decides what a rejection means (required:
    roll the mutation back; shadow: see record_effect_shadow). Recording the
    same effect twice returns the first row."""
    _approvals, expected_table, effects = _tables()
    manifest_row = db.execute(select(expected_table.c.expected).where(
        expected_table.c.approval_uuid == approval_uuid, expected_table.c.effect_type == effect_type,
        expected_table.c.effect_key == effect_key)).first()
    if manifest_row is None:
        raise EffectRejected(NOT_IN_MANIFEST, f"{effect_type}:{effect_key}")
    existing = db.execute(select(effects.c.id).where(
        effects.c.approval_uuid == approval_uuid, effects.c.effect_type == effect_type,
        effects.c.effect_key == effect_key)).scalar()
    if existing is not None:
        return int(existing)
    resource_type, resource_id, actual, delta = _project(effect_type, effect_key, resource, evidence)
    expected = _resolve_bindings(db, approval_uuid, json.loads(manifest_row[0]))
    if canonical_json(actual) != canonical_json(expected):
        differing = sorted(k for k in set(actual) | set(expected) if actual.get(k) != expected.get(k))
        raise EffectRejected(MANIFEST_MISMATCH, f"{effect_type}:{effect_key} differs in {differing}")
    snapshot = _snapshot(resource_type, resource_id, actual, evidence)
    result = db.execute(effects.insert().values(
        approval_uuid=approval_uuid, effect_type=effect_type, effect_key=effect_key, resource_type=resource_type,
        resource_id=resource_id, actual_projection=canonical_json(actual), resource_snapshot=canonical_json(snapshot),
        delta_value=delta, created_at=dt.datetime.utcnow()))
    return int(result.inserted_primary_key[0])


def record_effect_shadow(db: Session, approval_uuid: Optional[str], effect_type: str, effect_key: str, resource,
                         evidence=None) -> bool:
    """Shadow mode: inside a savepoint; a failure of any kind rolls only the
    savepoint back, writes one shadow event and leaves the real mutation to
    commit. Never raises. Returns whether the effect was recorded."""
    if not approval_uuid:
        return False
    approvals = _tables()[0]
    try:
        with db.begin_nested():
            record_effect(db, approval_uuid, effect_type, effect_key, resource, evidence)
        return True
    except Exception as exc:
        code = exc.code if isinstance(exc, EffectRejected) else "effect_error"
        if not isinstance(exc, EffectRejected):
            log.exception("receipt approval %s: effect %s:%s could not be recorded", approval_uuid, effect_type, effect_key)
        try:
            pending = db.execute(select(approvals.c.pending_source_instance_id, approvals.c.pending_local_id)
                                 .where(approvals.c.approval_uuid == approval_uuid)).first()
        except Exception:
            pending = None
        runtime.record_shadow_event(
            db, pending_source_instance_id=pending[0] if pending else "?", pending_local_id=pending[1] if pending else 0,
            stage="effect", error_code=code, approval_uuid=approval_uuid if pending else None)
        return False


class ShadowRecorder:
    """What a mutation endpoint uses to record its effects for one approval
    while registration is in shadow. Inactive (every call a no-op) when the
    request carries no approval uuid - the legacy path - so an endpoint can
    call it unconditionally. Never raises."""

    def __init__(self, db: Session, approval_uuid: Optional[str]):
        self.db = db
        self.approval_uuid = approval_uuid or None
        self._used_slots: set[str] = set()

    @property
    def active(self) -> bool:
        return self.approval_uuid is not None

    def effect(self, effect_type: str, effect_key: str, resource, evidence=None) -> bool:
        if not self.active or resource is None:
            return False
        return record_effect_shadow(self.db, self.approval_uuid, effect_type, effect_key, resource, evidence)

    def connection(self, connection) -> bool:
        """Finds this connection's slot: the first unused connection row of
        the manifest with the same (node, protocol, flow). No such slot is
        itself a shadow error (effect_not_in_manifest)."""
        if not self.active or connection is None:
            return False
        key = "conn:unexpected"
        try:
            _approvals, expected, _effects = _tables()
            rows = self.db.execute(select(expected.c.effect_key, expected.c.expected).where(
                expected.c.approval_uuid == self.approval_uuid, expected.c.effect_type == "connection_created")
                .order_by(expected.c.id)).all()
            wanted = (connection.node_id, _protocol(connection.type), connection.xr_flow or "")
            for effect_key, raw in rows:
                spec = json.loads(raw)
                if effect_key not in self._used_slots and (spec["node_id"], spec["protocol"], spec["flow"]) == wanted:
                    key = effect_key
                    self._used_slots.add(effect_key)
                    break
        except Exception:
            log.exception("receipt approval %s: could not look up connection slots", self.approval_uuid)
        return self.effect("connection_created", key, connection, ConnectionCreatedEvidence(key))

    def tag_ledger(self, entry) -> None:
        """Puts the approval uuid on the sale's ledger row - the link the
        void screen will follow. Only for an approval that really exists."""
        if not self.active or entry is None:
            return
        try:
            if self._known():
                entry.approval_uuid = self.approval_uuid
        except Exception:
            log.exception("receipt approval %s: could not tag the ledger row", self.approval_uuid)

    def _known(self) -> bool:
        approvals = _tables()[0]
        return self.db.execute(select(approvals.c.id).where(approvals.c.approval_uuid == self.approval_uuid)).first() is not None

    def card_event_uuid(self) -> Optional[str]:
        """The uuid to put on this payment's pool event: only for an
        approval that exists and has no card event yet (the event table
        allows one per approval). None otherwise - the payment is then
        recorded exactly as it would be without an approval."""
        if not self.active:
            return None
        try:
            from .. import models_receipt_void as rv
            events = rv.payment_card_pool_events
            if self._known() and self.db.execute(
                    select(events.c.id).where(events.c.approval_uuid == self.approval_uuid)).first() is None:
                return self.approval_uuid
        except Exception:
            log.exception("receipt approval %s: could not check the card event", self.approval_uuid)
        return None

    def card_payment(self) -> bool:
        """Records the pool event written for this approval, if the pool
        logs events at all (a 'legacy' pool writes none)."""
        if not self.active:
            return False
        try:
            from .. import models_receipt_void as rv
            events = rv.payment_card_pool_events
            row = self.db.execute(select(events).where(events.c.approval_uuid == self.approval_uuid)).mappings().first()
        except Exception:
            log.exception("receipt approval %s: could not read the card event", self.approval_uuid)
            return False
        return self.effect("card_payment_recorded", "card_payment", dict(row)) if row else False
