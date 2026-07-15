"""Shared business logic for creating/renewing/removing users & connections.

Used by both the admin-panel router (JWT auth, browser) and the external
bot router (API-key auth) so behaviour stays identical between the two."""
from __future__ import annotations

import datetime as dt
import ipaddress
import random
import re
import uuid
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from .. import models
from .mikrotik_client import MikrotikClient, MikrotikError
from .xray_client import XrayError, client_for_node
from .keys import generate_wireguard_keypair, generate_password
from .link_builder import (
    build_wireguard_config,
    build_vless_link,
    build_openvpn_config,
    build_l2tp_info,
    build_ikev2_info,
    build_sstp_info,
)

def gb_to_bytes(gb: float) -> int:
    return int(round((gb or 0) * 1024 ** 3))


# ---------------------------------------------------- referral & loyalty
_REFERRAL_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I - avoids look-alike mistakes when typed by hand


def _generate_referral_code(db: Session) -> str:
    """Short, unique, easy-to-share code for User.referral_code - retries a
    few times on the (very unlikely) chance of a collision rather than
    trusting an 8-char keyspace is collision-free forever; falls back to a
    longer uuid-derived code if it somehow still collides every time."""
    for _ in range(20):
        code = "".join(random.choice(_REFERRAL_CODE_ALPHABET) for _ in range(8))
        if not db.query(models.User).filter(models.User.referral_code == code).first():
            return code
    return uuid.uuid4().hex[:12].upper()


def _maybe_grant_loyalty_reward(db: Session, user: models.User) -> None:
    """Called right after user.purchase_count is incremented (new purchase
    or renewal - see create_user_record/renew_user/bulk_update_users) -
    grants PanelSettings.loyalty_reward_credit/_gb once per
    loyalty_purchase_threshold crossing. Comparing
    `purchase_count // threshold` against `loyalty_rewards_given` (instead
    of a plain `purchase_count % threshold == 0` check) means a jump of
    more than one threshold in a single call (e.g. a bulk admin action)
    still only grants exactly the rewards not already given, never more
    than once for the same crossing. No-ops entirely while
    loyalty_purchase_threshold is unset/0 (feature disabled by default -
    see models.PanelSettings).

    When a reward IS granted, stashes the (credit, gb) amounts on a
    transient (non-persisted) `user._loyalty_reward_just_granted` attribute
    so routers/bot.py's _user_response can surface a one-time "you just got
    a loyalty reward!" notice to the bot without needing a whole separate
    table/endpoint to track "has this reward been shown yet"."""
    settings_row = db.get(models.PanelSettings, 1)
    threshold = settings_row.loyalty_purchase_threshold if settings_row else None
    if not threshold or threshold <= 0:
        return
    due = (user.purchase_count or 0) // threshold
    already = user.loyalty_rewards_given or 0
    if due <= already:
        return
    rewards_to_grant = due - already
    credit = (settings_row.loyalty_reward_credit or 0) * rewards_to_grant
    gb = (settings_row.loyalty_reward_gb or 0) * rewards_to_grant
    if credit:
        user.balance = (user.balance or 0) + credit
    if gb:
        user.total_quota_bytes = (user.total_quota_bytes or 0) + gb_to_bytes(gb)
    user.loyalty_rewards_given = due
    if credit or gb:
        user._loyalty_reward_just_granted = (credit, gb)


# --------------------------------------------------------------------- users
def create_user_record(
    db: Session,
    username: str,
    full_name: Optional[str] = None,
    quota_gb: float = 0,
    expire_days: Optional[int] = None,
    notes: Optional[str] = None,
    telegram_id: Optional[int] = None,
    owner_admin_id: Optional[int] = None,
    package_id: Optional[int] = None,
) -> models.User:
    if db.query(models.User).filter(models.User.username == username).first():
        raise HTTPException(400, "این نام کاربری قبلا ثبت شده است")
    # NOTE: telegram_id is intentionally NOT required to be unique - a
    # single Telegram account can be linked to several panel accounts (a
    # customer who bought more than once under different usernames). See
    # routers/bot.py's list_users_by_telegram + the bot's account-picker
    # (telegram_bot/handlers/customer.py's _resolve_account) for how the
    # bot lets the customer choose which one they mean.
    expire_at = None
    if expire_days:
        expire_at = dt.datetime.utcnow() + dt.timedelta(days=expire_days)
    user = models.User(
        username=username,
        full_name=full_name,
        notes=notes,
        total_quota_bytes=gb_to_bytes(quota_gb),
        expire_at=expire_at,
        telegram_id=telegram_id,
        owner_admin_id=owner_admin_id,
        # This was missing entirely before - every user created through the
        # bot's purchase flow (routers/bot.py's create_user, i.e. almost all
        # real customers) ended up with package_id=NULL forever, silently
        # breaking the panel's "filter/select users by package" feature for
        # them even though it worked fine for users created from the web
        # panel's own create-user form (routers/users.py).
        package_id=package_id,
        referral_code=_generate_referral_code(db),
        # This user's own first purchase counts toward THEIR loyalty
        # progress too (not just subsequent renewals) - see
        # models.User.purchase_count.
        purchase_count=1,
    )
    db.add(user)
    db.flush()  # assigns user.id, still inside this same transaction
    _maybe_grant_loyalty_reward(db, user)
    db.commit()
    db.refresh(user)
    return user


def apply_referral_code(db: Session, user: models.User, referral_code: str) -> tuple[bool, str]:
    """Called once, right after a brand-new customer's account is created
    (routers/bot.py's apply_referral, itself called from
    telegram_bot/handlers/admin_pending.py right after create_user
    succeeds - see that module for why account creation is the one choke
    point this can hook into). Rewards BOTH sides per the user's chosen
    design (see PanelSettings.referral_referrer_reward_* /
    referral_new_user_reward_*), never just the referrer. Returns
    (ok, reason) - reason is a Persian message safe to show the customer
    only when ok is False; silently no-ops (still returns True) when no
    reward amounts are configured, since applying the code itself (linking
    referred_by_id for future reporting) is still valid even with rewards
    at zero."""
    code = (referral_code or "").strip().upper()
    if not code:
        return False, "کد دعوت وارد نشده است"
    if user.referred_by_id or user.referral_reward_granted:
        return False, "قبلاً یک کد دعوت برای این حساب ثبت شده است"
    referrer = (
        db.query(models.User)
        .filter(models.User.referral_code == code, models.User.id != user.id)
        .first()
    )
    if not referrer:
        return False, "کد دعوت نامعتبر است"
    settings_row = db.get(models.PanelSettings, 1)
    user.referred_by_id = referrer.id
    user.referral_reward_granted = True
    if settings_row:
        ref_credit = settings_row.referral_referrer_reward_credit or 0
        ref_gb = settings_row.referral_referrer_reward_gb or 0
        new_credit = settings_row.referral_new_user_reward_credit or 0
        new_gb = settings_row.referral_new_user_reward_gb or 0
        if ref_credit:
            referrer.balance = (referrer.balance or 0) + ref_credit
        if ref_gb:
            referrer.total_quota_bytes = (referrer.total_quota_bytes or 0) + gb_to_bytes(ref_gb)
        if new_credit:
            user.balance = (user.balance or 0) + new_credit
        if new_gb:
            user.total_quota_bytes = (user.total_quota_bytes or 0) + gb_to_bytes(new_gb)
    db.commit()
    return True, ""


# ---------------------------------------------------- discount codes
def validate_discount_code(
    db: Session, code: str, package_price: int = 0, username: Optional[str] = None
) -> tuple[bool, str, int]:
    """Checks a promo code by its human-typed text (stored upper-cased -
    see routers/discount_codes.py's create) without recording anything -
    see redeem_discount_code for the version called once an order is
    actually confirmed. Returns (valid, reason, discount_amount); reason is
    a Persian message ready to show the customer as-is when invalid,
    discount_amount is 0 when invalid. `username`, when given, also checks
    the one-redemption-per-customer-per-code rule (see
    models.DiscountCodeRedemption) - omitted for a brand-new customer whose
    account doesn't exist yet."""
    row = db.query(models.DiscountCode).filter(models.DiscountCode.code == (code or "").strip().upper()).first()
    if not row:
        return False, "کد تخفیف نامعتبر است", 0
    if not row.enabled:
        return False, "این کد تخفیف غیرفعال شده است", 0
    if row.expires_at and row.expires_at < dt.datetime.utcnow():
        return False, "این کد تخفیف منقضی شده است", 0
    if row.max_uses is not None and (row.used_count or 0) >= row.max_uses:
        return False, "ظرفیت استفاده از این کد تخفیف تمام شده است", 0
    if username:
        already = (
            db.query(models.DiscountCodeRedemption)
            .filter(models.DiscountCodeRedemption.code_id == row.id, models.DiscountCodeRedemption.username == username)
            .first()
        )
        if already:
            return False, "شما قبلاً از این کد تخفیف استفاده کرده‌اید", 0
    if row.kind == "percent":
        amount = int(round((package_price or 0) * (row.value / 100)))
    else:
        amount = int(round(row.value))
    amount = max(0, min(amount, package_price or 0))
    return True, "", amount


def redeem_discount_code(db: Session, code: str, username: str, package_price: int = 0) -> tuple[bool, str, int]:
    """Re-validates (a code can hit its cap/expire between the customer
    typing it and confirming payment) then, if still valid, atomically
    bumps DiscountCode.used_count and records a DiscountCodeRedemption row.
    Call this only once, at the point a purchase is actually confirmed -
    never from the validate-as-you-type step."""
    valid, reason, amount = validate_discount_code(db, code, package_price, username=username)
    if not valid:
        return False, reason, 0
    row = db.query(models.DiscountCode).filter(models.DiscountCode.code == code.strip().upper()).first()
    row.used_count = (row.used_count or 0) + 1
    user = db.query(models.User).filter(models.User.username == username).first()
    db.add(models.DiscountCodeRedemption(
        code_id=row.id,
        user_id=user.id if user else None,
        username=username,
        package_price=package_price,
        discount_amount=amount,
    ))
    db.commit()
    return True, "", amount


def renew_user(
    db: Session,
    user: models.User,
    add_gb: float = 0,
    add_days: int = 0,
    reset_usage: bool = False,
    package_id: Optional[int] = None,
) -> models.User:
    """Renews a user - but NOT always immediately. If the user's CURRENT
    quota and expiry both still have room left (they haven't actually used
    up what they already paid for), this renewal is queued as a "reserved"
    package instead of being applied on top - see
    User.reserved_quota_bytes's docstring and _maybe_activate_reserved_renewal
    below, which is what actually turns a reservation into a real
    quota/expiry reset once the current one genuinely runs out.

    If either dimension IS already exhausted (or the user had no
    quota/expiry limits set at all yet), this renewal applies right now as
    a full fresh reset (usage -> 0, quota/expiry -> exactly what's being
    granted) rather than stacking onto a used-up allotment - deliberately a
    RESET, not an addition, per the requested behavior."""
    now = dt.datetime.utcnow()
    is_real_renewal = bool(add_gb or add_days)

    if is_real_renewal:
        quota_ok = not user.total_quota_bytes or user.used_bytes < user.total_quota_bytes
        expiry_ok = not user.expire_at or user.expire_at > now
        has_existing_limits = bool(user.total_quota_bytes or user.expire_at)
        if has_existing_limits and quota_ok and expiry_ok:
            user.reserved_quota_bytes = (user.reserved_quota_bytes or 0) + (gb_to_bytes(add_gb) if add_gb else 0)
            user.reserved_duration_days = (user.reserved_duration_days or 0) + add_days
            if package_id is not None:
                user.reserved_package_id = package_id
            if user.reserved_created_at is None:
                user.reserved_created_at = now
            db.commit()
            db.refresh(user)
            return user
        # Current quota or expiry is already exhausted (or this user never
        # had limits set before) - apply as a clean restart right now.
        user.used_bytes = 0
        if add_gb:
            user.total_quota_bytes = gb_to_bytes(add_gb)
        if add_days:
            user.expire_at = now + dt.timedelta(days=add_days)
    elif reset_usage:
        user.used_bytes = 0

    if user.status in (models.UserStatus.quota_exceeded, models.UserStatus.expired):
        user.status = models.UserStatus.active
        # Renewing a previously cut-off user must also push the "enabled"
        # state back out to their actual connections (MikroTik peer/RADIUS
        # flag, Xray/3X-UI client) - just flipping the DB column here is not
        # enough, since quota_manager.py's own reconciliation only fires on
        # a status TRANSITION it detects itself; because we already set
        # user.status = active above, it would see no transition on the
        # next poll and never notice the connections are still
        # disabled/deleted from before. See reconcile_user_connections.
        reconcile_user_connections(db, user)
    # Same package_id gap as create_user_record above - a bot renewal from a
    # package purchase (or an admin re-tagging an existing user with a
    # package via routers/bot.py) should keep package_id in sync too, not
    # just at first creation.
    if package_id is not None:
        user.package_id = package_id
    if is_real_renewal:
        # Only a REAL renewal (actual quota/time added) counts toward
        # loyalty progress - a bare package re-tag or a reset-usage-only
        # call shouldn't silently advance it. See _maybe_grant_loyalty_reward.
        user.purchase_count = (user.purchase_count or 0) + 1
        _maybe_grant_loyalty_reward(db, user)
    db.commit()
    db.refresh(user)
    return user


def _maybe_activate_reserved_renewal(db: Session, user: models.User) -> bool:
    """Called right when a user's quota/expiry is ABOUT to be marked
    exhausted (quota_manager.py's _enforce_user_limits - the single choke
    point both the periodic poll and RADIUS accounting funnel through - and
    radius_server.py's HandleAuthPacket, for the real-time login-attempt
    check). If they have a renewal queued up (see renew_user's docstring),
    activates it right now instead: full reset using the reserved amounts,
    clearing the reservation. Returns True if something was activated, so
    the caller knows to re-evaluate exceeded/expired against the fresh
    values rather than the stale ones it just computed."""
    if not (user.reserved_quota_bytes or user.reserved_duration_days or user.reserved_package_id):
        return False
    now = dt.datetime.utcnow()
    user.used_bytes = 0
    if user.reserved_quota_bytes:
        user.total_quota_bytes = user.reserved_quota_bytes
    if user.reserved_duration_days:
        user.expire_at = now + dt.timedelta(days=user.reserved_duration_days)
    if user.reserved_package_id is not None:
        user.package_id = user.reserved_package_id
    user.reserved_quota_bytes = None
    user.reserved_duration_days = None
    user.reserved_package_id = None
    user.reserved_created_at = None
    if user.status in (models.UserStatus.quota_exceeded, models.UserStatus.expired):
        user.status = models.UserStatus.active
        # Same reconciliation-gap fix as renew_user above - this function is
        # also called directly from radius_server.py's live login check, so
        # without this the customer's PPP login would be let back in while
        # their Xray/other connections stay deleted/disabled from before.
        reconcile_user_connections(db, user)
    user.purchase_count = (user.purchase_count or 0) + 1
    _maybe_grant_loyalty_reward(db, user)
    return True


def reconcile_user_connections(db: Session, user: models.User):
    """Pushes the user's CURRENT `status` out to every real connection they
    own (MikroTik WireGuard peer, RADIUS-checked PPP flag, Xray/3X-UI
    client). Call this any time `user.status` is set directly - renewal,
    reset-usage, manual enable/disable, bulk actions, reserved-renewal
    activation - from anywhere OTHER than quota_manager.py's own
    _enforce_user_limits, which already reconciles the transitions IT
    computes itself.

    Skipping this after a direct status write is exactly what caused a
    real bug: a user's Xray client got deleted on quota exhaustion, and
    renewing them only flipped `user.status` back to "active" in the DB -
    the next poll cycle's _enforce_user_limits saw target_status already
    equal to user.status (no transition) and silently never recreated the
    Xray client, so the "renewed" user still had no working V2Ray account.

    Lazy import to dodge a circular import: quota_manager.py already
    imports from this module (user_ops.py) at the top of the file.

    Connections linked to an independent Purchase (see
    models.Purchase/Connection.purchase_id) need special handling: a manual
    account-wide DISABLE is an absolute override and force-disables them
    too, but a manual account-wide ENABLE must NOT blindly force them back
    on - that purchase might be separately quota_exceeded/expired on its
    own, and re-enabling the whole account shouldn't silently undo that
    independent state. So on enable, a purchase-linked connection only
    actually turns on if its OWN purchase is currently "active"."""
    from .quota_manager import _set_connection_enabled
    user_enabled = user.status == models.UserStatus.active
    for conn in user.connections:
        if conn.purchase_id and user_enabled:
            enabled = conn.purchase.status == models.UserStatus.active
        else:
            enabled = user_enabled
        _set_connection_enabled(db, conn, enabled=enabled)


def delete_user_cascade(db: Session, user: models.User):
    for conn in list(user.connections):
        deprovision_connection(conn)
    db.delete(user)
    db.commit()


def bulk_delete_users(db: Session, user_ids: list[int], owner_admin_id: Optional[int] = None) -> dict:
    """owner_admin_id, when given (i.e. the caller is a non-superadmin - see
    routers/users.py), restricts this to only ids that admin actually owns -
    anything else is silently skipped, same as a plain missing id, so a
    non-superadmin can never delete another group's users by guessing ids."""
    deleted_count = 0
    for uid in user_ids:
        user = db.get(models.User, uid)
        if not user:
            continue
        if owner_admin_id is not None and user.owner_admin_id != owner_admin_id:
            continue
        delete_user_cascade(db, user)
        deleted_count += 1
    return {"deleted_count": deleted_count}


# ------------------------------------------------------------------- bulk ops
def bulk_create_users(
    db: Session,
    prefix: str,
    count: int,
    package_id: Optional[int] = None,
    quota_gb: float = 0,
    expire_days: Optional[int] = None,
    notes: Optional[str] = None,
    connections: Optional[list] = None,
    owner_admin_id: Optional[int] = None,
) -> dict:
    """Creates up to `count` users named prefix+1, prefix+2, ... prefix+N,
    each with the same quota/expiry, optionally provisioning the same set
    of connections (node+protocol) for every one of them. Numbers already
    taken (existing username) are skipped rather than overwritten, and
    numbering keeps going past them so you still end up with `count` new
    users when possible.

    If package_id is given, every user is built from that package instead
    (quota/duration/max-concurrent-sessions/services all come from the
    package, same as a single "ساخت با پکیج" - see routers/users.py's
    create_user) and quota_gb/expire_days/connections are ignored."""
    if count <= 0:
        raise HTTPException(400, "تعداد باید بزرگتر از صفر باشد")
    if count > 1000:
        raise HTTPException(400, "حداکثر ۱۰۰۰ کاربر در هر بار")

    package = None
    if package_id:
        package = db.get(models.Package, package_id)
        if not package:
            raise HTTPException(400, "پکیج پیدا نشد")

    created: list[str] = []
    skipped: list[dict] = []
    connections = connections or []

    i = 1
    attempts = 0
    max_attempts = count * 5 + 20  # safety cap in case of heavy collisions
    while len(created) < count and attempts < max_attempts:
        attempts += 1
        username = f"{prefix}{i}"
        i += 1
        if db.query(models.User).filter(models.User.username == username).first():
            skipped.append({"name": username, "reason": "این نام کاربری قبلا وجود دارد"})
            continue

        if package:
            user = create_user_record(db, username, notes=notes)
            user.total_quota_bytes = gb_to_bytes(package.quota_gb) if package.quota_gb else 0
            user.expire_at = (
                dt.datetime.utcnow() + dt.timedelta(days=package.duration_days) if package.duration_days else None
            )
            user.max_concurrent_sessions = package.max_concurrent_sessions
            user.owner_admin_id = owner_admin_id
            user.package_id = package.id
            db.commit()
            db.refresh(user)
            result = provision_package_connections(db, user, package)
            for s in result["skipped"]:
                skipped.append({"name": f"{username} (اتصال)", "reason": s["reason"]})
        else:
            user = create_user_record(db, username, quota_gb=quota_gb, expire_days=expire_days, notes=notes)
            user.owner_admin_id = owner_admin_id
            db.commit()
            # Every service picked in this bulk-create form is one bundle
            # for this user, same idea as a package purchase - share one
            # batch so they group together in the bot's "اکانت من" screen.
            batch = uuid.uuid4().hex if connections else None
            for spec in connections:
                node = db.get(models.Node, spec.node_id)
                if not node:
                    skipped.append({"name": username, "reason": f"نود {spec.node_id} پیدا نشد (کاربر ساخته شد، اتصال ساخته نشد)"})
                    continue
                try:
                    provision_connection(
                        db, user, node, spec.protocol,
                        max_concurrent_sessions=getattr(spec, "max_concurrent_sessions", 1),
                        purchase_batch=batch,
                    )
                except HTTPException as exc:
                    skipped.append({"name": f"{username} (اتصال)", "reason": str(exc.detail)})
        created.append(username)

    return {
        "created": created,
        "created_count": len(created),
        "skipped": skipped,
        "skipped_count": len(skipped),
    }


def bulk_update_users(
    db: Session,
    user_ids: list,
    add_gb: float = 0,
    add_days: int = 0,
    reset_usage: bool = False,
    status: Optional[models.UserStatus] = None,
    max_concurrent_sessions: Optional[int] = None,
    package: Optional[models.Package] = None,
    owner_admin_id: Optional[int] = None,
) -> dict:
    """Applies the same renewal/status/limit change to every user in
    user_ids. Silently skips ids that don't exist - and, when
    owner_admin_id is given (non-superadmin caller), ids belonging to a
    different admin's group too.

    If `package` is given, it takes priority over add_gb/add_days: each
    user's quota/expiry/concurrent-session-cap is overwritten outright from
    the package (same values a fresh "ساخت با پکیج" would set) and their
    package_id is stamped to match, so this doubles as a group "renew with
    package" and as a way to (re)tag existing users with a package for
    future package-based filtering/selection. Does not touch the package's
    own connections/services - only the user's quota/expiry/cap fields."""
    updated = 0
    for uid in user_ids:
        user = db.get(models.User, uid)
        if not user:
            continue
        if owner_admin_id is not None and user.owner_admin_id != owner_admin_id:
            continue
        if package is not None:
            user.total_quota_bytes = gb_to_bytes(package.quota_gb) if package.quota_gb else 0
            user.expire_at = (
                dt.datetime.utcnow() + dt.timedelta(days=package.duration_days) if package.duration_days else None
            )
            user.expire_days_after_first_use = None
            user.max_concurrent_sessions = package.max_concurrent_sessions
            user.package_id = package.id
            if reset_usage:
                user.used_bytes = 0
            if user.status in (models.UserStatus.quota_exceeded, models.UserStatus.expired):
                user.status = models.UserStatus.active
                reconcile_user_connections(db, user)
            # This bypasses renew_user() (it overwrites quota/expiry outright
            # from the package instead of adding to it) so it needs its own
            # loyalty-progress bump - same rule as renew_user: a real
            # renewal-by-package counts.
            user.purchase_count = (user.purchase_count or 0) + 1
            _maybe_grant_loyalty_reward(db, user)
        elif add_gb or add_days or reset_usage:
            renew_user(db, user, add_gb=add_gb, add_days=add_days, reset_usage=reset_usage)
        if status is not None and status != user.status:
            user.status = status
            # Explicit bulk enable/disable (e.g. "غیرفعال‌سازی گروهی") - push
            # it out to the real connections too, both directions.
            reconcile_user_connections(db, user)
        if max_concurrent_sessions is not None and package is None:
            # combined cap across all of the user's connections together -
            # see models.User.max_concurrent_sessions (skipped when a
            # package was applied above - its own cap already won)
            user.max_concurrent_sessions = max_concurrent_sessions
        updated += 1
    db.commit()
    return {"updated_count": updated}


# ------------------------------------------------------------------ wireguard
# Auto-expand cap: never widen a node's WireGuard client subnet automatically
# past a /16 (65 534 usable hosts) - if that's not enough, an admin needs to
# step in manually rather than the panel silently growing it forever.
_WG_SUBNET_AUTO_EXPAND_MIN_PREFIXLEN = 16


def _wg_used_ips(node: models.Node, db: Session) -> set:
    """Every client IP already claimed on this node's WireGuard interface.
    A single Connection can reserve more than one address at once (see
    provision_wireguard's `count` - a shared/multi-user peer keeps ONE
    config but several adjacent IPs, comma-joined in wg_client_address so
    each concurrently-connecting device gets a distinct source IP)."""
    used = set()
    for conn in db.query(models.Connection).filter(
        models.Connection.node_id == node.id,
        models.Connection.type == models.ConnectionType.wireguard,
    ):
        if conn.wg_client_address:
            for part in conn.wg_client_address.split(","):
                ip = part.strip().split("/")[0]
                if ip:
                    used.add(ip)
    return used


def _wg_find_free_run(subnet, gateway, used: set, count: int) -> Optional[list]:
    """First run of `count` FREE, CONSECUTIVE host addresses in `subnet`
    (skipping the gateway), or None if no such run exists."""
    run: list = []
    for host in subnet.hosts():
        if host == gateway or str(host) in used:
            run = []
            continue
        run.append(host)
        if len(run) == count:
            return run
    return None


def _wg_reserve_ips(node: models.Node, db: Session, count: int = 1) -> tuple[str, list, bool]:
    """Finds `count` free, adjacent host addresses on node.mt_client_subnet.
    If the current subnet has no such run left (pool exhausted), it is
    auto-expanded (doubled) and persisted on the node instead of rejecting
    the new connection - this is the admin's own explicit choice for task
    #226 ("ساب‌نت رو خودکار بزرگ‌تر کن"). Returns
    (gateway_with_prefix, [ip_str, ...], subnet_was_expanded)."""
    subnet = ipaddress.ip_network(node.mt_client_subnet or "10.66.66.0/24")
    expanded = False

    run = _wg_find_free_run(subnet, subnet.network_address + 1, _wg_used_ips(node, db), count)
    while run is None:
        new_subnet = subnet.supernet(prefixlen_diff=1)
        if new_subnet.prefixlen < _WG_SUBNET_AUTO_EXPAND_MIN_PREFIXLEN:
            raise HTTPException(
                400,
                "ظرفیت آدرس‌های WireGuard این نود تمام شده و امکان بزرگ‌تر کردن خودکار بیشتر وجود ندارد",
            )
        subnet = new_subnet
        node.mt_client_subnet = str(subnet)
        db.commit()
        db.refresh(node)
        expanded = True
        run = _wg_find_free_run(subnet, subnet.network_address + 1, _wg_used_ips(node, db), count)

    gateway_with_prefix = f"{subnet.network_address + 1}/{subnet.prefixlen}"
    return gateway_with_prefix, [str(h) for h in run], expanded


def provision_wireguard(
    db: Session,
    user: models.User,
    node: models.Node,
    purchase_batch: Optional[str] = None,
    package_name: Optional[str] = None,
    max_concurrent_sessions: Optional[int] = 1,
) -> models.Connection:
    """max_concurrent_sessions > 1 means this ONE peer/config is meant to be
    shared by several people/devices at once (see routers/users.py and the
    admin's explicit choice for task #227: "فقط رزرو N آی‌پی کنار هم بدون
    ساخت کانفیگ جدا") - reserves that many adjacent free IPs under a single
    WireGuard key instead of creating N separate peers. All reserved IPs are
    comma-joined into wg_client_address and comma-joined into the peer's
    RouterOS allowed-address list, and link_builder.build_wireguard_config
    already emits them as a comma-separated wg-quick `Address =` line
    unchanged - see that module."""
    if node.type != models.NodeType.mikrotik:
        raise HTTPException(400, "نود میکروتیک معتبر نیست")

    count = max(1, max_concurrent_sessions or 1)
    gateway_with_prefix, client_ips, subnet_expanded = _wg_reserve_ips(node, db, count)
    client_address = ",".join(f"{ip}/32" for ip in client_ips)
    private_key, public_key = generate_wireguard_keypair()
    peer_name = f"user-{user.username}-{uuid.uuid4().hex[:6]}"

    try:
        with MikrotikClient.for_node(node) as mt:
            mt.ensure_wireguard_interface(node.mt_wireguard_interface, node.mt_endpoint_port or 13231)
            if subnet_expanded:
                # The gateway's prefix length just grew (e.g. /24 -> /23) -
                # ensure_interface_address would skip since an address
                # already exists, so force-replace it to keep routing/NAT
                # consistent with the wider pool.
                mt.set_interface_address(node.mt_wireguard_interface, gateway_with_prefix)
            else:
                mt.ensure_interface_address(node.mt_wireguard_interface, gateway_with_prefix)
            mt.add_peer(
                node.mt_wireguard_interface,
                public_key=public_key,
                allowed_address=client_address,
                comment=peer_name,
            )
    except MikrotikError as exc:
        raise HTTPException(400, str(exc))

    conn = models.Connection(
        user_id=user.id,
        node_id=node.id,
        type=models.ConnectionType.wireguard,
        wg_peer_name=peer_name,
        wg_public_key=public_key,
        wg_private_key=private_key,
        wg_client_address=client_address,
        max_concurrent_sessions=count,
        purchase_batch=purchase_batch,
        package_name_snapshot=package_name,
    )
    db.add(conn)
    db.commit()
    db.refresh(conn)
    return conn


# ------------------------------------------------------------- openvpn/l2tp
def _provision_ppp(
    db: Session,
    user: models.User,
    node: models.Node,
    service: str,
    max_concurrent_sessions: Optional[int] = 1,
    purchase_batch: Optional[str] = None,
    package_name: Optional[str] = None,
) -> models.Connection:
    """Creates only a username/password pair, stored in the panel's own
    database. Authentication now happens via RADIUS (this panel runs its own
    RADIUS server) instead of a local PPP secret on the router, so nothing
    needs to be pushed to the router here at all. The IP pool, the OpenVPN/
    L2TP server itself, certificates and IPsec are all expected to already
    be configured on the router by the admin - the panel does not touch any
    of that, by design. (The router only needs its /radius client entry
    pointed at this panel - see the "push RADIUS config" node action.)

    max_concurrent_sessions caps how many simultaneous RADIUS sessions this
    credential may have open at once (enforced by the RADIUS auth handler);
    0/None means unlimited."""
    if node.type != models.NodeType.mikrotik:
        raise HTTPException(400, "نود میکروتیک معتبر نیست")

    username = f"{user.username}-{service}-{uuid.uuid4().hex[:5]}"
    password = generate_password()

    conn_type = {
        "ovpn": models.ConnectionType.openvpn,
        "l2tp": models.ConnectionType.l2tp,
        "ikev2": models.ConnectionType.ikev2,
        "sstp": models.ConnectionType.sstp,
    }[service]
    conn = models.Connection(
        user_id=user.id,
        node_id=node.id,
        type=conn_type,
        ppp_username=username,
        ppp_password=password,
        max_concurrent_sessions=max_concurrent_sessions if max_concurrent_sessions is not None else 1,
        purchase_batch=purchase_batch,
        package_name_snapshot=package_name,
    )
    db.add(conn)
    db.commit()
    db.refresh(conn)
    return conn


def provision_openvpn(
    db: Session,
    user: models.User,
    node: models.Node,
    max_concurrent_sessions: Optional[int] = 1,
    purchase_batch: Optional[str] = None,
    package_name: Optional[str] = None,
) -> models.Connection:
    return _provision_ppp(db, user, node, "ovpn", max_concurrent_sessions, purchase_batch, package_name)


def provision_l2tp(
    db: Session,
    user: models.User,
    node: models.Node,
    max_concurrent_sessions: Optional[int] = 1,
    purchase_batch: Optional[str] = None,
    package_name: Optional[str] = None,
) -> models.Connection:
    return _provision_ppp(db, user, node, "l2tp", max_concurrent_sessions, purchase_batch, package_name)


def provision_ikev2(
    db: Session,
    user: models.User,
    node: models.Node,
    max_concurrent_sessions: Optional[int] = 1,
    purchase_batch: Optional[str] = None,
    package_name: Optional[str] = None,
) -> models.Connection:
    return _provision_ppp(db, user, node, "ikev2", max_concurrent_sessions, purchase_batch, package_name)


def provision_sstp(
    db: Session,
    user: models.User,
    node: models.Node,
    max_concurrent_sessions: Optional[int] = 1,
    purchase_batch: Optional[str] = None,
    package_name: Optional[str] = None,
) -> models.Connection:
    return _provision_ppp(db, user, node, "sstp", max_concurrent_sessions, purchase_batch, package_name)


# ---------------------------------------------------------------------- xray
def provision_xray(
    db: Session,
    user: models.User,
    node: models.Node,
    flow: str = "",
    purchase_batch: Optional[str] = None,
    package_name: Optional[str] = None,
) -> models.Connection:
    if node.type != models.NodeType.xray:
        raise HTTPException(400, "نود Xray معتبر نیست")

    email = f"{user.username}-{uuid.uuid4().hex[:6]}@usermanager.local"
    try:
        with client_for_node(node) as xc:
            client_uuid = xc.add_client(node.xr_inbound_tag, email, flow=flow or "")
    except XrayError as exc:
        raise HTTPException(400, str(exc))

    conn = models.Connection(
        user_id=user.id,
        node_id=node.id,
        type=models.ConnectionType.xray,
        xr_uuid=client_uuid,
        xr_email=email,
        xr_flow=flow or "",
        purchase_batch=purchase_batch,
        package_name_snapshot=package_name,
    )
    db.add(conn)
    db.commit()
    db.refresh(conn)
    return conn


def provision_package_connections(db: Session, user: models.User, package: models.Package) -> dict:
    """Provisions every server/service bundled into a package for this
    user in one go - used when a user is created "with a package" from the
    web panel (and the sales bot) instead of picking a node/protocol by
    hand. All connections created here share ONE auto-generated
    purchase_batch (see models.Connection.purchase_batch) and the package's
    current name as a display snapshot, so they show up as a single grouped
    "purchase" in the bot's "اکانت من" screen instead of a flat list."""
    created: list[models.Connection] = []
    skipped: list[dict] = []
    batch = uuid.uuid4().hex
    for pc in package.connections:
        node = db.get(models.Node, pc.node_id)
        if not node:
            skipped.append({"node_id": pc.node_id, "reason": "نود پیدا نشد"})
            continue
        try:
            # per-connection max_concurrent_sessions is just a fallback now
            # (see models.User.max_concurrent_sessions) - default to 1;
            # create_user_record/routers/users.py sets the real combined
            # cap from package.max_concurrent_sessions on the user itself.
            conn = provision_connection(
                db, user, node, pc.protocol, pc.flow or "", 1,
                purchase_batch=batch, package_name=package.name,
            )
            created.append(conn)
        except HTTPException as exc:
            skipped.append({"node_id": pc.node_id, "reason": str(exc.detail)})
    return {"created": created, "skipped": skipped}


def apply_package_as_purchase(db: Session, user: models.User, package: models.Package) -> models.Purchase:
    """The real, independently-enforced counterpart to
    provision_package_connections above - used by routers/users.py's
    apply_package endpoint (the "افزودن پکیج" admin action, for giving an
    EXISTING user an extra package on top of whatever they already have).

    Before this existed, that action only ever created connections and left
    the user's own combined total_quota_bytes/used_bytes/expire_at
    untouched - which meant the new package's own quota/duration was never
    actually enforced anywhere: a user already on an unlimited plan (or one
    whose combined quota simply had room left) could blow straight past the
    new package's own limit with nothing to stop it (the exact bug this
    fixes). Every connection created here is linked via Connection.purchase_id
    to a brand-new Purchase row with its OWN quota_bytes/used_bytes/expire_at
    - see models.Purchase's docstring and services/quota_manager.py's
    _enforce_purchase_limits for how that gets enforced independently of the
    user's own fields."""
    purchase = models.Purchase(
        user_id=user.id,
        package_id=package.id,
        package_name_snapshot=package.name,
        quota_bytes=gb_to_bytes(package.quota_gb) if package.quota_gb else 0,
        expire_at=(
            dt.datetime.utcnow() + dt.timedelta(days=package.duration_days) if package.duration_days else None
        ),
        max_concurrent_sessions=package.max_concurrent_sessions,
        status=models.UserStatus.active,
    )
    db.add(purchase)
    db.flush()  # assigns purchase.id inside this same transaction

    # Keeps the existing purchase_batch string grouping too (still what the
    # bot's "اکانت من" screen and UserDetail.jsx's display grouping key off
    # of) - purchase_id is the NEW, separate thing that actually carries
    # quota semantics.
    batch = uuid.uuid4().hex
    for pc in package.connections:
        node = db.get(models.Node, pc.node_id)
        if not node:
            continue
        try:
            conn = provision_connection(
                db, user, node, pc.protocol, pc.flow or "", 1,
                purchase_batch=batch, package_name=package.name,
            )
            conn.purchase_id = purchase.id
        except HTTPException:
            continue

    user.purchase_count = (user.purchase_count or 0) + 1
    _maybe_grant_loyalty_reward(db, user)
    db.commit()
    db.refresh(purchase)
    return purchase


def renew_purchase(
    db: Session,
    purchase: models.Purchase,
    add_gb: float = 0,
    add_days: int = 0,
    reset_usage: bool = False,
    package_id: Optional[int] = None,
) -> models.Purchase:
    """Same reservation-queue renewal behavior as renew_user above (see its
    docstring), but scoped to just ONE independent Purchase instead of the
    user's combined fields - lets an admin renew/reset a single package a
    user bought without touching anything else they have."""
    now = dt.datetime.utcnow()
    is_real_renewal = bool(add_gb or add_days)

    if is_real_renewal:
        quota_ok = not purchase.quota_bytes or purchase.used_bytes < purchase.quota_bytes
        expiry_ok = not purchase.expire_at or purchase.expire_at > now
        has_existing_limits = bool(purchase.quota_bytes or purchase.expire_at)
        if has_existing_limits and quota_ok and expiry_ok:
            purchase.reserved_quota_bytes = (purchase.reserved_quota_bytes or 0) + (gb_to_bytes(add_gb) if add_gb else 0)
            purchase.reserved_duration_days = (purchase.reserved_duration_days or 0) + add_days
            if package_id is not None:
                purchase.reserved_package_id = package_id
            if purchase.reserved_created_at is None:
                purchase.reserved_created_at = now
            db.commit()
            db.refresh(purchase)
            return purchase
        purchase.used_bytes = 0
        if add_gb:
            purchase.quota_bytes = gb_to_bytes(add_gb)
        if add_days:
            purchase.expire_at = now + dt.timedelta(days=add_days)
    elif reset_usage:
        purchase.used_bytes = 0

    if purchase.status in (models.UserStatus.quota_exceeded, models.UserStatus.expired):
        purchase.status = models.UserStatus.active
        # Same reconciliation-gap fix as reconcile_user_connections above -
        # without this, quota_manager.py's own change-detection would see no
        # transition on the next poll (since purchase.status is already
        # "active" by the time it looks) and never re-enable this purchase's
        # connections.
        reconcile_purchase_connections(db, purchase)
    if package_id is not None:
        purchase.package_id = package_id
    db.commit()
    db.refresh(purchase)
    return purchase


def _maybe_activate_reserved_purchase_renewal(db: Session, purchase: models.Purchase) -> bool:
    """Per-purchase counterpart to _maybe_activate_reserved_renewal above -
    called from quota_manager.py's _enforce_purchase_limits and
    radius_server.py's live login check when THIS purchase (not the user
    overall) is about to be marked exhausted."""
    if not (purchase.reserved_quota_bytes or purchase.reserved_duration_days or purchase.reserved_package_id):
        return False
    now = dt.datetime.utcnow()
    purchase.used_bytes = 0
    if purchase.reserved_quota_bytes:
        purchase.quota_bytes = purchase.reserved_quota_bytes
    if purchase.reserved_duration_days:
        purchase.expire_at = now + dt.timedelta(days=purchase.reserved_duration_days)
    if purchase.reserved_package_id is not None:
        purchase.package_id = purchase.reserved_package_id
    purchase.reserved_quota_bytes = None
    purchase.reserved_duration_days = None
    purchase.reserved_package_id = None
    purchase.reserved_created_at = None
    if purchase.status in (models.UserStatus.quota_exceeded, models.UserStatus.expired):
        purchase.status = models.UserStatus.active
        reconcile_purchase_connections(db, purchase)
    return True


def reconcile_purchase_connections(db: Session, purchase: models.Purchase):
    """Per-purchase counterpart to reconcile_user_connections above - pushes
    JUST this purchase's connections' enabled-state out to the real nodes,
    without touching any of the user's other (legacy or other-purchase)
    connections."""
    from .quota_manager import _set_connection_enabled
    enabled = purchase.status == models.UserStatus.active
    for conn in purchase.connections:
        _set_connection_enabled(db, conn, enabled=enabled)


def provision_connection(
    db: Session,
    user: models.User,
    node: models.Node,
    protocol: models.ConnectionType,
    flow: str = "",
    max_concurrent_sessions: Optional[int] = 1,
    purchase_batch: Optional[str] = None,
    package_name: Optional[str] = None,
) -> models.Connection:
    """Generic dispatcher used by the bot API, where the protocol is picked
    dynamically per request."""
    if protocol == models.ConnectionType.wireguard:
        return provision_wireguard(db, user, node, purchase_batch, package_name, max_concurrent_sessions)
    if protocol == models.ConnectionType.openvpn:
        return provision_openvpn(db, user, node, max_concurrent_sessions, purchase_batch, package_name)
    if protocol == models.ConnectionType.l2tp:
        return provision_l2tp(db, user, node, max_concurrent_sessions, purchase_batch, package_name)
    if protocol == models.ConnectionType.ikev2:
        return provision_ikev2(db, user, node, max_concurrent_sessions, purchase_batch, package_name)
    if protocol == models.ConnectionType.sstp:
        return provision_sstp(db, user, node, max_concurrent_sessions, purchase_batch, package_name)
    if protocol == models.ConnectionType.xray:
        return provision_xray(db, user, node, flow, purchase_batch, package_name)
    raise HTTPException(400, "پروتکل نامعتبر است")


# ------------------------------------------------------------- deprovisioning
def deprovision_connection(connection: models.Connection):
    """Removes the connection from the remote node (MikroTik/Xray). Does
    NOT touch the database row - callers are expected to db.delete() after."""
    node = connection.node
    try:
        if connection.type == models.ConnectionType.wireguard:
            with MikrotikClient.for_node(node) as mt:
                peers = mt.list_peers(node.mt_wireguard_interface)
                match = next((p for p in peers if p.get("comment") == connection.wg_peer_name), None)
                if match:
                    mt.remove_peer(match[".id"])
        elif connection.type in (models.ConnectionType.openvpn, models.ConnectionType.l2tp, models.ConnectionType.ikev2, models.ConnectionType.sstp):
            # Authenticated via RADIUS against this panel's own database -
            # there is no remote PPP secret to remove, deleting the DB row
            # (done by the caller) is all that's needed.
            pass
        elif connection.type == models.ConnectionType.xray:
            with client_for_node(node) as xc:
                xc.remove_client(node.xr_inbound_tag, connection.xr_email, connection.xr_uuid)
    except (MikrotikError, XrayError) as exc:
        raise HTTPException(400, str(exc))


def delete_connection(db: Session, connection: models.Connection):
    deprovision_connection(connection)
    db.delete(connection)
    db.commit()


# -------------------------------------------------------- import PPP secrets
_PPP_SERVICE_TO_CONN_TYPE = {
    "ovpn": models.ConnectionType.openvpn,
    "l2tp": models.ConnectionType.l2tp,
    "sstp": models.ConnectionType.sstp,
}


def _secret_is_disabled(value) -> bool:
    return value in (True, "true", "yes")


def import_ppp_secrets(db: Session, node: models.Node) -> dict:
    """Reads /ppp/secret entries that were created directly on the router
    (outside the panel, before or alongside it) and creates matching
    User+Connection rows here so they show up in the panel and can be
    managed/quota-tracked going forward. Purely additive and read-only on
    the router side: nothing is changed or removed on the router, and any
    username that already exists as a panel connection is skipped rather
    than overwritten.

    Only 'ovpn', 'l2tp' and 'sstp' service secrets are imported (the only PPP
    services this panel understands); other services (pppoe, pptp, async,
    ...) are reported as skipped. Secrets with no password (e.g. already
    RADIUS-only) are skipped too since there's nothing to copy."""
    if node.type != models.NodeType.mikrotik:
        raise HTTPException(400, "نود میکروتیک معتبر نیست")

    try:
        with MikrotikClient.for_node(node) as mt:
            secrets_ = mt.read_ppp_secrets()
    except MikrotikError as exc:
        raise HTTPException(400, str(exc))

    imported: list[str] = []
    skipped: list[dict] = []

    for secret in secrets_:
        name = secret.get("name")
        service = secret.get("service")
        password = secret.get("password")

        if not name:
            continue

        conn_type = _PPP_SERVICE_TO_CONN_TYPE.get(service)
        if conn_type is None:
            skipped.append({"name": name, "reason": f"سرویس پشتیبانی‌نشده ({service})"})
            continue

        if not password:
            skipped.append({"name": name, "reason": "این PPP secret پسورد ندارد (قابل کپی به RADIUS نیست)"})
            continue

        existing_conn = (
            db.query(models.Connection)
            .filter(models.Connection.ppp_username == name)
            .first()
        )
        if existing_conn:
            skipped.append({"name": name, "reason": "قبلا ایمپورت شده"})
            continue

        user = db.query(models.User).filter(models.User.username == name).first()
        if not user:
            user = models.User(
                username=name,
                notes="ایمپورت‌شده خودکار از PPP secret میکروتیک",
            )
            db.add(user)
            db.flush()

        conn = models.Connection(
            user_id=user.id,
            node_id=node.id,
            type=conn_type,
            enabled=not _secret_is_disabled(secret.get("disabled")),
            ppp_username=name,
            ppp_password=password,
            # /ppp/secret has no concept of a simultaneous-session limit -
            # default to unlimited rather than silently restricting an
            # already-working customer to a single connection.
            max_concurrent_sessions=0,
        )
        db.add(conn)
        imported.append(name)

    db.commit()
    return {
        "imported": imported,
        "imported_count": len(imported),
        "skipped": skipped,
        "skipped_count": len(skipped),
    }


# -------------------------------------------------- import User Manager accounts
_MT_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _parse_mikrotik_datetime(value) -> Optional[dt.datetime]:
    """RouterOS returns datetimes like 'jul/07/2026 10:00:00'."""
    if not value or not isinstance(value, str):
        return None
    value = value.strip()
    m = re.match(r"^([a-zA-Z]{3})/(\d{1,2})/(\d{4})\s+(\d{1,2}):(\d{2}):(\d{2})$", value)
    if m:
        month = _MT_MONTHS.get(m.group(1).lower())
        if month:
            try:
                return dt.datetime(int(m.group(3)), month, int(m.group(2)), int(m.group(4)), int(m.group(5)), int(m.group(6)))
            except ValueError:
                return None
    try:
        return dt.datetime.fromisoformat(value)
    except ValueError:
        return None


_MT_DURATION_TOKEN_RE = re.compile(r"(\d+)([wdhms])")
_MT_DURATION_CLOCK_SUFFIX_RE = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})$")
_MT_DURATION_UNIT_SECONDS = {"w": 7 * 86400, "d": 86400, "h": 3600, "m": 60, "s": 1}


def _parse_mikrotik_duration_days(value) -> Optional[int]:
    """Parses RouterOS 'time' values (a Profile's 'validity' field) into
    whole days, rounded UP so an imported user never ends up with less than
    what was actually configured (e.g. a sub-day validity becomes 1 day,
    not 0). Confirmed against a live router that RouterOS prints these as
    compact tokens like '4w2d', '1h', '1d', '30m' (NOT a fixed 'w/d' +
    'HH:MM:SS' layout) - but a trailing clock-style 'HH:MM:SS' suffix (seen
    on some other RouterOS 'time' fields, e.g. '4w2d03:00:00') is also
    handled just in case. Returns None for missing/'unlimited'/unparseable/
    zero values."""
    if not value or not isinstance(value, str):
        return None
    value = value.strip().lower()
    if value in ("unlimited", ""):
        return None
    total_seconds = 0
    clock = _MT_DURATION_CLOCK_SUFFIX_RE.search(value)
    if clock:
        h, m, s = (int(g) for g in clock.groups())
        total_seconds += h * 3600 + m * 60 + s
        value = value[: clock.start()]
    for amount, unit in _MT_DURATION_TOKEN_RE.findall(value):
        total_seconds += int(amount) * _MT_DURATION_UNIT_SECONDS[unit]
    if total_seconds <= 0:
        return None
    return max(1, -(-total_seconds // 86400))  # ceil to whole days


def _parse_shared_users(value) -> int:
    """RouterOS User Manager's 'shared-users' is an integer or the literal
    string 'unlimited'. The panel represents unlimited as 0."""
    if value is None:
        return 1
    if isinstance(value, str) and value.strip().lower() == "unlimited":
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 1


def import_usermanager_accounts(db: Session, node: models.Node) -> dict:
    """Reads accounts from MikroTik's own built-in User Manager
    (/user-manager/...), a separate RADIUS user database many admins already
    use - with its own quotas, expiry dates, and simultaneous-session
    limits - independently of /ppp/secret and independently of this panel.

    Unlike /ppp/secret, a User Manager account is NOT tied to a single
    service: the same username/password authenticates regardless of whether
    the client connects via OpenVPN, L2TP, PPPoE, etc., because User Manager
    itself has no "service" field. This creates one User + one Connection
    (stored as type=openvpn, but the same credentials also work for L2TP
    logins through this panel's RADIUS server, since its lookup doesn't
    discriminate by protocol either) per User Manager account:

    - total_quota_bytes is taken from the sum of download-limit/upload-limit
      (or transfer-limit if set) of all Limitations linked to the user's
      currently active/running Profile, via profile-limitation. 0 if none.
    - used_bytes is seeded from RouterOS's own persistent per-user lifetime
      counter, read via the "/user-manager/user monitor" command (NOT by
      summing /user-manager/session, which only retains a rolling window of
      recent sessions and badly undercounts anyone who has reconnected a
      few times - confirmed on a live router: monitor's total-download/
      total-upload matches exactly what Winbox's own User Manager > Users
      view shows, while summing /session came out ~100x too low). NOTE: if
      a user's real historical usage already exceeds the quota computed
      above, they will show as quota_exceeded (and get disabled) on the
      very next poll - check the numbers after import before relying on
      this for active customers.
    - expire_at is taken from the active Profile assignment's end-time
      (absent if the profile has no expiry / is not currently running).
    - max_concurrent_sessions is copied directly from the account's
      "shared-users" value (0 = unlimited), and is enforced going forward
      by this panel's own RADIUS server the same way User Manager enforced
      it (new connection attempts beyond the limit are rejected).

    Purely read-only on the router - nothing is changed there."""
    if node.type != models.NodeType.mikrotik:
        raise HTTPException(400, "نود میکروتیک معتبر نیست")

    try:
        with MikrotikClient.for_node(node) as mt:
            um_users = mt.read_um_users()
            user_profiles = mt.read_um_user_profiles()
            profiles = mt.read_um_profiles()
            profile_limitations = mt.read_um_profile_limitations()
            limitations = mt.read_um_limitations()
            usage_by_id = mt.read_um_usage([u.get(".id") for u in um_users if u.get(".id")])
    except MikrotikError as exc:
        raise HTTPException(400, str(exc))

    # profile -> validity duration in days, ONLY for profiles configured as
    # "starts-when: first-auth" (RouterOS: validity counts from the user's
    # first successful login, not from when the profile was assigned). Used
    # below as a fallback for accounts whose user-profile assignment has no
    # end-time yet because that first login hasn't happened - see
    # MikrotikClient.read_um_user_profiles' docstring for why that happens.
    profile_first_use_days: dict[str, int] = {}
    for p in profiles:
        name = p.get("name")
        if not name:
            continue
        if (p.get("starts-when") or "").strip().lower() != "first-auth":
            continue
        days = _parse_mikrotik_duration_days(p.get("validity"))
        if days:
            profile_first_use_days[str(name)] = days

    # RouterOS sometimes returns these name/reference fields as ints (e.g. a
    # limitation literally named "100") instead of strings - normalize both
    # sides of every join to str() so a type mismatch never silently breaks
    # the lookup.
    limitation_by_name = {str(lim.get("name")): lim for lim in limitations if lim.get("name") is not None}

    # profile -> combined byte quota (sum of linked limitations; 0 = unlimited)
    profile_quota_bytes: dict[str, int] = {}
    for pl in profile_limitations:
        profile = pl.get("profile")
        lim = limitation_by_name.get(str(pl.get("limitation"))) if pl.get("limitation") is not None else None
        if not profile or not lim:
            continue
        try:
            transfer = int(lim.get("transfer-limit") or 0)
        except (TypeError, ValueError):
            transfer = 0
        if not transfer:
            try:
                transfer = int(lim.get("download-limit") or 0) + int(lim.get("upload-limit") or 0)
            except (TypeError, ValueError):
                transfer = 0
        if transfer:
            profile_quota_bytes[profile] = profile_quota_bytes.get(profile, 0) + transfer

    # user -> quota / expiry, from every profile assignment that hasn't
    # already been fully consumed. NOTE: on a live router this has been seen
    # returning "running", "running-active" (hyphenated, unlike MikroTik's
    # own docs page which shows "running active" with a space), AND
    # "waiting" (a profile that's assigned but not yet started - e.g. a
    # starts-when=first-auth profile before the user's first login, where
    # end-time also literally comes back as the string "not-yet-running").
    # RouterOS's own state vocabulary clearly isn't fully documented/stable
    # across versions, so instead of allow-listing "running"-ish strings
    # (which silently excluded "waiting" accounts entirely - they got NO
    # quota and NO expiry at all, not even the first-use fallback below),
    # only deny-list "used" (an expired/superseded assignment that should
    # never contribute quota or expiry for a still-current account).
    #
    # Fallback for accounts on a starts-when=first-auth profile whose
    # end-time isn't known yet (see profile_first_use_days above): the
    # user hasn't ever logged in, so we can't compute an absolute expire_at,
    # but we CAN carry over the profile's validity as
    # User.expire_days_after_first_use - the panel's RADIUS server already
    # activates that into a real expire_at on this user's actual first
    # successful login (see services/radius_server.py), exactly mirroring
    # what RouterOS itself would have done.
    user_quota: dict[str, int] = {}
    user_expiry: dict[str, dt.datetime] = {}
    user_expiry_days_after_first_use: dict[str, int] = {}
    for up in user_profiles:
        state = (up.get("state") or "").strip().lower()
        if state == "used":
            continue
        username = up.get("user")
        profile = up.get("profile")
        if not username:
            continue
        if profile in profile_quota_bytes:
            user_quota[username] = user_quota.get(username, 0) + profile_quota_bytes[profile]
        end_time = _parse_mikrotik_datetime(up.get("end-time"))
        if end_time and (username not in user_expiry or end_time > user_expiry[username]):
            user_expiry[username] = end_time
        elif not end_time and str(profile) in profile_first_use_days:
            user_expiry_days_after_first_use[username] = profile_first_use_days[str(profile)]

    # user -> true lifetime bytes used, from RouterOS's own per-user monitor
    # counter (keyed by the user's ".id", so join through um_users below).
    user_used: dict[str, int] = {}
    for um_user in um_users:
        uid = um_user.get(".id")
        name = um_user.get("name")
        if not uid or not name:
            continue
        row = usage_by_id.get(uid) or {}
        try:
            used = int(row.get("total-download") or 0) + int(row.get("total-upload") or 0)
        except (TypeError, ValueError):
            used = 0
        user_used[name] = used

    imported: list[str] = []
    skipped: list[dict] = []

    for um_user in um_users:
        name = um_user.get("name")
        password = um_user.get("password")
        if not name:
            continue
        if not password:
            skipped.append({"name": name, "reason": "این کاربر پسورد ندارد (شاید فقط OTP دارد)"})
            continue

        existing_conn = (
            db.query(models.Connection)
            .filter(models.Connection.ppp_username == name)
            .first()
        )
        if existing_conn:
            skipped.append({"name": name, "reason": "قبلا ایمپورت شده"})
            continue

        user = db.query(models.User).filter(models.User.username == name).first()
        if not user:
            user = models.User(
                username=name,
                notes="ایمپورت‌شده خودکار از User Manager میکروتیک",
                total_quota_bytes=user_quota.get(name, 0),
                used_bytes=user_used.get(name, 0),
                expire_at=user_expiry.get(name),
                # Only meaningful when expire_at above is None - i.e. this
                # account's profile counts validity from first login and
                # that hasn't happened yet, so RouterOS had no absolute
                # end-time to give us (see profile_first_use_days above).
                expire_days_after_first_use=None if user_expiry.get(name) else user_expiry_days_after_first_use.get(name),
            )
            db.add(user)
            db.flush()

        conn = models.Connection(
            user_id=user.id,
            node_id=node.id,
            type=models.ConnectionType.openvpn,
            enabled=not _secret_is_disabled(um_user.get("disabled")),
            ppp_username=name,
            ppp_password=password,
            # Carries over RouterOS User Manager's "shared-users" (max
            # simultaneous sessions) as-is, so the limit that already
            # applied on the router keeps applying once the panel takes
            # over authentication.
            max_concurrent_sessions=_parse_shared_users(um_user.get("shared-users")),
        )
        db.add(conn)
        imported.append(name)

    db.commit()
    return {
        "imported": imported,
        "imported_count": len(imported),
        "skipped": skipped,
        "skipped_count": len(skipped),
    }


# -------------------------------------------------------- import 3X-UI clients
def import_threexui_clients(db: Session, node: models.Node) -> dict:
    """Reads clients that already exist directly on the 3X-UI panel's
    configured inbound (created there before this node was connected to the
    panel) and imports any not already known here as a new User+Connection,
    preserving their uuid/email/flow so the client's existing vless
    link/QR code keeps working unchanged. Their current up/down usage is
    seeded as a starting point on the shared quota. Quota/expiry
    enforcement moves to this panel going forward (same as every other
    import path here) rather than anything configured on 3X-UI itself.
    Purely read-only on the panel side - nothing is changed there."""
    if node.type != models.NodeType.xray or node.xr_panel_mode != "3xui":
        raise HTTPException(400, "این عملیات فقط برای نود Xray با روش اتصال «پنل 3X-UI» است")

    try:
        with client_for_node(node) as xc:
            clients = xc.list_clients_with_usage()
    except XrayError as exc:
        raise HTTPException(400, str(exc))

    imported: list[str] = []
    skipped: list[dict] = []

    for c in clients:
        email = c.get("email")
        client_uuid = c.get("id")
        if not email:
            continue
        if not client_uuid:
            skipped.append({"name": email, "reason": "این کلاینت شناسه (uuid) ندارد"})
            continue

        existing_conn = (
            db.query(models.Connection)
            .filter(models.Connection.xr_email == email, models.Connection.node_id == node.id)
            .first()
        )
        if existing_conn:
            skipped.append({"name": email, "reason": "قبلا ایمپورت شده"})
            continue

        # 3X-UI client emails are just a free-text label chosen in the panel
        # (not necessarily a real email) - use the part before "@" (if any)
        # as the panel username.
        username = (email.split("@")[0] or email).strip()
        if not username:
            skipped.append({"name": email, "reason": "نام کلاینت خالی است"})
            continue

        used_bytes = int(c.get("up", 0) or 0) + int(c.get("down", 0) or 0)
        total_quota_bytes = int(c.get("totalGB", 0) or 0)  # already raw bytes, see ThreeXUIClient
        expiry_ms = int(c.get("expiryTime", 0) or 0)
        expire_at = dt.datetime.utcfromtimestamp(expiry_ms / 1000) if expiry_ms > 0 else None
        # 3X-UI's "Start After First Use" client option is encoded as a
        # NEGATIVE expiryTime: -(days * 86400000) - it does NOT mean "no
        # expiry" (that's expiryTime == 0). Missing this meant every client
        # using that 3X-UI option imported as permanently unlimited instead
        # of carrying its day count over.
        expire_days_after_first_use = round(abs(expiry_ms) / 86400000) if expiry_ms < 0 else None

        user = db.query(models.User).filter(models.User.username == username).first()
        if not user:
            user = models.User(
                username=username,
                notes="ایمپورت‌شده خودکار از پنل 3X-UI",
                used_bytes=used_bytes,
                total_quota_bytes=total_quota_bytes,
                expire_at=expire_at,
                expire_days_after_first_use=expire_days_after_first_use,
            )
            db.add(user)
            db.flush()
        else:
            # merge this connection's history into the user's shared usage/
            # quota/expiry (take the larger quota, the later expiry - same
            # "don't silently shrink an existing entitlement" idea as the
            # MikroTik User Manager import).
            user.used_bytes = (user.used_bytes or 0) + used_bytes
            if total_quota_bytes and (not user.total_quota_bytes or total_quota_bytes > user.total_quota_bytes):
                user.total_quota_bytes = total_quota_bytes
            if expire_at and (not user.expire_at or expire_at > user.expire_at):
                user.expire_at = expire_at
                user.expire_days_after_first_use = None
            elif (
                expire_days_after_first_use
                and not user.expire_at
                and (not user.expire_days_after_first_use or expire_days_after_first_use > user.expire_days_after_first_use)
            ):
                user.expire_days_after_first_use = expire_days_after_first_use

        conn = models.Connection(
            user_id=user.id,
            node_id=node.id,
            type=models.ConnectionType.xray,
            enabled=bool(c.get("enable", True)),
            xr_uuid=client_uuid,
            xr_email=email,
            xr_flow=c.get("flow") or "",
            total_bytes=used_bytes,
        )
        db.add(conn)
        imported.append(email)

    db.commit()
    return {
        "imported": imported,
        "imported_count": len(imported),
        "skipped": skipped,
        "skipped_count": len(skipped),
    }


# ---------------------------------------------------------------- share info
def get_connection_share(connection: models.Connection) -> dict:
    """Returns {"kind": ..., "link": ..., "config_text": ...} for a
    connection, PLUS the individual fields that went into config_text
    (server/port/username/password/psk) so a caller that wants to render
    its own nicer, type-specific layout (e.g. the sales bot - see
    telegram_bot/connection_sender.py) doesn't have to re-parse the
    human-readable Persian config_text blob to get them back out."""
    node = connection.node
    if connection.type == models.ConnectionType.wireguard:
        try:
            with MikrotikClient.for_node(node) as mt:
                server_pub = mt.get_public_key(node.mt_wireguard_interface) or ""
        except MikrotikError as exc:
            raise HTTPException(400, str(exc))
        text = build_wireguard_config(connection, node, server_pub)
        return {
            "kind": "wireguard", "link": None, "config_text": text,
            "server": node.mt_endpoint_host, "port": node.mt_endpoint_port,
            "username": None, "password": None, "psk": None,
        }

    if connection.type == models.ConnectionType.openvpn:
        text = build_openvpn_config(connection, node)
        return {
            "kind": "openvpn", "link": None, "config_text": text,
            "server": node.mt_endpoint_host, "port": node.mt_ovpn_port or 1194,
            "username": connection.ppp_username, "password": connection.ppp_password, "psk": None,
        }

    if connection.type == models.ConnectionType.l2tp:
        text = build_l2tp_info(connection, node)
        psk = node.mt_l2tp_ipsec_secret if node.mt_l2tp_use_ipsec else None
        return {
            "kind": "l2tp", "link": None, "config_text": text,
            "server": node.mt_endpoint_host, "port": None,
            "username": connection.ppp_username, "password": connection.ppp_password, "psk": psk,
        }

    if connection.type == models.ConnectionType.ikev2:
        text = build_ikev2_info(connection, node)
        return {
            "kind": "ikev2", "link": None, "config_text": text,
            "server": node.mt_endpoint_host, "port": None,
            "username": connection.ppp_username, "password": connection.ppp_password, "psk": node.mt_ikev2_psk,
        }

    if connection.type == models.ConnectionType.sstp:
        text = build_sstp_info(connection, node)
        return {
            "kind": "sstp", "link": None, "config_text": text,
            "server": node.mt_endpoint_host, "port": node.mt_sstp_port or 443,
            "username": connection.ppp_username, "password": connection.ppp_password, "psk": None,
        }

    # xray
    link = build_vless_link(connection, node)
    return {
        "kind": "vless", "link": link, "config_text": None,
        "server": None, "port": None, "username": None, "password": None, "psk": None,
    }
