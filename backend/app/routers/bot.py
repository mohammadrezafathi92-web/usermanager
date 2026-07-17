"""External API for a customer-facing bot (e.g. a Telegram sales bot) to
create/renew/check/delete users without needing an admin login. Auth is a
static per-integration key sent in the `X-API-Key` header - manage keys
from the panel's Settings page.

This is also what a REMOTELY-deployed bot instance talks to (see
telegram_bot/remote_bridge.py + services/remote_deploy.py) when the admin
chooses to run the interactive Telegram bot on a second server instead of
in-process here - same endpoints, same X-API-Key auth, just reached over
the network instead of in-process."""
import os
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from .. import models, schemas
from ..database import get_db
from ..deps import get_bot_api_key
from ..services import user_ops
from ..services.quota_manager import _set_connection_enabled
from .panel_settings import _get_or_create as _get_or_create_settings

router = APIRouter(prefix="/api/bot", tags=["bot"], dependencies=[Depends(get_bot_api_key)])


def _connection_info(conn: models.Connection) -> schemas.BotConnectionInfo:
    share = user_ops.get_connection_share(conn)
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
        total_bytes=conn.total_bytes or 0,
        created_at=conn.created_at,
        purchase_batch=conn.purchase_batch,
        package_name=conn.package_name_snapshot,
    )


def _user_response(user: models.User) -> schemas.BotUserResponse:
    loyalty = getattr(user, "_loyalty_reward_just_granted", None)
    return schemas.BotUserResponse(
        id=user.id,
        username=user.username,
        full_name=user.full_name,
        status=user.status,
        total_quota_bytes=user.total_quota_bytes,
        used_bytes=user.used_bytes,
        remaining_bytes=user.remaining_bytes,
        expire_at=user.expire_at,
        telegram_id=user.telegram_id,
        balance=user.balance or 0,
        connections=[_connection_info(c) for c in user.connections],
        referral_code=user.referral_code,
        loyalty_reward_credit=loyalty[0] if loyalty else None,
        loyalty_reward_gb=loyalty[1] if loyalty else None,
        reserved_quota_gb=(user.reserved_quota_bytes / (1024 ** 3)) if user.reserved_quota_bytes else None,
        reserved_duration_days=user.reserved_duration_days,
    )


def _get_user_or_404(db: Session, username: str, owner_admin_id: Optional[int] = None) -> models.User:
    """owner_admin_id, when given, scopes this lookup to one admin's group -
    used by the built-in bot when a linked group-admin (see
    telegram_bot/admin_scope.py) is operating on "their" users, so they
    can't reach/guess a user belonging to a different admin's group by
    username. A full/config bot admin never passes this (sees everyone,
    same as before this scoping existed)."""
    user = db.query(models.User).filter(models.User.username == username).first()
    if not user or (owner_admin_id is not None and user.owner_admin_id != owner_admin_id):
        raise HTTPException(404, "کاربر پیدا نشد")
    return user


@router.get("/nodes", response_model=list[schemas.BotNodeInfo])
def list_nodes(db: Session = Depends(get_db)):
    nodes = db.query(models.Node).filter(models.Node.enabled == True).all()  # noqa: E712
    return nodes


@router.get("/packages", response_model=list[schemas.PackageOut])
def list_packages(db: Session = Depends(get_db)):
    """Active packages, in the order the admin arranged them - shown to
    customers by the sales bot at checkout. Eager-loads `connections` AND
    `files` - the built-in bot (app/telegram_bot/panel_bridge.py) closes
    its DB session before converting the result to a schema, so a
    lazy-loaded relationship accessed at that point would raise a
    DetachedInstanceError and silently hang the "خرید اکانت جدید" button
    (this bit us once already with `connections` - `files` needs the same
    treatment since it's read by PackageOut too)."""
    return (
        db.query(models.Package)
        .options(joinedload(models.Package.connections), joinedload(models.Package.files))
        .filter(models.Package.bot_enabled == True)  # noqa: E712
        .order_by(models.Package.sort_order, models.Package.id)
        .all()
    )


@router.get("/payment-info", response_model=schemas.PanelSettingsOut)
def get_payment_info(db: Session = Depends(get_db)):
    """Card-to-card payment details configured by the admin - shown by the
    sales bot right before it asks the customer for a receipt photo."""
    return _get_or_create_settings(db)


@router.get("/customer-menu-config")
def get_customer_menu_config(db: Session = Depends(get_db)):
    """Which customer main-menu buttons are hidden (see Settings > ربات >
    منوی مشتری and telegram_bot/keyboards.py's main_menu_kb)."""
    row = db.get(models.BotSettings, 1)
    raw = (row.customer_menu_disabled_items or "") if row else ""
    items = [x.strip() for x in raw.split(",") if x.strip()]
    return {"disabled_items": items}


@router.get("/tutorials", response_model=list[schemas.TutorialOut])
def list_tutorials(db: Session = Depends(get_db)):
    """Enabled tutorial entries, shown to customers from the bot's "📚
    آموزش" menu. Eager-loads `media` for the same reason list_packages
    eager-loads `connections`/`files` - the built-in bot converts this to a
    schema after its DB session has already closed."""
    return (
        db.query(models.Tutorial)
        .options(joinedload(models.Tutorial.media), joinedload(models.Tutorial.software))
        .filter(models.Tutorial.enabled == True)  # noqa: E712
        .order_by(models.Tutorial.sort_order, models.Tutorial.id)
        .all()
    )


@router.get("/packages/{package_id}/files/{file_id}/download")
def download_package_file(package_id: int, file_id: int, db: Session = Depends(get_db)):
    """Raw bytes of a package's attached file - used by the bot (in-process
    or remote) to actually hand the file to the customer. The in-process
    bot doesn't need this (it reads stored_path straight off disk via
    panel_bridge.py), but a remotely-deployed bot has no local access to
    this server's disk, so it downloads the bytes over this endpoint
    instead - same X-API-Key auth as everything else on this router."""
    row = (
        db.query(models.PackageFile)
        .filter(models.PackageFile.id == file_id, models.PackageFile.package_id == package_id)
        .first()
    )
    if not row or not os.path.exists(row.stored_path):
        raise HTTPException(404, "فایل پیدا نشد")
    return FileResponse(row.stored_path, filename=row.filename, media_type=row.content_type or "application/octet-stream")


@router.get("/tutorials/{tutorial_id}/media/{media_id}/download")
def download_tutorial_media(tutorial_id: int, media_id: int, db: Session = Depends(get_db)):
    """Raw bytes of a tutorial's attached photo/video - same rationale as
    download_package_file above."""
    row = (
        db.query(models.TutorialMedia)
        .filter(models.TutorialMedia.id == media_id, models.TutorialMedia.tutorial_id == tutorial_id)
        .first()
    )
    if not row or not os.path.exists(row.stored_path):
        raise HTTPException(404, "فایل پیدا نشد")
    return FileResponse(row.stored_path, filename=row.filename, media_type=row.content_type or "application/octet-stream")


@router.get("/tutorials/{tutorial_id}/software/{software_id}/download")
def download_tutorial_software(tutorial_id: int, software_id: int, db: Session = Depends(get_db)):
    """Raw bytes of a tutorial's uploaded software file - same rationale as
    download_package_file above. Only applies to entries that have an
    uploaded file (stored_path set); link-only entries are just sent as a
    URL and never hit this endpoint."""
    row = (
        db.query(models.TutorialSoftware)
        .filter(models.TutorialSoftware.id == software_id, models.TutorialSoftware.tutorial_id == tutorial_id)
        .first()
    )
    if not row or not row.stored_path or not os.path.exists(row.stored_path):
        raise HTTPException(404, "فایل پیدا نشد")
    return FileResponse(row.stored_path, filename=row.filename, media_type=row.content_type or "application/octet-stream")


@router.get("/admin-by-telegram/{tg_id}", response_model=schemas.BotAdminInfo)
def get_admin_by_telegram(tg_id: int, db: Session = Depends(get_db)):
    """Looked up by the built-in bot on every message from someone who
    isn't in the bot's global admin_ids list, to see whether they're
    instead a linked group-admin (AdminUser.telegram_id) who should get a
    scoped-down admin menu for their own group only - see
    telegram_bot/admin_scope.py."""
    admin = db.query(models.AdminUser).filter(models.AdminUser.telegram_id == tg_id).first()
    if not admin:
        raise HTTPException(404, "ادمین پیدا نشد")
    return admin


@router.get("/telegram-user-ids", response_model=list[int])
def telegram_user_ids(db: Session = Depends(get_db)):
    """Every DISTINCT telegram id currently linked to a panel account - used
    by the admin bot's "📢 پیام همگانی" broadcast, which sends one generic
    message per chat id (as opposed to the daily quota/expiry reminder job,
    which queries per-User and is fine seeing the same telegram_id more than
    once - see services/notify.py). .distinct() matters now that a single
    telegram id can be linked to more than one User (see User.telegram_id in
    models.py) - without it, a customer with 2 linked accounts would get the
    same broadcast message twice."""
    rows = (
        db.query(models.User.telegram_id)
        .filter(models.User.telegram_id.isnot(None))
        .distinct()
        .all()
    )
    return [r[0] for r in rows]


@router.post("/users", response_model=schemas.BotUserResponse)
def create_user(payload: schemas.BotCreateUserRequest, db: Session = Depends(get_db)):
    user = user_ops.create_user_record(
        db, payload.username, payload.full_name, payload.quota_gb, payload.expire_days,
        telegram_id=payload.telegram_id, owner_admin_id=payload.owner_admin_id,
        package_id=payload.package_id,
    )
    # Every connection in this one request is one purchase - share a single
    # batch (see models.Connection.purchase_batch) so the bot's "اکانت من"
    # groups them together instead of listing each service separately.
    batch = uuid.uuid4().hex if payload.connections else None
    for spec in payload.connections:
        node = db.get(models.Node, spec.node_id)
        if not node:
            continue
        user_ops.provision_connection(
            db, user, node, spec.protocol, spec.flow or "",
            purchase_batch=batch, package_name=payload.package_name,
        )
    db.refresh(user)
    return _user_response(user)


@router.post("/referral/apply")
def apply_referral(payload: schemas.ReferralApplyRequest, db: Session = Depends(get_db)):
    """Called once, right after admin_pending.py's receipt-approval handler
    creates a brand-new customer account via create_user above - the ONLY
    choke point new customer accounts are created through, so this is the
    one place referral-code redemption needs to be wired in. Actual reward
    logic lives in services/user_ops.py's apply_referral_code (both the
    referrer and the new user get a gift, per the confirmed design - not
    just the referrer)."""
    user = db.query(models.User).filter(models.User.username == payload.username).first()
    if not user:
        raise HTTPException(404, "کاربر پیدا نشد")
    ok, reason = user_ops.apply_referral_code(db, user, payload.referral_code)
    return {"ok": ok, "reason": reason}


@router.post("/discount/validate", response_model=schemas.DiscountValidateResult)
def validate_discount(payload: schemas.DiscountValidateRequest, db: Session = Depends(get_db)):
    """Check-as-you-type step - does NOT consume the code (see
    /discount/redeem for that). `username`, when the customer already has
    an account, also catches "you already used this code" before they get
    to the final confirm screen."""
    valid, reason, amount = user_ops.validate_discount_code(
        db, payload.code, payload.package_price, username=payload.username
    )
    return schemas.DiscountValidateResult(
        valid=valid,
        reason=reason or None,
        discount_amount=amount,
        final_price=max(0, (payload.package_price or 0) - amount),
    )


@router.post("/discount/redeem", response_model=schemas.DiscountValidateResult)
def redeem_discount(payload: schemas.DiscountRedeemRequest, db: Session = Depends(get_db)):
    """Called once a purchase is actually confirmed - re-validates then
    atomically consumes the code (bumps used_count, records a
    DiscountCodeRedemption row). See services/user_ops.py's
    redeem_discount_code."""
    ok, reason, amount = user_ops.redeem_discount_code(db, payload.code, payload.username, payload.package_price)
    return schemas.DiscountValidateResult(
        valid=ok,
        reason=reason or None,
        discount_amount=amount,
        final_price=max(0, (payload.package_price or 0) - amount),
    )


@router.get("/users", response_model=schemas.BotUserListPage)
def list_users(
    db: Session = Depends(get_db),
    page: int = 1,
    page_size: int = 20,
    search: Optional[str] = None,
    owner_admin_id: Optional[int] = None,
):
    """Used by the admin side of the sales bot to browse/search customers.
    owner_admin_id, when given, scopes this to one group-admin's own users
    (see telegram_bot/admin_scope.py) - omitted entirely by the global bot
    admin flow, which still sees everyone."""
    page = max(page, 1)
    page_size = min(max(page_size, 1), 100)
    query = db.query(models.User)
    if owner_admin_id is not None:
        query = query.filter(models.User.owner_admin_id == owner_admin_id)
    if search:
        like = f"%{search}%"
        query = query.filter(or_(models.User.username.ilike(like), models.User.full_name.ilike(like)))
    total = query.count()
    items = (
        query.order_by(models.User.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/users/by-telegram/{telegram_id}", response_model=schemas.BotUserResponse)
def get_user_by_telegram(telegram_id: int, db: Session = Depends(get_db), owner_admin_id: Optional[int] = None):
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
    query = db.query(models.User).filter(models.User.telegram_id == telegram_id)
    if owner_admin_id is not None:
        query = query.filter(models.User.owner_admin_id == owner_admin_id)
    user = query.order_by(models.User.id.desc()).first()
    if not user:
        raise HTTPException(404, "کاربری با این حساب تلگرام پیدا نشد")
    return _user_response(user)


@router.get("/users/by-telegram/{telegram_id}/all", response_model=list[schemas.BotUserResponse])
def list_users_by_telegram(telegram_id: int, db: Session = Depends(get_db), owner_admin_id: Optional[int] = None):
    """Every account linked to this telegram id (could be 0, 1, or several -
    see the big comment on User.telegram_id in models.py). The bot uses
    this to decide whether to act directly (0 or 1 result) or show an
    account picker (2+ results) - see telegram_bot's _resolve_account.

    owner_admin_id scopes this to one Admin's own tree, same rationale as
    get_user_by_telegram above - a per-admin bot's account-picker should
    never surface someone else's customer accounts from another Admin."""
    query = db.query(models.User).filter(models.User.telegram_id == telegram_id)
    if owner_admin_id is not None:
        query = query.filter(models.User.owner_admin_id == owner_admin_id)
    users = query.order_by(models.User.id.desc()).all()
    return [_user_response(u) for u in users]


@router.get("/users/{username}", response_model=schemas.BotUserResponse)
def get_user(username: str, db: Session = Depends(get_db), owner_admin_id: Optional[int] = None):
    return _user_response(_get_user_or_404(db, username, owner_admin_id))


@router.post("/users/{username}/link-telegram", response_model=schemas.BotUserResponse)
def link_telegram(username: str, payload: schemas.BotLinkTelegramRequest, db: Session = Depends(get_db)):
    # telegram_id is intentionally NOT required to be unique across users -
    # one Telegram account can be linked to several panel accounts (a
    # customer who bought more than once under different usernames). The
    # bot's "🔗 وصل کردن حساب قبلی" flow (telegram_bot/handlers/customer.py)
    # just adds this account to that telegram id's list; when there's more
    # than one, the bot shows an account picker (see list_users_by_telegram
    # below + telegram_bot's _resolve_account).
    user = _get_user_or_404(db, username)
    user.telegram_id = payload.telegram_id
    db.commit()
    db.refresh(user)
    return _user_response(user)


@router.post("/users/{username}/connections", response_model=schemas.BotConnectionInfo)
def add_connection(
    username: str, spec: schemas.BotCreateConnectionSpec, db: Session = Depends(get_db),
    owner_admin_id: Optional[int] = None,
):
    user = _get_user_or_404(db, username, owner_admin_id)
    node = db.get(models.Node, spec.node_id)
    if not node:
        raise HTTPException(400, "نود پیدا نشد")
    conn = user_ops.provision_connection(
        db, user, node, spec.protocol, spec.flow or "",
        purchase_batch=spec.purchase_batch, package_name=spec.package_name,
    )
    return _connection_info(conn)


@router.post("/users/{username}/renew", response_model=schemas.BotUserResponse)
def renew(
    username: str, payload: schemas.BotRenewRequest, db: Session = Depends(get_db),
    owner_admin_id: Optional[int] = None,
):
    user = _get_user_or_404(db, username, owner_admin_id)
    user_ops.renew_user(db, user, payload.add_gb, payload.add_days, payload.reset_usage, package_id=payload.package_id)
    return _user_response(user)


@router.post("/users/{username}/reset-usage", response_model=schemas.BotUserResponse)
def reset_usage(username: str, db: Session = Depends(get_db), owner_admin_id: Optional[int] = None):
    user = _get_user_or_404(db, username, owner_admin_id)
    user_ops.renew_user(db, user, reset_usage=True)
    return _user_response(user)


@router.post("/users/{username}/set-enabled", response_model=schemas.BotUserResponse)
def set_user_enabled(username: str, enabled: bool, db: Session = Depends(get_db), owner_admin_id: Optional[int] = None):
    """Enables/disables the user AND actually pushes the change to every
    node they have a connection on (unlike just flipping the status column,
    which the background poller would otherwise silently revert back to
    "active" once quota/expiry no longer justify it)."""
    user = _get_user_or_404(db, username, owner_admin_id)
    user.status = models.UserStatus.active if enabled else models.UserStatus.disabled
    for conn in user.connections:
        _set_connection_enabled(db, conn, enabled=enabled)
    db.commit()
    db.refresh(user)
    return _user_response(user)


@router.post("/users/{username}/add-balance", response_model=schemas.BotUserResponse)
def add_balance(username: str, payload: schemas.BotAddBalanceRequest, db: Session = Depends(get_db)):
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
    user = _get_user_or_404(db, username)
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
        user.balance = (user.balance or 0) + payload.amount
        db.commit()
        db.refresh(user)
    return _user_response(user)


@router.delete("/users/{username}")
def delete_user(username: str, db: Session = Depends(get_db), owner_admin_id: Optional[int] = None):
    user = _get_user_or_404(db, username, owner_admin_id)
    user_ops.delete_user_cascade(db, user)
    return {"ok": True}
