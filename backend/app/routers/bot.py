"""External API for a customer-facing bot (e.g. a Telegram sales bot) to
create/renew/check/delete users without needing an admin login. Auth is a
static per-integration key sent in the `X-API-Key` header - manage keys
from the panel's Settings page.

This is also what a REMOTELY-deployed bot instance talks to (see
telegram_bot/remote_bridge.py + services/remote_deploy.py) when the admin
chooses to run the interactive Telegram bot on a second server instead of
in-process here - same endpoints, same X-API-Key auth, just reached over
the network instead of in-process."""
import datetime as dt
import logging
import os
import time
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..deps import get_bot_principal
from ..services import user_ops, hierarchy, payment_cards, accounting, admin_billing, trial
from ..services import bot_resources
from ..services import payment_card_events
from ..services import receipt_approval_effects as approval_effects
from ..services import receipt_approval_registration as approval_registration
from ..services.bot_auth import (
    BROADCAST,
    CUSTOMER_READ,
    CUSTOMER_WRITE,
    FILES_READ,
    IDENTITY_WRITE,
    PAYMENT_READ,
    PAYMENT_WRITE,
    ADMIN_LOOKUP,
    WALLET_WRITE,
    BotPrincipal,
    NodeAuthorizationScope,
    bot_route_policy,
    resolve_bot_authorization_scope,
    resolve_claimed_owner,
)
from ..services.quota_manager import _set_connection_enabled
from .panel_settings import _get_or_create as _get_or_create_settings

logger = logging.getLogger("bot_api")

# Phase C (docs/api-key-scope-audit-2026-09-27.md): get_bot_api_key remains
# the router-level dependency purely as the 401 gate (missing/invalid/
# disabled key) - its RETURN VALUE is no longer injected into any endpoint
# here. Every endpoint below instead takes `principal: BotPrincipal =
# Depends(get_bot_principal)` (which itself depends on get_bot_api_key) and
# is wrapped in @bot_route_policy, which enforces the declared capability
# before the endpoint body ever runs - see bot_auth.bot_route_policy's own
# docstring for why this, not a bare dict of policies, is what actually
# makes the capability check impossible to forget on a new endpoint.
router = APIRouter(prefix="/api/bot", tags=["bot"], dependencies=[Depends(get_bot_principal)])


def _connection_info(conn: models.Connection) -> schemas.BotConnectionInfo:
    # get_connection_share reaches out to the NODE for a WireGuard
    # connection (it needs the interface's public key), and raised a 400
    # when it could not. That 400 came out of _user_response, which nearly
    # every bot endpoint returns - so one unreachable MikroTik meant a
    # customer could not even open «اکانت من» or press /start. Their other
    # services, on perfectly healthy nodes, became unreachable too.
    #
    # A node being down must degrade to "this one connection has no config
    # right now", not to "you have no account". The customer still sees the
    # connection, its name, its status and its usage; only the config text
    # is missing, and it comes back on its own when the node does.
    try:
        share = user_ops.get_connection_share(conn)
    except Exception as exc:  # noqa: BLE001 - HTTPException or any node error
        logger.warning(
            "connection %s (node %s): config unavailable, showing it without one: %s",
            conn.id, conn.node_id, exc,
        )
        share = {}
    return schemas.BotConnectionInfo(
        id=conn.id,
        type=conn.type,
        node_id=conn.node_id,
        node_name=conn.node.name,
        enabled=conn.enabled,
        link=share.get("link"),
        config_text=share.get("config_text"),
        server=share.get("server"),
        port=share.get("port"),
        username=share.get("username"),
        password=share.get("password"),
        psk=share.get("psk"),
        ovpn_files=share.get("ovpn_files") or [],
        total_bytes=conn.total_bytes or 0,
        created_at=conn.created_at,
        purchase_batch=conn.purchase_batch,
        package_name=conn.package_name_snapshot,
        # conn.purchase is None for a connection never turned into its own
        # Purchase (still on the user's combined legacy pool) - comment
        # stays None there too, same as it always has.
        comment=conn.purchase.comment if conn.purchase else None,
        purchase_id=conn.purchase_id,
    )


def _user_response(user: models.User) -> schemas.BotUserResponse:
    loyalty = getattr(user, "_loyalty_reward_just_granted", None)
    return schemas.BotUserResponse(
        id=user.id,
        username=user.username,
        full_name=user.full_name,
        status=user.status,
        # models.User.effective_* rather than the columns - see that block's
        # comment. This is the customer-facing number, and it was the one
        # most visibly wrong: the bot told a customer "21.9 GB / 50 GB" from
        # the frozen pool while they actually had three services totalling
        # 173GB across 130GB of quota.
        total_quota_bytes=user.effective_quota_bytes,
        used_bytes=user.effective_used_bytes,
        remaining_bytes=(
            max(user.effective_quota_bytes - user.effective_used_bytes, 0)
            if user.effective_quota_bytes else None
        ),
        expire_at=user.effective_expire_at,
        service_count=user.service_count,
        telegram_id=user.telegram_id,
        balance=user.balance or 0,
        connections=[_connection_info(c) for c in user.connections],
        referral_code=user.referral_code,
        loyalty_reward_credit=loyalty[0] if loyalty else None,
        loyalty_reward_gb=loyalty[1] if loyalty else None,
        reserved_quota_gb=(user.reserved_quota_bytes / (1024 ** 3)) if user.reserved_quota_bytes else None,
        reserved_duration_days=user.reserved_duration_days,
        purchases_blocked=bool(user.purchases_blocked),
        purchases_blocked_reason=user.purchases_blocked_reason or None,
    )


def _charge_seller(
    db: Session, user: models.User, package: Optional[models.Package], add_gb: float = 0,
) -> None:
    """Charges the reseller who owns this customer. Always on - see
    2026-09-05 decision below.

    Every sale below used to be free: this router never touched
    services/admin_billing, so a purchase or renewal through a reseller's
    own Telegram bot - the way most selling actually happens - cost them
    nothing, while the identical action from the panel was charged. The
    credit system therefore metered the quiet path and ignored the busy one.

    Used to be gated by PanelSettings.charge_admins_for_bot_sales (default
    off, a superadmin-only switch) so operators could opt in gradually.
    Removed by explicit request (2026-09-05): billing bot sales is no
    longer optional, so there is nothing left to toggle - a reseller with
    no credit simply cannot complete a sale, same as any other charge in
    this system.

    A customer with no owner (the shared bot's own signups) belongs to the
    superadmin, who is never charged - so there is nothing to do.
    """
    if user.owner_admin_id is None:
        return
    # A sample the reseller is giving away must not also be a sample they
    # are BUYING. Charging for it would mean a reseller with an empty
    # balance cannot offer one at all, which defeats the point - and the
    # cost of a trial is the traffic, which is metered normally either way.
    if trial.is_trial(package):
        return
    admin = db.get(models.AdminUser, user.owner_admin_id)
    if admin is None:
        return
    # charge_for_renewal covers both shapes and already exempts superadmins
    # and volume-billed accounts.
    admin_billing.charge_for_renewal(db, admin, package, add_gb)


# Phase C (docs/api-key-scope-audit-2026-09-27.md): the old _visibility_filter
# helper that used to live here (same clause, same reasoning as
# hierarchy.user_visibility_clause) moved into services/bot_resources.py's
# _list_users_query as a nested closure - every caller here now goes through
# that accessor instead of building the clause locally, so this file itself
# never touches models.User/models.AdminUser outside the accessor functions
# (see that module's own docstring, and tests/test_bot_resource_accessor_ast.py).

DEFAULT_PURCHASE_BLOCK_MESSAGE = (
    "امکان خرید و تمدید برای این حساب فعلا غیرفعال است. "
    "سرویس فعلی شما تا پایان اعتبارش کار می‌کند. برای اطلاعات بیشتر با پشتیبانی تماس بگیرید."
)


def _ensure_can_buy(user: models.User) -> None:
    """The till is closed for this customer ("قفل خرید" - see
    models.User.purchases_blocked).

    Called at the four places money or service can newly enter the account
    through the bot: a new service, a renewal, a wallet top-up, and signing
    up a second account on the same Telegram id. Everything the customer
    already has is untouched on purpose - this must never be able to cut
    off a service that is already paid for, so it is not called from any
    read, config-fetch, or auth path.

    Raises 403 carrying the admin's own words, so the customer is told why
    instead of meeting a button that silently fails.
    """
    if not getattr(user, "purchases_blocked", False):
        return
    raise HTTPException(403, (user.purchases_blocked_reason or "").strip() or DEFAULT_PURCHASE_BLOCK_MESSAGE)


def _ensure_telegram_can_buy(db: Session, telegram_id: Optional[int]) -> None:
    """Same lock, applied to a Telegram account rather than one User row.

    A customer whose account is locked could otherwise just sign up again
    from the same Telegram account and carry on buying - the lock would
    look enforced while doing nothing. One User row locked locks that
    person's ability to open new ones.
    """
    if not telegram_id:
        return
    blocked = (
        db.query(models.User)
        .filter(models.User.telegram_id == telegram_id, models.User.purchases_blocked.is_(True))
        .first()
    )
    if blocked is not None:
        _ensure_can_buy(blocked)


def _ensure_one_time_package_not_reused(
    db: Session, package: Optional[models.Package], *,
    user: Optional[models.User] = None, telegram_id: Optional[int] = None,
) -> None:
    """package.one_time_per_user ("فقط یک‌بار قابل خرید") - for a trial/
    heavily-discounted package an admin doesn't want the same customer
    buying twice through the bot's own self-service purchase flow.

    Checked across EVERY User row tied to the same Telegram id, not just
    `user` - otherwise the limit is trivially dodged by signing up a
    second account (same reasoning as _ensure_telegram_can_buy above).
    `user`/`telegram_id` are both optional and additive: purchase_package
    below has an existing `user`; create_user below only has a
    `telegram_id` (the account being created can't have bought anything
    itself yet, but a SIBLING account on the same Telegram id might have).

    Deliberately never called from routers/users.py (the admin panel's own
    apply_package/create_user) - an admin or seller can always manually
    grant this package again; only a customer buying it themselves is
    limited."""
    if not package or not package.one_time_per_user:
        return
    user_ids: set[int] = set()
    if user is not None:
        user_ids.add(user.id)
    if telegram_id:
        user_ids.update(
            uid for (uid,) in db.query(models.User.id).filter(models.User.telegram_id == telegram_id).all()
        )
    if not user_ids:
        return
    already = (
        db.query(models.Purchase.id)
        .filter(models.Purchase.package_id == package.id, models.Purchase.user_id.in_(user_ids))
        .first()
    )
    if already is not None:
        raise HTTPException(403, "این بسته فقط یک‌بار برای هر مشتری قابل خرید است و قبلاً توسط شما خریداری شده.")


# Phase C: the old _get_user_or_404 helper moved into services/
# bot_resources.py's _get_user_or_403 - every caller here now passes its
# own owner_admin_id query param through as claimed_owner_admin_id
# (still the SAME unconditional hierarchy.can_see_user check the old
# helper applied whenever a caller passed one - see that accessor's own
# docstring for why dropping this unconditional part during the first
# C0 wiring pass was a real regression, not a no-op), plus
# require_bot_user_access (the NEW principal-identity-bound check) on top.


def _record_bot_sale(
    db: Session, principal: BotPrincipal, kind: str, payload, user: models.User,
    package: Optional[models.Package], purchase_id: Optional[int] = None,
) -> Optional[models.LedgerEntry]:
    """Shared accounting hook for every bot sale endpoint below (see
    services/accounting.py). Uses the exact paid amount when the bot sent
    it (new bot builds pass the post-discount final price), otherwise falls
    back to the package's list/seller price so a stale remote bot still
    produces sensible books. Adds to the session only - the caller's own
    commit right after makes it atomic with the sale itself.

    payload.payment_card_id is validated through _get_payment_card_or_403
    before it ever reaches accounting.record - found during Phase C review
    (docs/api-key-scope-audit-2026-09-27.md): this was the one remaining
    write path that reached PaymentCard by id without going through the
    accessor built specifically for it, which would have left a scoped
    tenant's sale bookkeeping permanently attributable to a foreign card
    (wrong reseller's rotation/aggregate totals) even after enforcement is
    turned on for that tenant, since the bypass has nothing to do with
    scope_enforced."""
    paid = getattr(payload, "paid_amount", None)
    if paid is None:
        if package is None:
            return None  # package-less admin-created user - nothing was sold
        paid = accounting.sale_fallback_price(db, package, user.owner_admin_id)
    payment_card_id = getattr(payload, "payment_card_id", None)
    if payment_card_id is not None:
        bot_resources._get_payment_card_or_403(db, principal, payment_card_id)
    return accounting.record(
        db, kind, paid,
        user=user,
        admin_id=user.owner_admin_id,
        package=package,
        purchase_id=purchase_id,
        payment_card_id=payment_card_id,
        payment_method=getattr(payload, "payment_method", None),
        discount_code=getattr(payload, "discount_code", None),
        discount_amount=getattr(payload, "discount_amount", None),
    )


def _record_renewal(db: Session, payload, resource, evidence: list, sale_entry) -> None:
    """Receipt-approval effects of a renewal, in shadow: the renewal itself
    (with the evidence user_ops' renew function produced) and the sale's
    ledger row, tagged with the approval. No-op without an approval uuid.
    The renewal already committed inside user_ops; this runs right after it
    and is committed by the caller."""
    recorder = approval_effects.ShadowRecorder(db, getattr(payload, "approval_uuid", None))
    if not recorder.active:
        return
    db.flush()
    if evidence:
        recorder.effect("purchase_renewed", "renew", resource, evidence[0])
    if sale_entry is not None:
        recorder.effect("ledger_sale", "sale", sale_entry)
        recorder.tag_ledger(sale_entry)


@router.get("/nodes", response_model=list[schemas.BotNodeInfo])
@bot_route_policy(capability=None, resource_strategy="node_list")
def list_nodes(db: Session = Depends(get_db), principal: BotPrincipal = Depends(get_bot_principal)):
    allowed = bot_resources._scoped_node_ids(db, principal)
    query = db.query(models.Node).filter(models.Node.enabled == True)  # noqa: E712
    if allowed is not None:
        query = query.filter(models.Node.id.in_(allowed))
    return query.all()


@router.get("/packages", response_model=list[schemas.PackageOut])
@bot_route_policy(capability=CUSTOMER_READ, resource_strategy="package_list")
def list_packages(
    owner_admin_id: Optional[int] = None, db: Session = Depends(get_db),
    principal: BotPrincipal = Depends(get_bot_principal),
):
    """Active packages, in the order the admin arranged them - shown to
    customers by the sales bot at checkout. Eager-loads `connections` AND
    `files` - the built-in bot (app/telegram_bot/panel_bridge.py) closes
    its DB session before converting the result to a schema, so a
    lazy-loaded relationship accessed at that point would raise a
    DetachedInstanceError and silently hang the "خرید اکانت جدید" button
    (this bit us once already with `connections` - `files` needs the same
    treatment since it's read by PackageOut too).

    owner_admin_id identifies WHICH bot is asking (see
    telegram_bot/panel_bridge.py's _scope() - None for the shared/global
    bot, an AdminUser id for a per-Admin/per-Seller own bot). The result is
    ALWAYS scoped to that account's own tree ONLY now (changed 2026-07-19,
    same fix/reasoning as routers/packages.py's list_packages and
    hierarchy.accessible_package_owner_ids's docstring): their own packages
    for an Admin, their parent Admin's for a Seller, and - as of this fix -
    the superadmin's own "global" (owner_admin_id IS NULL) packages for the
    shared bot too (owner_admin_id=None case below), never anyone else's
    tree either direction. Before this fix the shared bot showed every
    single bot_enabled package panel-wide regardless of owner (a gap, not a
    deliberate choice - same bug class as the panel's own Packages page
    used to have for a superadmin).

    Additionally, if the resolved account is a level-3 Seller, each
    package's `price` is replaced with their own resale override (models.
    PackageSellerPrice) where one is set, so their bot shows/charges their
    own number instead of their parent Admin's base price."""
    q = bot_resources._list_packages_query(db, principal, owner_admin_id)
    # Phase C: the accessor above already resolved+validated the claim via
    # resolve_claimed_owner and applied the same owner filtering this
    # function used to build inline - this second call just re-derives
    # `target` (already-validated, since resolve_claimed_owner is a pure
    # function of the same inputs) for the seller_prices overlay below,
    # which is a display concern the accessor itself has no reason to know
    # about.
    resolved_owner_admin_id = resolve_claimed_owner(db, principal, owner_admin_id, endpoint="list_packages")
    target = db.get(models.AdminUser, resolved_owner_admin_id) if resolved_owner_admin_id is not None else None
    seller_prices: dict[int, int] = {}
    if target is not None and not target.is_superadmin:
        if hierarchy.role(target) == hierarchy.ROLE_SELLER:
            seller_prices = {
                row.package_id: row.price
                for row in db.query(models.PackageSellerPrice)
                .filter(models.PackageSellerPrice.seller_admin_id == target.id)
                .all()
            }
    # (No `else` branch needed here anymore - bot_resources._list_packages_query
    # already applied the "shared/global bot, or an owner_admin_id that
    # resolved to the superadmin themself -> only NULL-owned packages" filter
    # itself, on the exact same resolved_owner_admin_id.)

    pkgs = q.order_by(models.Package.sort_order, models.Package.id).all()
    # Keyed on OWNERSHIP, not on the role column.
    #
    # It used to read `role(target) == ROLE_SELLER`, which is right only as
    # long as every level-3 account's stored role says so - and role() falls
    # back to deriving from parent_admin_id precisely because some rows
    # predate that column. An account whose role reads "admin" while sitting
    # under a parent skipped this filter entirely, and the package the owner
    # had kept for themselves showed up in that account's shop. Reported
    # 2026-09-15: «اونای که با گزینه عدم نمایش در بات فروشنده تیک خورده بود
    # توی اپ نماینده دیده میشه».
    #
    # Ownership is the question the flag actually asks - «نمایش به
    # فروشنده‌ها» means "anyone but me" - and it cannot be wrong in the way
    # a stored role can: the owner always sees their own package, and
    # everyone else in the tree needs the flag. Same restriction as
    # routers/packages.py's panel list, and it now holds for every surface
    # a downstream account reaches: their admin-menu pickers AND their own
    # dedicated bot's and Mini App's customer-facing shop.
    if target is not None and not target.is_superadmin:
        # Same restriction as routers/packages.py's panel list_packages -
        # an Admin can keep a package for their own use without handing it
        # to this Seller at all, and that has to hold everywhere this
        # Seller's owner_admin_id reaches: both the shared bot's
        # admin-menu package pickers (telegram_bot/handlers/admin_users.py,
        # the Seller creating/renewing a purchase for their own customer)
        # AND the Seller's OWN dedicated bot's customer-facing checkout
        # (AdminUser.own_bot_token) - both land in this exact function with
        # the same owner_admin_id, indistinguishable from here. Reported
        # 2026-09-09: seller_visible=False hid a package on the panel's
        # Packages page but it still showed up (and was purchasable) in the
        # bot either way - models.Package.seller_visible's docstring
        # claimed this was panel-only by design; it wasn't meant to leave
        # the bot as a back door around it.
        pkgs = [
            p for p in pkgs
            if p.seller_visible or p.owner_admin_id == target.id
        ]
    for p in pkgs:
        if p.id in seller_prices:
            # In-memory only, on this freshly-queried (never committed)
            # object - the customer's bot sees their reseller's price
            # without Package.price itself ever changing for anyone else.
            p.price = seller_prices[p.id]
    return pkgs


@router.get("/payment-info", response_model=schemas.PanelSettingsOut)
@bot_route_policy(capability=PAYMENT_READ, resource_strategy="claim")
def get_payment_info(
    owner_admin_id: Optional[int] = None, db: Session = Depends(get_db),
    principal: BotPrincipal = Depends(get_bot_principal),
):
    """Card-to-card payment details - shown by the sales bot right before it
    asks the customer for a receipt photo (also used for the top-up presets
    and support-contact text elsewhere in the bot, see telegram_bot/
    handlers/customer.py).

    owner_admin_id identifies WHICH bot is asking (see
    telegram_bot/panel_bridge.py's _scope() - None for the shared/global
    bot, an AdminUser id for a per-Admin/per-Seller own bot) - same shape as
    list_packages above. Every level-2 Admin and level-3 Seller now has
    their own dedicated bot with their own customers, who need to deposit
    into THAT reseller's card, not the superadmin's - so when owner_admin_id
    identifies a non-superadmin, their own_payment_* fields (models.
    AdminUser, set via PUT /api/settings/my-payment) overlay the global
    PanelSettings row IN-MEMORY (never committed - same trick as
    list_packages's per-seller price overlay), one field at a time: any
    field they haven't set themselves still falls back to the panel-wide
    default instead of showing the customer nothing. support_contact_text
    is overlaid the same way (own_support_contact_text, set via PUT
    /api/settings/my-payment - added 2026-09 so every admin with a
    dedicated bot has their own پشتیبانی contact, not just the shared
    bot's). referral/loyalty/HA/port fields remain untouched - still
    panel-wide only, not part of this per-admin overlay.

    Multi-card pools (services/payment_cards.py) then take priority over
    whichever single payment_card_number/holder the block above landed on:
    if the relevant pool (this admin's own, if any, otherwise the global
    one) has at least one registered active card, resolve_active_card's
    pick overrides payment_card_number/holder and resolved_payment_card_id
    is set on the response - the bot threads that id through into the
    pending purchase/top-up record it creates (telegram_bot/handlers/
    customer.py) so, once an admin approves it, "threshold" mode's
    accumulated-deposit tracking (record_payment_card_use below) knows
    exactly which card to credit. A pool with zero registered cards keeps
    showing the legacy single-card fields exactly as before - this is
    fully additive, no behavior change for a panel that never adopts the
    multi-card feature."""
    owner_admin_id = resolve_claimed_owner(db, principal, owner_admin_id, endpoint="get_payment_info")
    row = _get_or_create_settings(db)
    own_admin = None
    if owner_admin_id is not None:
        candidate = db.get(models.AdminUser, owner_admin_id)
        if candidate is not None and not candidate.is_superadmin:
            own_admin = candidate
    if own_admin is not None:
        # WHERE THE MONEY GOES does not fall back. Every other field on this
        # row degrades gracefully to the panel-wide default; a bank card
        # cannot. A reseller who has not set their own card used to have the
        # MAIN admin's card shown to their customers, so those customers
        # paid the wrong person - silently, correctly-looking, and for as
        # long as nobody noticed. Reported 2026-09-14.
        #
        # Blank instead. The bot and the Mini App both already handle an
        # empty card by telling the customer payment is not set up yet and
        # to contact support, which is a bad screen; being paid into someone
        # else's account is not a screen at all, it is a loss.
        #
        # The instructions travel with the card for the same reason - the
        # main admin's «فقط کارت به کارت، بعد رسید بفرستید» printed under a
        # reseller's card number describes a process that is not theirs.
        row.payment_card_number = own_admin.own_payment_card_number or ""
        row.payment_card_holder = own_admin.own_payment_card_holder or ""
        row.payment_instructions = own_admin.own_payment_instructions or ""
        # These two DO still fall back, deliberately. Top-up presets are
        # just suggested amounts, and a support contact that reaches
        # somebody beats one that reaches nobody - neither can misdirect a
        # payment.
        if own_admin.own_topup_presets:
            row.topup_presets = own_admin.own_topup_presets
        if own_admin.own_support_contact_text:
            row.support_contact_text = own_admin.own_support_contact_text

    if own_admin is not None:
        pool_owner_id = own_admin.id
        pool_mode = own_admin.own_payment_card_mode
        pool_active_id = own_admin.own_active_payment_card_id
    else:
        pool_owner_id = None
        pool_mode = row.payment_card_mode
        pool_active_id = row.active_payment_card_id
    card = payment_cards.resolve_active_card(db, pool_owner_id, pool_mode or "manual", pool_active_id)

    out = schemas.PanelSettingsOut.model_validate(row)
    if card:
        out.payment_card_number = card.card_number
        out.payment_card_holder = card.card_holder or ""
        out.resolved_payment_card_id = card.id
    return out


@router.get("/payment-cards/{card_id}", response_model=schemas.PaymentCardOut)
@bot_route_policy(capability=PAYMENT_READ, resource_strategy="payment_card_owner")
def get_payment_card(
    card_id: int, db: Session = Depends(get_db), principal: BotPrincipal = Depends(get_bot_principal),
):
    """Single card lookup by id - used by the bot to find out which
    Telegram id (if any) a customer's receipt should ALSO be routed to for
    approval (see models.PaymentCard.approval_telegram_id and telegram_bot/
    handlers/customer.py's _notify_targets). Deliberately looks up the
    EXACT card recorded on the pending request (its payment_card_id) by
    id, rather than re-resolving whichever card the pool currently
    considers active (get_payment_info's job) - the pool may well have
    rotated to a different card since the customer's receipt came in, and
    approval must still go by what was actually shown to them."""
    return bot_resources._get_payment_card_or_403(db, principal, card_id)


@router.post("/payment-cards/{card_id}/record-payment")
@bot_route_policy(capability=PAYMENT_WRITE, resource_strategy="payment_card_owner")
def record_payment_card_use(
    card_id: int, payload: schemas.BotRecordCardPaymentRequest, db: Session = Depends(get_db),
    principal: BotPrincipal = Depends(get_bot_principal),
):
    """Called once by telegram_bot/handlers/admin_pending.py right after a
    receipt/top-up payment is actually approved - see
    services/payment_cards.py's advance_after_payment for what this does
    ("threshold" mode's auto-switch-to-next-card bookkeeping; a harmless
    no-op for a pool in "manual"/"rotate" mode)."""
    bot_resources._get_payment_card_or_403(db, principal, card_id)  # authorization only - advance_after_payment re-fetches
    # With a registered approval the pool event carries its uuid and becomes
    # the approval's card_payment effect; a repeat for the same approval
    # changes nothing. Without an approval: unchanged.
    recorder = approval_effects.ShadowRecorder(db, payload.approval_uuid)
    # Counter and event log change together or not at all, and one approval
    # is counted once - also when two requests arrive at the same moment.
    #  - SQLite: the request becomes a write transaction first, so nobody
    #    can commit in between.
    #  - MariaDB: the pool's state row lock serializes the requests, and the
    #    one that waited restarts in a FRESH transaction when the database
    #    says its view is stale (error 1020 under snapshot isolation, or a
    #    deadlock/lock timeout): the retry then sees what the first wrote.
    for attempt in range(1, approval_registration.WRITE_ATTEMPTS + 1):
        try:
            if payment_card_events.enabled():
                approval_registration.take_write_lock(db)
            if recorder.card_payment_already_recorded():
                db.rollback()
                return {"ok": True}         # a repeat for the same approval: counted once
            event_uuid = recorder.card_event_uuid()
            written = payment_cards.advance_after_payment_core(db, card_id, payload.amount, approval_uuid=event_uuid)
            if written:
                if event_uuid:
                    recorder.card_payment()
                db.commit()
            return {"ok": True}
        except payment_card_events.DuplicateApprovalPayment:
            db.rollback()                   # lost the race to the same approval's other request: counted once
            return {"ok": True}
        except payment_card_events.PoolLogBroken:
            db.rollback()                   # no event and no demotion: no counter change either
            raise HTTPException(503, "ثبت پرداخت کارت موقتاً ممکن نیست")
        except OperationalError:
            db.rollback()
            if attempt == approval_registration.WRITE_ATTEMPTS:
                raise HTTPException(503, "ثبت پرداخت کارت موقتاً ممکن نیست")
            time.sleep(0.2 * attempt)
    return {"ok": True}


@router.get("/sales-stats")
@bot_route_policy(capability=CUSTOMER_READ, resource_strategy="claim")
def get_sales_stats(
    owner_admin_id: Optional[int] = None, db: Session = Depends(get_db),
    principal: BotPrincipal = Depends(get_bot_principal),
):
    """Compact sales summary for the bot's admin «📊 گزارش فروش» screen -
    today / last 7 days / last 30 days, read off the accounting ledger
    (see services/accounting.py) and scoped to whichever admin's bot is
    asking, exactly like list_packages/get_payment_info are."""
    owner_admin_id = resolve_claimed_owner(db, principal, owner_admin_id, endpoint="get_sales_stats")
    now = dt.datetime.utcnow()
    windows = {
        "today": now.replace(hour=0, minute=0, second=0, microsecond=0),
        "week": now - dt.timedelta(days=7),
        "month": now - dt.timedelta(days=30),
    }
    out: dict = {}
    for key, since in windows.items():
        q = db.query(models.LedgerEntry).filter(
            models.LedgerEntry.created_at >= since,
            models.LedgerEntry.kind.in_(accounting.SALE_KINDS),
        )
        if owner_admin_id is not None:
            q = q.filter(models.LedgerEntry.admin_id == owner_admin_id)
        rows = q.all()
        out[key] = {"total": sum(r.amount or 0 for r in rows), "count": len(rows)}

    # Already-resolved/validated owner_admin_id above - _list_users_query
    # re-resolving the same value through resolve_claimed_owner a second
    # time is a no-op (same principal, same claim), not a second decision.
    user_q = bot_resources._list_users_query(db, principal, owner_admin_id)
    out["users_total"] = user_q.count()
    out["users_active"] = user_q.filter(models.User.status == models.UserStatus.active).count()
    return out


@router.get("/customer-menu-config")
@bot_route_policy(capability=None, resource_strategy="none")
def get_customer_menu_config(db: Session = Depends(get_db), principal: BotPrincipal = Depends(get_bot_principal)):
    """Which customer main-menu buttons are hidden (see Settings > ربات >
    منوی مشتری and telegram_bot/keyboards.py's main_menu_kb), PLUS whether
    customers can use the bot AT ALL right now (Settings > ربات > «دسترسی
    مشتری‌ها به ربات فعال باشد» - models.BotSettings.customer_bot_enabled).
    Both are read live by every bot instance on every update (see
    runner.py's MaintenanceModeMiddleware and panel_bridge.py's/
    remote_bridge.py's get_customer_bot_enabled) instead of being baked in
    once at bot startup - the only way a bot running on a second/remote
    server (see services/remote_deploy.py) ever sees this setting at all,
    since its own local env has no field for it and its own local DB is
    just an empty throwaway file."""
    row = db.get(models.BotSettings, 1)
    raw = (row.customer_menu_disabled_items or "") if row else ""
    items = [x.strip() for x in raw.split(",") if x.strip()]
    customer_bot_enabled = row.customer_bot_enabled if row and row.customer_bot_enabled is not None else True
    return {"disabled_items": items, "customer_bot_enabled": customer_bot_enabled}


@router.get("/tutorials", response_model=list[schemas.TutorialOut])
@bot_route_policy(capability=CUSTOMER_READ, resource_strategy="tutorial_list")
def list_tutorials(
    owner_admin_id: Optional[int] = None, db: Session = Depends(get_db),
    principal: BotPrincipal = Depends(get_bot_principal),
):
    """Enabled tutorial entries, shown to customers from the bot's "📚
    آموزش" menu. Eager-loads `media` for the same reason list_packages
    eager-loads `connections`/`files` - the built-in bot converts this to a
    schema after its DB session has already closed.

    owner_admin_id identifies WHICH bot is asking (see
    telegram_bot/panel_bridge.py's _scope() - same shape as list_packages/
    get_payment_info above). Each superadmin/Admin has their own fully
    separate tutorial list now (models.Tutorial.owner_admin_id) - a
    Seller's own bot shows their PARENT Admin's list (see
    hierarchy.accessible_tutorial_owner_ids); the shared/global bot (or any
    owner_admin_id that resolves to the superadmin) shows only the
    superadmin's own NULL-owned tutorials."""
    return (
        bot_resources._list_tutorials_query(db, principal, owner_admin_id)
        .order_by(models.Tutorial.sort_order, models.Tutorial.id)
        .all()
    )


@router.get("/packages/{package_id}/files/{file_id}/download")
@bot_route_policy(capability=FILES_READ, resource_strategy="package_owner")
def download_package_file(
    package_id: int, file_id: int, db: Session = Depends(get_db),
    principal: BotPrincipal = Depends(get_bot_principal),
):
    """Raw bytes of a package's attached file - used by the bot (in-process
    or remote) to actually hand the file to the customer. The in-process
    bot doesn't need this (it reads stored_path straight off disk via
    panel_bridge.py), but a remotely-deployed bot has no local access to
    this server's disk, so it downloads the bytes over this endpoint
    instead - same X-API-Key auth as everything else on this router."""
    bot_resources._get_package_or_403(db, principal, package_id)
    row = (
        db.query(models.PackageFile)
        .filter(models.PackageFile.id == file_id, models.PackageFile.package_id == package_id)
        .first()
    )
    if not row or not os.path.exists(row.stored_path):
        raise HTTPException(404, "فایل پیدا نشد")
    return FileResponse(row.stored_path, filename=row.filename, media_type=row.content_type or "application/octet-stream")


@router.get("/tutorials/{tutorial_id}/media/{media_id}/download")
@bot_route_policy(capability=FILES_READ, resource_strategy="tutorial_owner")
def download_tutorial_media(
    tutorial_id: int, media_id: int, db: Session = Depends(get_db),
    principal: BotPrincipal = Depends(get_bot_principal),
):
    """Raw bytes of a tutorial's attached photo/video - same rationale as
    download_package_file above."""
    bot_resources._get_tutorial_or_403(db, principal, tutorial_id)
    row = (
        db.query(models.TutorialMedia)
        .filter(models.TutorialMedia.id == media_id, models.TutorialMedia.tutorial_id == tutorial_id)
        .first()
    )
    if not row or not os.path.exists(row.stored_path):
        raise HTTPException(404, "فایل پیدا نشد")
    return FileResponse(row.stored_path, filename=row.filename, media_type=row.content_type or "application/octet-stream")


@router.get("/tutorials/{tutorial_id}/software/{software_id}/download")
@bot_route_policy(capability=FILES_READ, resource_strategy="tutorial_owner")
def download_tutorial_software(
    tutorial_id: int, software_id: int, db: Session = Depends(get_db),
    principal: BotPrincipal = Depends(get_bot_principal),
):
    """Raw bytes of a tutorial's uploaded software file - same rationale as
    download_package_file above. Only applies to entries that have an
    uploaded file (stored_path set); link-only entries are just sent as a
    URL and never hit this endpoint."""
    bot_resources._get_tutorial_or_403(db, principal, tutorial_id)
    row = (
        db.query(models.TutorialSoftware)
        .filter(models.TutorialSoftware.id == software_id, models.TutorialSoftware.tutorial_id == tutorial_id)
        .first()
    )
    if not row or not row.stored_path or not os.path.exists(row.stored_path):
        raise HTTPException(404, "فایل پیدا نشد")
    return FileResponse(row.stored_path, filename=row.filename, media_type=row.content_type or "application/octet-stream")


@router.get("/admin-by-telegram/{tg_id}", response_model=schemas.BotAdminInfo)
@bot_route_policy(capability=ADMIN_LOOKUP, resource_strategy="admin_hierarchy")
def get_admin_by_telegram(
    tg_id: int, db: Session = Depends(get_db), principal: BotPrincipal = Depends(get_bot_principal),
):
    """Looked up by the built-in bot on every message from someone who
    isn't in the bot's global admin_ids list, to see whether they're
    instead a linked group-admin (AdminUser.telegram_id) who should get a
    scoped-down admin menu for their own group only - see
    telegram_bot/admin_scope.py."""
    admin = bot_resources._get_admin_by_telegram_or_403(db, principal, tg_id)
    if not admin:
        raise HTTPException(404, "ادمین پیدا نشد")
    # Asked once, here, so the bot can refuse a sale BEFORE walking someone
    # through a create/renew flow. Phrased as "why not" rather than a bool
    # so the bot has something to say - see schemas.BotAdminInfo.
    try:
        admin_billing.ensure_volume_available(admin)
        sell_block_reason = None
    except HTTPException as exc:
        sell_block_reason = str(exc.detail)
    return schemas.BotAdminInfo(
        id=admin.id,
        username=admin.username,
        is_superadmin=bool(admin.is_superadmin),
        sell_block_reason=sell_block_reason,
        role=hierarchy.role(admin),
        owner_ids=sorted(hierarchy.owned_admin_ids(db, admin)),
        # Same rule as hierarchy.user_visibility_clause: ownerless rows
        # belong to the superadmin, who is the only account that can
        # reassign them.
        include_unowned=bool(admin.is_superadmin),
    )


@router.get("/admin-username/{admin_id}")
@bot_route_policy(capability=ADMIN_LOOKUP, resource_strategy="admin_hierarchy")
def get_admin_username(
    admin_id: int, db: Session = Depends(get_db), principal: BotPrincipal = Depends(get_bot_principal),
):
    """This AdminUser's own username - used only to tag a pending request
    in the bot's «درخواست‌های در انتظار» list with WHICH admin/seller it
    belongs to (telegram_bot/handlers/admin_pending.py's _pending_summary,
    added 2026-09-10 - a superadmin legitimately sees every Admin's/
    Seller's pending requests mixed together with no indication of whose
    is whose). 404 if no such account, matching get_admin_by_telegram's
    convention just above - panel_bridge.py's/remote_bridge.py's
    get_admin_username both treat that as None (no tag), never an error."""
    admin = bot_resources._get_admin_or_403(db, principal, admin_id)
    return {"username": admin.username}


@router.get("/telegram-user-ids", response_model=list[int])
@bot_route_policy(capability=BROADCAST, resource_strategy="claim")
def telegram_user_ids(
    db: Session = Depends(get_db), owner_admin_id: Optional[int] = None,
    principal: BotPrincipal = Depends(get_bot_principal),
):
    """Every DISTINCT telegram id currently linked to a panel account - used
    by the admin bot's "📢 پیام همگانی" broadcast, which sends one generic
    message per chat id (as opposed to the daily quota/expiry reminder job,
    which queries per-User and is fine seeing the same telegram_id more than
    once - see services/notify.py). .distinct() matters now that a single
    telegram id can be linked to more than one User (see User.telegram_id in
    models.py) - without it, a customer with 2 linked accounts would get the
    same broadcast message twice.

    owner_admin_id scopes the recipient list to that account's own tree.
    This became load-bearing the moment level-2 Admins were given the full
    bot menu: an unscoped broadcast from one reseller would have messaged
    every OTHER reseller's customers, which is worse than the missing menu
    ever was.
    """
    query = (
        bot_resources._list_users_query(db, principal, owner_admin_id)
        .filter(models.User.telegram_id.isnot(None))
        .with_entities(models.User.telegram_id)
    )
    return [r[0] for r in query.distinct().all()]


@router.post("/users", response_model=schemas.BotUserResponse)
@bot_route_policy(capability=CUSTOMER_WRITE, resource_strategy="claim")
def create_user(
    payload: schemas.BotCreateUserRequest, db: Session = Depends(get_db),
    principal: BotPrincipal = Depends(get_bot_principal),
):
    # A locked customer must not be able to start a fresh account from the
    # same Telegram id and keep buying - that would leave the lock looking
    # enforced while doing nothing at all.
    _ensure_telegram_can_buy(db, payload.telegram_id)
    owner_admin_id = resolve_claimed_owner(db, principal, payload.owner_admin_id, endpoint="create_user")
    if payload.package_id:
        _new_package = bot_resources._get_package_or_403(db, principal, payload.package_id)
        _ensure_one_time_package_not_reused(
            db, _new_package, telegram_id=payload.telegram_id,
        )
        # The usual way a trial is taken: a brand-new customer, so there is
        # no `user` yet - only their Telegram id, which is what every rule
        # is keyed on anyway (see services/trial.py).
        trial.ensure_allowed(db, _new_package, telegram_id=payload.telegram_id)
    # Authorize payload.payment_card_id BEFORE create_user_record, which
    # commits internally - found during Phase C review
    # (docs/api-key-scope-audit-2026-09-27.md): checking it only later,
    # inside _record_bot_sale, meant an unauthorized card_id still left a
    # real User row committed by the time the request was refused.
    if payload.payment_card_id is not None:
        bot_resources._get_payment_card_or_403(db, principal, payload.payment_card_id)
    user = user_ops.create_user_record(
        db, payload.username, payload.full_name, payload.quota_gb, payload.expire_days,
        telegram_id=payload.telegram_id, owner_admin_id=owner_admin_id,
        package_id=payload.package_id,
    )
    # Marks where this customer came from, so the panel can show «ربات»
    # instead of «بدون ادمین» for a bot signup that legitimately has no
    # owning reseller - see models.User.created_via.
    user.created_via = "bot"
    # Receipt-approval effects (services/receipt_approval_effects.py). A
    # no-op unless the bot registered this approval, i.e. unless the
    # panel's registration mode is 'shadow'; never raises, never blocks.
    recorder = approval_effects.ShadowRecorder(db, payload.approval_uuid)
    if recorder.active:
        db.flush()
        recorder.effect("user_created", "user", user, approval_effects.UserCreatedEvidence(
            package_id=payload.package_id, quota_bytes=int(user.total_quota_bytes or 0),
            days=payload.expire_days or None))
    # Every connection in this one request is one purchase - share a single
    # batch (see models.Connection.purchase_batch) so the bot's "اکانت من"
    # groups them together instead of listing each service separately.
    batch = uuid.uuid4().hex if payload.connections else None
    connection_authorization_scope = resolve_bot_authorization_scope(principal)
    created_connections = []
    for spec in payload.connections:
        node = db.get(models.Node, spec.node_id)
        if not node:
            continue
        created_connections.append(user_ops.provision_connection(
            db, user, node, spec.protocol, spec.flow or "",
            purchase_batch=batch, package_name=payload.package_name,
            authorization_scope=connection_authorization_scope,
        ))

    # Turn what was just provisioned into a real, independently-enforced
    # service instead of leaving it on the customer's shared pool.
    #
    # Without this, a customer's FIRST purchase was always a legacy shared
    # service and only their second onwards was a proper one: purchase_
    # package (the existing-customer path) creates a Purchase, and this
    # brand-new-customer path never did. The panel then showed "سرویس
    # اشتراکی (قدیمی)" with a "convert" button on an account bought
    # minutes earlier, which is exactly how it was reported.
    #
    # absorb_legacy_pool_into_purchase is the same function the startup
    # migration uses, and it carries the user-level quota/usage/expiry onto
    # the Purchase 1:1 - so nothing is double-counted: once any Purchase
    # exists, User.effective_quota_bytes stops reading the user-level
    # number at all.
    new_purchase = None
    if payload.connections:
        new_purchase = user_ops.absorb_legacy_pool_into_purchase(db, user, comment=payload.comment)

    # Reuses the same already-authorized fetch above rather than a second
    # raw db.get - one _get_package_or_403 call per package_id per request.
    package = _new_package if payload.package_id else None
    # Charged AFTER provisioning here, unlike everywhere else: this endpoint
    # is called from the receipt-approval handler, so the customer has
    # already paid. Refusing at this point would take their money and give
    # them nothing. The reseller goes into debt instead - which their
    # overdraft is for, and which the superadmin can see.
    _charge_seller(db, user, package)
    sale_entry = _record_bot_sale(db, principal, "sale_new", payload, user, package)
    if recorder.active:
        db.flush()
        recorder.effect("purchase_created", "purchase", new_purchase,
                        approval_effects.PurchaseCreatedEvidence(days=payload.expire_days or None))
        for connection in created_connections:
            recorder.connection(connection)
        recorder.effect("ledger_sale", "sale", sale_entry)
        recorder.tag_ledger(sale_entry)
    db.commit()
    db.refresh(user)
    return _user_response(user)


@router.post("/users/{username}/purchase-package", response_model=schemas.BotPurchaseResponse)
@bot_route_policy(capability=CUSTOMER_WRITE, resource_strategy="user")
def purchase_package(
    username: str, payload: schemas.BotPurchasePackageRequest, db: Session = Depends(get_db),
    owner_admin_id: Optional[int] = None, principal: BotPrincipal = Depends(get_bot_principal),
):
    """Bot counterpart of routers/users.py's apply_package ("افزودن پکیج") -
    gives an EXISTING customer a NEW, independently-enforced Purchase (its
    own quota_bytes/expire_at, not merged into the user's combined fields)
    instead of the old add_connection()+renew() pairing the sales bot's
    "new" purchase flow used to funnel an already-registered customer's
    purchase through. That old pairing pooled the new package's quota/
    duration into the user's SHARED total_quota_bytes/expire_at - the exact
    bug report this endpoint fixes: buying a second service looked, from
    the customer's side, exactly like the first service had just been
    renewed (a later expiry date, no visibly separate new service), because
    functionally that's what it was doing.

    payload.connections overrides package.connections when given - used for
    a "plain" package with no admin-defined bundle, where the customer
    picked exactly one node/protocol by hand in the bot's purchase flow
    (see telegram_bot/handlers/customer.py's pick_node/pick_protocol)."""
    user = bot_resources._get_user_or_403(db, principal, username, owner_admin_id)
    _ensure_can_buy(user)
    package = bot_resources._get_package_or_403(db, principal, payload.package_id)
    _ensure_one_time_package_not_reused(db, package, user=user, telegram_id=user.telegram_id)
    trial.ensure_allowed(db, package, user=user, telegram_id=user.telegram_id)
    # Same ordering fix as create_user above: authorize payload.
    # payment_card_id BEFORE apply_package_as_purchase, which commits
    # internally - checking it only later inside _record_bot_sale left an
    # unauthorized card_id's Purchase/Connection rows already committed by
    # the time the request was refused.
    if payload.payment_card_id is not None:
        bot_resources._get_payment_card_or_403(db, principal, payload.payment_card_id)
    override = (
        [{"node_id": c.node_id, "protocol": c.protocol, "flow": c.flow or ""} for c in payload.connections]
        if payload.connections else None
    )
    purchase = user_ops.apply_package_as_purchase(
        db, user, package, connections_override=override, comment=payload.comment, principal=principal,
    )
    _charge_seller(db, user, package)
    sale_entry = _record_bot_sale(db, principal, "sale_new", payload, user, package, purchase_id=purchase.id)
    recorder = approval_effects.ShadowRecorder(db, payload.approval_uuid)      # no-op without an approval
    if recorder.active:
        db.flush()
        recorder.effect("purchase_created", "purchase", purchase,
                        approval_effects.PurchaseCreatedEvidence(days=package.duration_days or None))
        for connection in purchase.connections:
            recorder.connection(connection)
        recorder.effect("ledger_sale", "sale", sale_entry)
        recorder.tag_ledger(sale_entry)
    db.commit()
    db.refresh(user)
    return schemas.BotPurchaseResponse(
        user=_user_response(user),
        connections=[_connection_info(c) for c in purchase.connections],
    )


@router.post("/referral/apply")
@bot_route_policy(capability=CUSTOMER_WRITE, resource_strategy="user")
def apply_referral(
    payload: schemas.ReferralApplyRequest, db: Session = Depends(get_db),
    principal: BotPrincipal = Depends(get_bot_principal),
):
    """Called once, right after admin_pending.py's receipt-approval handler
    creates a brand-new customer account via create_user above - the ONLY
    choke point new customer accounts are created through, so this is the
    one place referral-code redemption needs to be wired in. Actual reward
    logic lives in services/user_ops.py's apply_referral_code (both the
    referrer and the new user get a gift, per the confirmed design - not
    just the referrer)."""
    user = bot_resources._get_user_or_403(db, principal, payload.username, None)
    # Receipt-approval effects of the rewards that are really applied, written
    # INSIDE apply_referral_code's own transaction, just before its commit
    # (design 5.3: mutation and record_effect are one transaction). A no-op
    # without an approval; never changes the outcome. A reward the manifest
    # did not expect - or expected on another resource - is refused in its
    # own savepoint and logged as a shadow event; the reward still commits.
    referral_evidence: list = []
    recorder = approval_effects.ShadowRecorder(db, payload.approval_uuid)

    def _record_effects() -> None:
        for effect_type, effect_key, resource, evidence in referral_evidence:
            recorder.effect(effect_type, effect_key, resource, evidence)

    ok, reason = user_ops.apply_referral_code(
        db, user, payload.referral_code, evidence_sink=referral_evidence if recorder.active else None,
        before_commit=_record_effects if recorder.active else None)
    return {"ok": ok, "reason": reason}


@router.post("/discount/validate", response_model=schemas.DiscountValidateResult)
@bot_route_policy(capability=CUSTOMER_WRITE, resource_strategy="claim")
def validate_discount(
    payload: schemas.DiscountValidateRequest, db: Session = Depends(get_db),
    principal: BotPrincipal = Depends(get_bot_principal),
):
    """Check-as-you-type step - does NOT consume the code (see
    /discount/redeem for that). `username`, when the customer already has
    an account, also catches "you already used this code" before they get
    to the final confirm screen."""
    owner_admin_id = resolve_claimed_owner(db, principal, payload.owner_admin_id, endpoint="validate_discount")
    valid, reason, amount = user_ops.validate_discount_code(
        db, payload.code, payload.package_price, username=payload.username,
        owner_admin_id=owner_admin_id,
    )
    return schemas.DiscountValidateResult(
        valid=valid,
        reason=reason or None,
        discount_amount=amount,
        final_price=max(0, (payload.package_price or 0) - amount),
    )


@router.post("/discount/redeem", response_model=schemas.DiscountValidateResult)
@bot_route_policy(capability=CUSTOMER_WRITE, resource_strategy="claim")
def redeem_discount(
    payload: schemas.DiscountRedeemRequest, db: Session = Depends(get_db),
    principal: BotPrincipal = Depends(get_bot_principal),
):
    """Called once a purchase is actually confirmed - re-validates then
    atomically consumes the code (bumps used_count, records a
    DiscountCodeRedemption row). See services/user_ops.py's
    redeem_discount_code."""
    owner_admin_id = resolve_claimed_owner(db, principal, payload.owner_admin_id, endpoint="redeem_discount")
    discount_evidence: list = []
    ok, reason, amount = user_ops.redeem_discount_code(
        db, payload.code, payload.username, payload.package_price,
        owner_admin_id=owner_admin_id, evidence_sink=discount_evidence,
    )
    recorder = approval_effects.ShadowRecorder(db, payload.approval_uuid)      # no-op without an approval
    if recorder.active and discount_evidence:
        redemption, evidence = discount_evidence[0]
        recorder.effect("discount_redeemed", f"discount:{redemption.code_id}", redemption, evidence)
        db.commit()
    return schemas.DiscountValidateResult(
        valid=ok,
        reason=reason or None,
        discount_amount=amount,
        final_price=max(0, (payload.package_price or 0) - amount),
    )


@router.get("/users", response_model=schemas.BotUserListPage)
@bot_route_policy(capability=CUSTOMER_READ, resource_strategy="user_list")
def list_users(
    db: Session = Depends(get_db),
    page: int = 1,
    page_size: int = 20,
    search: Optional[str] = None,
    owner_admin_id: Optional[int] = None,
    principal: BotPrincipal = Depends(get_bot_principal),
):
    """Used by the admin side of the sales bot to browse/search customers.
    owner_admin_id, when given, scopes this to one group-admin's own users
    (see telegram_bot/admin_scope.py) - omitted entirely by the global bot
    admin flow, which still sees everyone."""
    page = max(page, 1)
    page_size = min(max(page_size, 1), 100)
    query = bot_resources._list_users_query(db, principal, owner_admin_id, search=search)
    total = query.count()
    items = (
        query.order_by(models.User.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/users/by-telegram/{telegram_id}", response_model=schemas.BotUserResponse)
@bot_route_policy(capability=CUSTOMER_READ, resource_strategy="user_list")
def get_user_by_telegram(
    telegram_id: int, db: Session = Depends(get_db), owner_admin_id: Optional[int] = None,
    principal: BotPrincipal = Depends(get_bot_principal),
):
    """Single-account lookup - kept for callers that only ever cared about
    "the" account for this telegram id (e.g. the daily notify job, the
    /start greeting). Now that telegram_id can point at more than one User,
    this returns the most-recently-linked one; anything customer-facing
    that needs to let the person pick among several should use
    list_users_by_telegram below instead.

    owner_admin_id, when given (a per-admin dedicated bot - see
    panel_bridge.py's _scope()), only considers accounts already inside
    that Admin's own tree - a customer's account under a DIFFERENT Admin's
    bot should never surface here, same isolation as the rest of the
    3-tier hierarchy."""
    query = bot_resources._list_users_query(db, principal, owner_admin_id, telegram_id=telegram_id)
    user = query.order_by(models.User.id.desc()).first()
    if not user:
        raise HTTPException(404, "کاربری با این حساب تلگرام پیدا نشد")
    return _user_response(user)


@router.get("/users/by-telegram/{telegram_id}/all", response_model=list[schemas.BotUserResponse])
@bot_route_policy(capability=CUSTOMER_READ, resource_strategy="user_list")
def list_users_by_telegram(
    telegram_id: int, db: Session = Depends(get_db), owner_admin_id: Optional[int] = None,
    principal: BotPrincipal = Depends(get_bot_principal),
):
    """Every account linked to this telegram id (could be 0, 1, or several -
    see the big comment on User.telegram_id in models.py). The bot uses
    this to decide whether to act directly (0 or 1 result) or show an
    account picker (2+ results) - see telegram_bot's _resolve_account.

    owner_admin_id scopes this to one Admin's own tree, same rationale as
    get_user_by_telegram above - a per-admin bot's account-picker should
    never surface someone else's customer accounts from another Admin."""
    query = bot_resources._list_users_query(db, principal, owner_admin_id, telegram_id=telegram_id)
    users = query.order_by(models.User.id.desc()).all()
    return [_user_response(u) for u in users]


@router.get("/users/{username}", response_model=schemas.BotUserResponse)
@bot_route_policy(capability=CUSTOMER_READ, resource_strategy="user")
def get_user(
    username: str, db: Session = Depends(get_db), owner_admin_id: Optional[int] = None,
    principal: BotPrincipal = Depends(get_bot_principal),
):
    return _user_response(bot_resources._get_user_or_403(db, principal, username, owner_admin_id))


@router.post("/users/{username}/link-telegram", response_model=schemas.BotUserResponse)
@bot_route_policy(capability=IDENTITY_WRITE, resource_strategy="user")
def link_telegram(
    username: str, payload: schemas.BotLinkTelegramRequest, db: Session = Depends(get_db),
    principal: BotPrincipal = Depends(get_bot_principal),
):
    # telegram_id is intentionally NOT required to be unique across users -
    # one Telegram account can be linked to several panel accounts (a
    # customer who bought more than once under different usernames). The
    # bot's "🔗 وصل کردن حساب قبلی" flow (telegram_bot/handlers/customer.py)
    # just adds this account to that telegram id's list; when there's more
    # than one, the bot shows an account picker (see list_users_by_telegram
    # below + telegram_bot's _resolve_account).
    user = bot_resources._get_user_or_403(db, principal, username, None)
    user.telegram_id = payload.telegram_id
    db.commit()
    db.refresh(user)
    return _user_response(user)


@router.post("/users/{username}/connections", response_model=schemas.BotConnectionInfo)
@bot_route_policy(capability=CUSTOMER_WRITE, resource_strategy="user+node")
def add_connection(
    username: str, spec: schemas.BotCreateConnectionSpec, db: Session = Depends(get_db),
    owner_admin_id: Optional[int] = None, principal: BotPrincipal = Depends(get_bot_principal),
):
    user = bot_resources._get_user_or_403(db, principal, username, owner_admin_id)
    node = bot_resources._get_node_or_403(db, principal, spec.node_id)
    conn = user_ops.provision_connection(
        db, user, node, spec.protocol, spec.flow or "",
        purchase_batch=spec.purchase_batch, package_name=spec.package_name,
        authorization_scope=resolve_bot_authorization_scope(principal),
    )
    return _connection_info(conn)


@router.get("/users/{username}/purchases", response_model=list[schemas.BotPurchaseInfo])
@bot_route_policy(capability=CUSTOMER_READ, resource_strategy="user")
def list_user_purchases(
    username: str, db: Session = Depends(get_db), owner_admin_id: Optional[int] = None,
    principal: BotPrincipal = Depends(get_bot_principal),
):
    """The customer's independently-tracked services, for the bot's
    «کدام سرویس را تمدید می‌کنید؟» picker (renewal always continues one
    SPECIFIC existing service - see renew_service below)."""
    user = bot_resources._get_user_or_403(db, principal, username, owner_admin_id)
    out = []
    for p in sorted(user.purchases, key=lambda p: p.created_at or dt.datetime.min, reverse=True):
        info = schemas.BotPurchaseInfo.model_validate(p)
        info.connection_count = len(p.connections)
        out.append(info)
    return out


@router.post("/users/{username}/purchases/{purchase_id}/rename", response_model=schemas.BotPurchaseInfo)
@bot_route_policy(capability=CUSTOMER_WRITE, resource_strategy="user")
def rename_purchase(
    username: str, purchase_id: int, payload: schemas.BotRenamePurchaseRequest,
    db: Session = Depends(get_db), owner_admin_id: Optional[int] = None,
    principal: BotPrincipal = Depends(get_bot_principal),
):
    """Bot counterpart of the panel's own "📝 یادداشت/نام سرویس" field on
    UserDetail.jsx - lets the customer set/change it themselves instead of
    only an admin being able to (see handlers/customer_account.py's
    «✏️ تغییر نام» flow)."""
    user = bot_resources._get_user_or_403(db, principal, username, owner_admin_id)
    purchase = db.get(models.Purchase, purchase_id)
    if not purchase or purchase.user_id != user.id:
        raise HTTPException(404, "سرویس پیدا نشد")
    purchase = user_ops.rename_purchase(db, purchase, payload.comment)
    info = schemas.BotPurchaseInfo.model_validate(purchase)
    info.connection_count = len(purchase.connections)
    return info


# Both delete endpoints below are the customer's own self-service "🗑 حذف"
# (handlers/customer_account.py, added 2026-09-23) - unlike the admin
# panel's DELETE /users/{id}/purchases/{id} (routers/users.py), there is no
# password confirmation here (the bot has no admin session to confirm
# with), so eligibility is checked server-side instead of trusting the
# caller: only a service that is ALREADY expired/quota-exceeded (i.e. the
# customer gets no further use out of it either way) can be removed this
# way. A customer who wants to delete something still active has to ask an
# admin, same as before this feature existed.
def _ensure_deletable_purchase(purchase: models.Purchase) -> None:
    if purchase.status not in (models.UserStatus.expired, models.UserStatus.quota_exceeded):
        raise HTTPException(400, "این سرویس هنوز فعال است - فقط سرویس‌های تمام‌شده یا منقضی قابل حذف هستند")


@router.delete("/users/{username}/purchases/{purchase_id}")
@bot_route_policy(capability=CUSTOMER_WRITE, resource_strategy="user")
def delete_purchase(
    username: str, purchase_id: int,
    db: Session = Depends(get_db), owner_admin_id: Optional[int] = None,
    principal: BotPrincipal = Depends(get_bot_principal),
):
    """Deletes one of the customer's own expired/exhausted services (all of
    its connections, deprovisioned from their nodes, then the Purchase row
    itself) - same underlying work as the admin panel's own delete-purchase
    button (routers/users.py's delete_purchase), just self-service and
    scoped to services that are already unusable (see
    _ensure_deletable_purchase)."""
    user = bot_resources._get_user_or_403(db, principal, username, owner_admin_id)
    purchase = db.get(models.Purchase, purchase_id)
    if not purchase or purchase.user_id != user.id:
        raise HTTPException(404, "سرویس پیدا نشد")
    _ensure_deletable_purchase(purchase)
    removed = 0
    failed: list[str] = []
    for conn in list(purchase.connections):
        try:
            user_ops.delete_connection(db, conn)
            removed += 1
        except Exception as exc:  # noqa: BLE001 - one unreachable node must
            # not strand the whole service half-deleted; report and go on.
            failed.append(f"{conn.type.value}: {exc}")
    db.delete(purchase)
    db.commit()
    return {"ok": True, "connections_removed": removed, "failed": failed}


@router.delete("/users/{username}/connections/{connection_id}")
@bot_route_policy(capability=CUSTOMER_WRITE, resource_strategy="user")
def delete_connection(
    username: str, connection_id: int,
    db: Session = Depends(get_db), owner_admin_id: Optional[int] = None,
    principal: BotPrincipal = Depends(get_bot_principal),
):
    """Same self-service delete as delete_purchase above, for a connection
    that predates the Purchase feature (or was added one-at-a-time) and so
    has no purchase_id of its own - governed by the user's own combined
    status instead of a Purchase's (see models.Purchase's docstring)."""
    user = bot_resources._get_user_or_403(db, principal, username, owner_admin_id)
    conn = db.get(models.Connection, connection_id)
    if not conn or conn.user_id != user.id:
        raise HTTPException(404, "کانکشن پیدا نشد")
    if conn.purchase_id is not None:
        # Has its own Purchase after all - the customer's client is out of
        # sync (it should have called delete_purchase instead); refuse
        # rather than silently deleting just one connection of a service
        # that might have several.
        raise HTTPException(400, "این اتصال بخشی از یک سرویس است - حذف باید از طریق همان سرویس انجام شود")
    if user.status not in (models.UserStatus.expired, models.UserStatus.quota_exceeded):
        raise HTTPException(400, "این سرویس هنوز فعال است - فقط سرویس‌های تمام‌شده یا منقضی قابل حذف هستند")
    user_ops.delete_connection(db, conn)
    return {"ok": True}


@router.get("/users/{username}/subscription-link", response_model=schemas.BotSubscriptionLinkOut)
@bot_route_policy(capability=CUSTOMER_READ, resource_strategy="user")
def get_bot_subscription_link(
    username: str, db: Session = Depends(get_db), owner_admin_id: Optional[int] = None,
    principal: BotPrincipal = Depends(get_bot_principal),
):
    """Bot counterpart of routers/users.py's get_subscription_link, for the
    "🔗 دریافت لینک ساب" bot menu button (telegram_bot/handlers/
    customer.py) - a customer fetching their OWN link, rather than an
    admin looking one up from the panel.

    Unlike the admin-panel version, this returns ABSOLUTE urls: the bot
    process has no browser "origin" to prefix a relative path with (see
    schemas.BotSubscriptionLinkOut's docstring), so it needs
    PanelSettings.panel_public_url, set once from Settings. Both fields
    come back None until an admin configures that - the bot tells the
    customer support needs to set it up rather than sending a link that
    can never resolve to anything."""
    user = bot_resources._get_user_or_403(db, principal, username, owner_admin_id)
    settings_row = db.get(models.PanelSettings, 1)
    base = (settings_row.panel_public_url or "").strip().rstrip("/") if settings_row else ""
    if not base:
        return schemas.BotSubscriptionLinkOut()
    token = user_ops.ensure_subscription_token(db, user)
    return schemas.BotSubscriptionLinkOut(web_url=f"{base}/s/{token}", app_url=f"{base}/api/subscribe/{token}")


@router.get("/miniapp-button-text")
@bot_route_policy(capability=None, resource_strategy="none")
def get_miniapp_button_text(db: Session = Depends(get_db), principal: BotPrincipal = Depends(get_bot_principal)):
    """Label for the Mini App launcher beside the text box, for every bot
    this panel runs - see models.BotSettings.miniapp_button_text and
    telegram_bot/runner.py's _set_menu_button."""
    row = db.get(models.BotSettings, 1)
    return {"text": ((row.miniapp_button_text or "").strip() if row else "")}


@router.get("/panel-public-url")
@bot_route_policy(capability=None, resource_strategy="none")
def get_panel_public_url(db: Session = Depends(get_db), principal: BotPrincipal = Depends(get_bot_principal)):
    """PanelSettings.panel_public_url, for callers that need to build an
    absolute link. Used by telegram_bot/runner.py to point each bot's Menu
    button at this panel's Mini App - see _set_menu_button there."""
    settings_row = db.get(models.PanelSettings, 1)
    return {"url": (settings_row.panel_public_url or "").strip().rstrip("/") if settings_row else ""}


@router.post("/users/{username}/purchases/{purchase_id}/renew", response_model=schemas.BotUserResponse)
@bot_route_policy(capability=CUSTOMER_WRITE, resource_strategy="user")
def renew_service(
    username: str, purchase_id: int, payload: schemas.BotRenewRequest,
    db: Session = Depends(get_db), owner_admin_id: Optional[int] = None,
    principal: BotPrincipal = Depends(get_bot_principal),
):
    """Renews ONE specific service: same connections/credentials, the new
    package queued (reserved) behind whatever the service has left and
    auto-activated the moment it runs out - or applied immediately if the
    service is already exhausted (see user_ops.renew_purchase). Never
    creates anything new - renewal means CONTINUING the same service, per
    the panel owner's definition (2026-08-09)."""
    user = bot_resources._get_user_or_403(db, principal, username, owner_admin_id)
    _ensure_can_buy(user)
    purchase = db.get(models.Purchase, purchase_id)
    if not purchase or purchase.user_id != user.id:
        raise HTTPException(404, "سرویس پیدا نشد")
    # Authorize payload.package_id BEFORE renew_purchase ever touches the
    # row - found during Phase C review (docs/api-key-scope-audit-
    # 2026-09-27.md): renew_purchase commits internally (it stamps
    # reserved_package_id/package_id and calls db.commit() itself), so
    # calling it first and only checking ownership afterward would let an
    # unauthorized package_id land durably on the Purchase even though the
    # request goes on to 404.
    renew_package = bot_resources._get_package_or_403(db, principal, payload.package_id) if payload.package_id else None
    # Same reasoning as the package_id check just above - renew_purchase
    # commits regardless of package_id, so payment_card_id has to be
    # authorized before it too, not inside _record_bot_sale afterward.
    if payload.payment_card_id is not None:
        bot_resources._get_payment_card_or_403(db, principal, payload.payment_card_id)
    renewal_evidence: list = []
    user_ops.renew_purchase(db, purchase, payload.add_gb, payload.add_days, payload.reset_usage,
                            package_id=payload.package_id, evidence_sink=renewal_evidence)
    sale_entry = None
    _charge_seller(db, user, renew_package, payload.add_gb or 0)
    if payload.package_id or payload.paid_amount is not None:
        sale_entry = _record_bot_sale(db, principal, "sale_renew", payload, user, renew_package, purchase_id=purchase.id)
    _record_renewal(db, payload, purchase, renewal_evidence, sale_entry)
    db.commit()
    db.refresh(user)
    db.refresh(purchase)
    out = _user_response(user)
    # Surface THIS purchase's queued-renewal state in the user-level
    # reserved fields the bot's confirmation messages already read - the
    # user-level ones are always empty post-migration, which would make a
    # queued renewal read as an instant one.
    if purchase.reserved_quota_bytes or purchase.reserved_duration_days:
        out.reserved_quota_gb = (purchase.reserved_quota_bytes / (1024 ** 3)) if purchase.reserved_quota_bytes else None
        out.reserved_duration_days = purchase.reserved_duration_days
    return out


@router.post("/users/{username}/renew", response_model=schemas.BotUserResponse)
@bot_route_policy(capability=CUSTOMER_WRITE, resource_strategy="user")
def renew(
    username: str, payload: schemas.BotRenewRequest, db: Session = Depends(get_db),
    owner_admin_id: Optional[int] = None, principal: BotPrincipal = Depends(get_bot_principal),
):
    user = bot_resources._get_user_or_403(db, principal, username, owner_admin_id)
    _ensure_can_buy(user)
    # Post-migration (services/purchase_migration.py) the user-level pool
    # governs nothing for a fully-converted customer - a renewal landing
    # here (old bot build, or a flow that didn't pick a service) would
    # write to dead fields. If the customer has exactly one service, the
    # intent is unambiguous: renew THAT service. With several services the
    # bot's picker flow (renew_service above) should have been used; only
    # then fall back to the legacy user-level behavior.
    legacy_conns = [c for c in user.connections if c.purchase_id is None]
    if not legacy_conns and len(user.purchases) == 1:
        return renew_service(
            username, user.purchases[0].id, payload, db=db, owner_admin_id=owner_admin_id, principal=principal,
        )
    # Same ordering fix as renew_service above: authorize payload.package_id
    # and payload.payment_card_id BEFORE renew_user, which also commits
    # internally.
    renew_package = bot_resources._get_package_or_403(db, principal, payload.package_id) if payload.package_id else None
    if payload.payment_card_id is not None:
        bot_resources._get_payment_card_or_403(db, principal, payload.payment_card_id)
    renewal_evidence: list = []
    user_ops.renew_user(db, user, payload.add_gb, payload.add_days, payload.reset_usage,
                        package_id=payload.package_id, evidence_sink=renewal_evidence)
    sale_entry = None
    # Accounting: only a package-based renewal (or one where the bot sent
    # the exact paid amount) is a paid event - a bare reset_usage or manual
    # add_gb/add_days admin favor isn't a sale.
    _charge_seller(db, user, renew_package, payload.add_gb or 0)
    if payload.package_id or payload.paid_amount is not None:
        sale_entry = _record_bot_sale(db, principal, "sale_renew", payload, user, renew_package)
    _record_renewal(db, payload, user, renewal_evidence, sale_entry)
    db.commit()
    return _user_response(user)


@router.post("/users/{username}/reset-usage", response_model=schemas.BotUserResponse)
@bot_route_policy(capability=CUSTOMER_WRITE, resource_strategy="user")
def reset_usage(
    username: str, db: Session = Depends(get_db), owner_admin_id: Optional[int] = None,
    principal: BotPrincipal = Depends(get_bot_principal),
):
    user = bot_resources._get_user_or_403(db, principal, username, owner_admin_id)
    user_ops.renew_user(db, user, reset_usage=True)
    return _user_response(user)


@router.post("/users/{username}/set-enabled", response_model=schemas.BotUserResponse)
@bot_route_policy(capability=CUSTOMER_WRITE, resource_strategy="user")
def set_user_enabled(
    username: str, enabled: bool, db: Session = Depends(get_db), owner_admin_id: Optional[int] = None,
    principal: BotPrincipal = Depends(get_bot_principal),
):
    """Enables/disables the user AND actually pushes the change to every
    node they have a connection on (unlike just flipping the status column,
    which the background poller would otherwise silently revert back to
    "active" once quota/expiry no longer justify it)."""
    user = bot_resources._get_user_or_403(db, principal, username, owner_admin_id)
    user.status = models.UserStatus.active if enabled else models.UserStatus.disabled
    for conn in user.connections:
        _set_connection_enabled(db, conn, enabled=enabled)
    db.commit()
    db.refresh(user)
    return _user_response(user)


@router.post("/users/{username}/add-balance", response_model=schemas.BotUserResponse)
@bot_route_policy(capability=WALLET_WRITE, resource_strategy="user")
def add_balance(
    username: str, payload: schemas.BotAddBalanceRequest, db: Session = Depends(get_db),
    principal: BotPrincipal = Depends(get_bot_principal),
):
    """Credits (or, with a negative amount, debits) the user's wallet-style
    balance - used by the sales bot's "افزایش اعتبار" top-up flow (admin
    approval, positive amount) and the "پرداخت از اعتبار" purchase flow
    (negative amount, debit).

    Debits are applied as a single atomic conditional UPDATE (`WHERE
    balance + amount >= 0`) instead of a Python read-modify-write, so two
    concurrent/duplicate debit requests (e.g. a double-tapped purchase
    button) can't both succeed and drive the balance negative - the second
    one gets a clean "insufficient balance" error instead of silently
    overdrawing the wallet."""
    user = bot_resources._get_user_or_403(db, principal, username, None)
    # Only a TOP-UP is blocked. A negative amount is the wallet being spent
    # on a purchase, and that purchase is already refused upstream - but if
    # one ever reaches here, refusing the debit too would be the wrong way
    # round: it would take the money and give nothing.
    if payload.amount > 0:
        _ensure_can_buy(user)
    if payload.amount < 0:
        result = db.execute(
            models.User.__table__.update()
            .where(models.User.id == user.id, (models.User.balance + payload.amount) >= 0)
            .values(balance=models.User.balance + payload.amount)
        )
        db.commit()
        if result.rowcount == 0:
            raise HTTPException(400, "موجودی کیف پول کافی نیست")
        db.refresh(user)
    else:
        balance_before = int(user.balance or 0)
        topup_entry = None
        user.balance = (user.balance or 0) + payload.amount
        # Accounting: a positive credit is an approved wallet top-up - cash
        # that actually arrived on a card (see services/accounting.py's
        # LedgerEntry kind docs for why the negative/debit branch above is
        # deliberately NOT recorded - the sale row already covers it).
        if payload.amount > 0:
            # Same payment_card_id-ownership gap as _record_bot_sale's own
            # fix above, found while auditing every OTHER accounting.record
            # call site for the same pattern (not just the one Product's
            # review pointed at) - a top-up's card needs the same check a
            # sale's card gets, or this call site alone would still corrupt
            # ledger attribution once enforcement is on.
            if payload.payment_card_id is not None:
                bot_resources._get_payment_card_or_403(db, principal, payload.payment_card_id)
            topup_entry = accounting.record(
                db, "wallet_topup", payload.amount,
                user=user,
                admin_id=user.owner_admin_id,
                payment_card_id=payload.payment_card_id,
                payment_method="card",
            )
        recorder = approval_effects.ShadowRecorder(db, payload.approval_uuid)      # no-op without an approval
        if recorder.active and topup_entry is not None:
            db.flush()
            recorder.effect("wallet_credit_source_created", "credit:receipt_topup", user,
                            approval_effects.WalletCreditEvidence(balance_before, int(user.balance or 0)))
            recorder.effect("ledger_topup", "topup", topup_entry)
            recorder.tag_ledger(topup_entry)
        db.commit()
        db.refresh(user)
    return _user_response(user)


@router.delete("/users/{username}")
@bot_route_policy(capability=CUSTOMER_WRITE, resource_strategy="user")
def delete_user(
    username: str, db: Session = Depends(get_db), owner_admin_id: Optional[int] = None,
    principal: BotPrincipal = Depends(get_bot_principal),
):
    user = bot_resources._get_user_or_403(db, principal, username, owner_admin_id)
    user_ops.delete_user_cascade(db, user)
    return {"ok": True}
