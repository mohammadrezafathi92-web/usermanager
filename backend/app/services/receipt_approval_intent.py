"""What a receipt approval is ABOUT, and what it is expected to do (Receipt
Void design 5.1 and 5.3; rollout phase P3).

Three pure steps, none of which writes anything:

  resolve_target   - who the approval is for. For an existing customer the
                     identity is backend-derived (anything the caller sent
                     is only compared); for a brand-new one the Telegram id
                     is attested by a panel-managed bot.
  intent_hash      - SHA-256 of the fixed part of the request. It never
                     changes for one pending receipt.
  build_manifest   - the effects this approval must (or may) produce, each
                     with a fixed key known before anything runs, plus the
                     hash that locks them.

Referral rewards (design 5.3, 14.1) are in the manifest: both credits, and
each quota reward only when resolve_quota_reward_target finds exactly one
finite target - never a guessed one.

Loyalty rows are deliberately NOT built here: the frozen design allows them
only when loyalty_timing_mode is 'payment_event', i.e. after the joint
activation of phase P9a ("until then loyalty works exactly as today and no
loyalty row is in the manifest", section 14.2; RV-344). They need the
policy epochs, the loyalty event ledger and sale_payment_evidence, none of
which exist before that phase.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy.orm import Session

from .. import models
from . import bot_auth, bot_resources, hierarchy, payment_card_events, user_ops
from .receipt_approval_runtime import sha256_hex

KINDS = ("new", "renew", "topup")
REQUIRED, OPTIONAL = "required", "optional"
USER_REF = {"ref": "effect:user_created:user"}
PURCHASE_REF = {"ref": "effect:purchase_created:purchase"}


class IntentRejected(Exception):
    """The intent cannot become an approval. status/code are the HTTP status
    and stable error code of design section 18."""

    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status = status
        self.code = code


@dataclass(frozen=True)
class ConnectionSpec:
    node_id: int
    protocol: str
    flow: str = ""


@dataclass(frozen=True)
class ApprovalIntent:
    pending_source_instance_id: str
    pending_local_id: int
    kind: str
    target_username: str
    amount: int                                  # what the customer actually paid
    package_id: Optional[int] = None
    renew_purchase_id: Optional[int] = None
    payment_card_id: Optional[int] = None
    receipt_file_id: Optional[str] = None
    discount_code: Optional[str] = None
    referral_code: Optional[str] = None
    list_price: Optional[int] = None             # price before discount
    quota_bytes: int = 0                         # only used when there is no package to read it from
    days: int = 0
    connections: tuple[ConnectionSpec, ...] = ()  # explicit request slots, in request order
    claimed_telegram_id: Optional[int] = None    # compared, never trusted
    claimed_owner_admin_id: Optional[int] = None


@dataclass(frozen=True)
class Target:
    shape: str                                   # new_user | existing_user | renew_purchase | renew_user | topup
    owner_admin_id: Optional[int]
    tenant_scope_key: str
    telegram_id: Optional[int]
    telegram_id_source: str                      # user_row | bot_attested | none
    user_id: Optional[int] = None
    purchase_count: int = 0                      # for renew_user: the legacy shape registration saw


@dataclass(frozen=True)
class ManifestRow:
    effect_type: str
    effect_key: str
    requirement: str
    expected: dict = field(default_factory=dict)


def tenant_scope_key(db: Session, owner_admin_id: Optional[int]) -> str:
    """'shared' for a customer with no owner, otherwise 'admin:{id}' of the
    level-2 Admin at the top of the owner's tree (design 9, line on
    tenant_scope_key). A plain string: deleting the admin later changes
    nothing."""
    if owner_admin_id is None:
        return "shared"
    admin = db.get(models.AdminUser, owner_admin_id)
    root = hierarchy.parent_admin_scope_id(admin) if admin else None
    return f"admin:{root or owner_admin_id}"


def resolve_quota_reward_target(purchases_after: list, user_quota_after: int):
    """Design 14.1, verbatim. Input is the topology AFTER the approval, not
    the state at registration. Returns ("User", None), ("Purchase", purchase)
    or None. None means: no quota row in the manifest and no quota reward -
    it is never written to some other resource instead.

        no purchase:        the User if its quota is finite (> 0), else None (unlimited)
        exactly one:        that Purchase if its quota is finite, else None (unlimited)
        more than one:      None (ambiguous)
    """
    if not purchases_after:
        return ("User", None) if int(user_quota_after or 0) > 0 else None
    if len(purchases_after) == 1:
        only = purchases_after[0]
        return ("Purchase", only) if int(only.quota_bytes or 0) > 0 else None
    return None


def _referral_rows(db: Session, intent: ApprovalIntent, target: Target, package: models.Package,
                   has_connections: bool, quota_bytes: int) -> list[ManifestRow]:
    """The referral rewards this approval is expected to produce, frozen at
    registration (design 5.3): only for a brand-new customer, only for a
    code that resolves to a referrer, each amount from PanelSettings as it
    is now. A code that resolves to nobody adds nothing - the approval goes
    on without a referral, as the sale itself always has."""
    code = (intent.referral_code or "").strip().upper()
    if target.shape != "new_user" or not code:
        return []
    referrer = db.query(models.User).filter(models.User.referral_code == code).first()
    settings_row = db.get(models.PanelSettings, 1)
    if referrer is None or settings_row is None:
        return []
    rows: list[ManifestRow] = []
    referrer_credit = int(settings_row.referral_referrer_reward_credit or 0)
    new_user_credit = int(settings_row.referral_new_user_reward_credit or 0)
    referrer_bytes = user_ops.gb_to_bytes(settings_row.referral_referrer_reward_gb or 0)
    new_user_bytes = user_ops.gb_to_bytes(settings_row.referral_new_user_reward_gb or 0)
    if referrer_credit > 0:
        rows.append(ManifestRow("wallet_credit_source_created", "credit:referral_reward:referrer", REQUIRED, {
            "referrer_user_id": referrer.id, "referrer_username": referrer.username, "amount": referrer_credit}))
    if new_user_credit > 0:
        rows.append(ManifestRow("wallet_credit_source_created", "credit:referral_reward:new_user", REQUIRED, {
            "user": USER_REF, "amount": new_user_credit}))
    if referrer_bytes > 0:
        # the referrer's topology is not changed by this approval
        purchases = db.query(models.Purchase).filter(models.Purchase.user_id == referrer.id).all()
        found = resolve_quota_reward_target(purchases, referrer.total_quota_bytes)
        if found is not None:
            kind, purchase = found
            binding = {"type": "Purchase", "id": purchase.id} if kind == "Purchase" else {"type": "User", "id": referrer.id}
            rows.append(ManifestRow("quota_reward_granted", "quota:referral:referrer", REQUIRED,
                                    {"target": binding, "bytes": referrer_bytes}))
    if new_user_bytes > 0 and quota_bytes > 0:
        # after this approval the new customer has exactly one Purchase (the
        # one it creates) when the sale has connections, and none otherwise
        rows.append(ManifestRow("quota_reward_granted", "quota:referral:new_user", REQUIRED, {
            "target": PURCHASE_REF if has_connections else USER_REF, "bytes": new_user_bytes}))
    return rows


def is_managed_principal(principal: bot_auth.BotPrincipal) -> bool:
    """The in-process bot, or a key the panel itself minted for a remote
    installation - the only callers whose Telegram id claim is accepted."""
    return bool(principal.valid and (principal.is_internal or principal.key_type == bot_auth.KeyType.REMOTE_SHARED_BOT))


def _validate(intent: ApprovalIntent) -> None:
    if intent.kind not in KINDS:
        raise IntentRejected(422, "invalid_kind")
    if not intent.pending_source_instance_id or len(str(intent.pending_source_instance_id)) > 64:
        raise IntentRejected(422, "invalid_pending_source")
    if not isinstance(intent.pending_local_id, int) or intent.pending_local_id <= 0:
        raise IntentRejected(422, "invalid_pending_id")
    if not intent.target_username or len(intent.target_username) > 128:
        raise IntentRejected(422, "invalid_target_username")
    if not isinstance(intent.amount, int) or intent.amount < 0:
        raise IntentRejected(422, "invalid_amount")
    if intent.kind == "topup" and intent.amount <= 0:
        raise IntentRejected(422, "invalid_amount")
    if intent.kind != "topup" and intent.package_id is None:
        raise IntentRejected(422, "package_required")
    if intent.kind != "renew" and intent.renew_purchase_id is not None:
        raise IntentRejected(422, "renew_purchase_not_applicable")


def resolve_target(db: Session, principal: bot_auth.BotPrincipal, intent: ApprovalIntent) -> Target:
    _validate(intent)
    claimed_owner = bot_auth.resolve_claimed_owner(db, principal, intent.claimed_owner_admin_id,
                                                   endpoint="receipt_approval")
    user = db.query(models.User).filter(models.User.username == intent.target_username).first()
    if user is None:
        if intent.kind != "new":
            raise IntentRejected(404, "target_user_not_found")
        if not is_managed_principal(principal):
            raise IntentRejected(403, "new_user_requires_managed_bot")
        if not intent.claimed_telegram_id:
            raise IntentRejected(422, "telegram_id_required")
        return Target(shape="new_user", owner_admin_id=claimed_owner,
                      tenant_scope_key=tenant_scope_key(db, claimed_owner),
                      telegram_id=int(intent.claimed_telegram_id), telegram_id_source="bot_attested")

    bot_resources._get_user_or_403(db, principal, intent.target_username, claimed_owner)   # scope check
    if intent.claimed_telegram_id is not None and user.telegram_id is not None \
            and int(intent.claimed_telegram_id) != int(user.telegram_id):
        raise IntentRejected(422, "identity_mismatch")
    if claimed_owner is not None and user.owner_admin_id != claimed_owner:
        raise IntentRejected(422, "identity_mismatch")
    purchases = db.query(models.Purchase).filter(models.Purchase.user_id == user.id)
    if intent.kind == "topup":
        shape = "topup"
    elif intent.kind == "new":
        shape = "existing_user"
    elif intent.renew_purchase_id is not None:
        if purchases.filter(models.Purchase.id == intent.renew_purchase_id).first() is None:
            raise IntentRejected(404, "target_purchase_not_found")
        shape = "renew_purchase"
    else:
        shape = "renew_user"
    return Target(shape=shape, owner_admin_id=user.owner_admin_id,
                  tenant_scope_key=tenant_scope_key(db, user.owner_admin_id),
                  telegram_id=int(user.telegram_id) if user.telegram_id is not None else None,
                  telegram_id_source="user_row" if user.telegram_id is not None else "none",
                  user_id=user.id, purchase_count=purchases.count())


def intent_hash(intent: ApprovalIntent, target: Target) -> str:
    """Design 5.1: mode and approver are in neither hash; identities are the
    derived ones, not what the caller claimed."""
    return sha256_hex({
        "kind": intent.kind, "target_shape": target.shape, "target_username": intent.target_username,
        "target_user_id": target.user_id, "renew_purchase_id": intent.renew_purchase_id, "amount": intent.amount,
        "payment_card_id": intent.payment_card_id, "receipt_file_id": intent.receipt_file_id,
        "package_id": intent.package_id, "discount_code": (intent.discount_code or "").strip().upper() or None,
        "referral_code": (intent.referral_code or "").strip() or None,
        "connections": [[c.node_id, c.protocol, c.flow or ""] for c in intent.connections],
        "telegram_id": target.telegram_id, "owner_admin_id": target.owner_admin_id,
    })


def _protocol(value) -> str:
    return str(getattr(value, "value", value))


def _connection_rows(db: Session, intent: ApprovalIntent, package: models.Package, user_binding: dict) -> list[ManifestRow]:
    rows = []
    if intent.connections:
        for index, spec in enumerate(intent.connections):
            rows.append(ManifestRow("connection_created", f"conn:request_slot:{index}", REQUIRED, {
                "user": user_binding, "node_id": int(spec.node_id), "protocol": _protocol(spec.protocol), "flow": spec.flow or ""}))
        return rows
    bundled = (db.query(models.PackageConnection).filter(models.PackageConnection.package_id == package.id)
               .order_by(models.PackageConnection.id).all())
    for item in bundled:
        rows.append(ManifestRow("connection_created", f"conn:package_connection:{item.id}", REQUIRED, {
            "user": user_binding, "node_id": int(item.node_id), "protocol": _protocol(item.protocol), "flow": item.flow or ""}))
    return rows


def build_manifest(db: Session, intent: ApprovalIntent, target: Target) -> list[ManifestRow]:
    """The expected effects, sorted by (effect_type, effect_key)."""
    rows: list[ManifestRow] = []
    package = None
    if intent.kind != "topup":
        package = db.get(models.Package, intent.package_id)
        if package is None:
            raise IntentRejected(422, "package_not_found")
    quota_bytes = user_ops.gb_to_bytes(package.quota_gb) if package else int(intent.quota_bytes)
    days = int(package.duration_days or 0) if package else int(intent.days)
    user_binding = USER_REF if target.shape == "new_user" else {"type": "User", "id": target.user_id}

    if target.shape == "topup":
        rows.append(ManifestRow("wallet_credit_source_created", "credit:receipt_topup", REQUIRED,
                                {"user_id": target.user_id, "amount": intent.amount}))
        rows.append(ManifestRow("ledger_topup", "topup", REQUIRED, {
            "user_id": target.user_id, "amount": intent.amount, "payment_card_id": intent.payment_card_id}))
    elif target.shape in ("renew_purchase", "renew_user"):
        if quota_bytes <= 0 and days <= 0:
            raise IntentRejected(422, "empty_renewal")
        rows.append(ManifestRow("purchase_renewed", "renew", REQUIRED, {
            "user_id": target.user_id, "purchase_id": intent.renew_purchase_id, "package_id": package.id,
            "add_quota_bytes": quota_bytes, "add_days": days}))
        rows.append(ManifestRow("ledger_sale", "sale", REQUIRED, {
            "user": user_binding, "ledger_kind": "sale_renew", "amount": intent.amount,
            "payment_card_id": intent.payment_card_id}))
    else:
        connections = _connection_rows(db, intent, package, user_binding)
        purchase_expected = {"user": user_binding, "package_id": package.id, "quota_bytes": quota_bytes,
                             "days": days or None}
        if target.shape == "new_user":
            rows.append(ManifestRow("user_created", "user", REQUIRED, {
                "username": intent.target_username, "telegram_id": target.telegram_id,
                "owner_admin_id": target.owner_admin_id, "package_id": package.id, "quota_bytes": quota_bytes,
                "days": days or None}))
            if connections:                      # a new user with no connection gets no Purchase
                rows.append(ManifestRow("purchase_created", "purchase", REQUIRED, purchase_expected))
        else:
            rows.append(ManifestRow("purchase_created", "purchase", REQUIRED, purchase_expected))
        rows.extend(connections)
        rows.append(ManifestRow("ledger_sale", "sale", REQUIRED, {
            "user": user_binding, "ledger_kind": "sale_new", "amount": intent.amount,
            "payment_card_id": intent.payment_card_id}))
        rows.extend(_referral_rows(db, intent, target, package, bool(connections), quota_bytes))

    if package is not None and target.owner_admin_id is not None:
        owner = db.get(models.AdminUser, target.owner_admin_id)
        if owner is not None and not owner.is_superadmin and owner.billing_mode != "usage":
            from . import admin_billing
            cost = int(admin_billing.unit_price(owner, package))
            if cost > 0:
                rows.append(ManifestRow("ledger_credit_spend", "credit_spend", REQUIRED,
                                        {"admin_id": owner.id, "amount": cost}))

    if intent.payment_card_id is not None and intent.amount > 0 and payment_card_events.enabled():
        card = db.get(models.PaymentCard, intent.payment_card_id)
        if card is not None and payment_card_events.lock_pool(db, card.owner_admin_id, create_as=None) \
                == payment_card_events.EVENT_LOGGED:
            rows.append(ManifestRow("card_payment_recorded", "card_payment", REQUIRED,
                                    {"card_id": card.id, "amount": intent.amount}))

    if intent.discount_code and intent.kind != "topup":
        code = (db.query(models.DiscountCode)
                .filter(models.DiscountCode.code == intent.discount_code.strip().upper()).first())
        if code is not None:
            _ok, _reason, discount = user_ops.validate_discount_code(
                db, intent.discount_code, int(intent.list_price or 0),
                username=intent.target_username if target.user_id else None, owner_admin_id=target.owner_admin_id)
            rows.append(ManifestRow("discount_redeemed", f"discount:{code.id}", OPTIONAL, {
                "user": user_binding, "code_id": code.id, "discount_amount": int(discount)}))

    for row in rows:
        validate_expected(row.effect_type, row.effect_key, row.expected)
    return sorted(rows, key=lambda r: (r.effect_type, r.effect_key))


def manifest_hash(rows: list[ManifestRow]) -> str:
    ordered = sorted(rows, key=lambda r: (r.effect_type, r.effect_key))
    return sha256_hex([{"effect_type": r.effect_type, "effect_key": r.effect_key, "requirement": r.requirement,
                        "expected": r.expected} for r in ordered])


# ---------------------------------------------------------------- expected schemas (design 5.3)
BINDING = "binding"
NULLABLE_INT = "int|null"
_SCHEMAS: list[tuple[str, str, dict]] = [
    ("user_created", r"user", {"username": str, "telegram_id": int, "owner_admin_id": NULLABLE_INT, "package_id": int,
                               "quota_bytes": int, "days": NULLABLE_INT}),
    ("purchase_created", r"purchase", {"user": BINDING, "package_id": int, "quota_bytes": int, "days": NULLABLE_INT}),
    ("purchase_renewed", r"renew", {"user_id": int, "purchase_id": NULLABLE_INT, "package_id": int,
                                    "add_quota_bytes": int, "add_days": int}),
    ("connection_created", r"conn:(package_connection|request_slot):\d+",
     {"user": BINDING, "node_id": int, "protocol": str, "flow": str}),
    ("ledger_sale", r"sale", {"user": BINDING, "ledger_kind": str, "amount": int, "payment_card_id": NULLABLE_INT}),
    ("ledger_topup", r"topup", {"user_id": int, "amount": int, "payment_card_id": NULLABLE_INT}),
    ("ledger_credit_spend", r"credit_spend", {"admin_id": int, "amount": int}),
    ("card_payment_recorded", r"card_payment", {"card_id": int, "amount": int}),
    ("discount_redeemed", r"discount:\d+", {"user": BINDING, "code_id": int, "discount_amount": int}),
    ("wallet_credit_source_created", r"credit:receipt_topup", {"user_id": int, "amount": int}),
    ("wallet_credit_source_created", r"credit:referral_reward:referrer",
     {"referrer_user_id": int, "referrer_username": str, "amount": int}),
    ("wallet_credit_source_created", r"credit:referral_reward:new_user", {"user": BINDING, "amount": int}),
    ("quota_reward_granted", r"quota:referral:(referrer|new_user)", {"target": BINDING, "bytes": int}),
    ("wallet_credit_source_created", r"credit:loyalty:\d+:\d+",
     {"user": BINDING, "epoch_id": int, "slot": int, "amount": int}),
    ("quota_reward_granted", r"quota:loyalty:\d+:\d+", {"target": BINDING, "epoch_id": int, "slot": int, "bytes": int}),
    ("loyalty_purchase_recorded", r"loyalty_purchase", {"user": BINDING, "eligibility_reason": str}),
]


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def is_binding(value) -> bool:
    if not isinstance(value, dict):
        return False
    if set(value) == {"type", "id"}:
        return value["type"] in ("User", "Purchase") and _is_int(value["id"]) and value["id"] > 0
    if set(value) == {"ref"}:
        return value["ref"] in (USER_REF["ref"], PURCHASE_REF["ref"])
    return False


def schema_for(effect_type: str, effect_key: str) -> Optional[dict]:
    for kind, pattern, schema in _SCHEMAS:
        if kind == effect_type and re.fullmatch(pattern, effect_key):
            return schema
    return None


def validate_expected(effect_type: str, effect_key: str, expected: dict) -> None:
    """Exactly the keys of the row's schema, each of the right type - an
    extra or missing key is an error, not something to drop silently."""
    schema = schema_for(effect_type, effect_key)
    if schema is None:
        raise ValueError(f"no expected-schema for {effect_type}:{effect_key}")
    if set(expected) != set(schema):
        raise ValueError(f"{effect_type}:{effect_key}: keys {sorted(expected)} != {sorted(schema)}")
    for key, kind in schema.items():
        value = expected[key]
        if kind == BINDING:
            ok = is_binding(value)
        elif kind == NULLABLE_INT:
            ok = value is None or _is_int(value)
        elif kind is int:
            ok = _is_int(value)
        else:
            ok = isinstance(value, kind)
        if not ok:
            raise ValueError(f"{effect_type}:{effect_key}: {key}={value!r} is not {kind}")
