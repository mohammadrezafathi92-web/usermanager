"""The Telegram Mini App's own API.

No admin session anywhere near it. Every request carries the initData blob
Telegram gave the page, in the X-Telegram-InitData header, and
services/telegram_webapp.verify turns that into two facts: which Telegram
user this is, and which bot they came through. The second one is what names
the reseller whose shop they are standing in - see that module for why the
scope has to come from a signature rather than from the URL.

Everything below then reuses the functions the sales bot already calls
(routers/bot.py's list_packages, get_payment_info, list_users_by_telegram),
with the same owner_admin_id those functions already take. That is the whole
point: the Mini App is a second face on the existing shop, not a second shop.
If a price, a package's visibility or a payment card is right in the bot, it
is right here, because it is the same code answering.

Under /api/ so nginx's existing `location /api/` proxy rule covers it with
no configuration change - the same reason routers/subscription.py lives
there.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging

import requests

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..services import hierarchy, telegram_webapp
from . import bot as bot_router

logger = logging.getLogger("miniapp")

router = APIRouter(prefix="/api/miniapp", tags=["miniapp"])


def current_visitor(
    x_telegram_init_data: str = Header(None, alias="X-Telegram-InitData"),
    db: Session = Depends(get_db),
) -> dict:
    """The one door in. Returns {"telegram_id", "owner_admin_id", "user"}."""
    try:
        return telegram_webapp.verify(db, x_telegram_init_data or "")
    except telegram_webapp.WebAppAuthError as exc:
        # Logged with the reason, answered without it. An unauthenticated
        # caller learning WHICH part of their forgery failed is being helped
        # to fix it.
        logger.warning("miniapp: initData rejected (%s)", exc)
        raise HTTPException(401, "این صفحه باید از داخل ربات تلگرام باز شود.")


def _shop_packages(db: Session, owner: int | None) -> list:
    """Every package this shop may show in the Mini App, trial included.

    Split out from _shop_shelves because the trial is sold like the others
    and DISPLAYED unlike them - so the buyability check (_package_or_404)
    has to see it while the shelves must not.
    """
    return [
        p for p in bot_router.list_packages(owner_admin_id=owner, db=db)
        if getattr(p, "miniapp_enabled", True)
    ]


def _trial_package(db: Session, owner: int | None):
    """The free sample, which is not a shelf.

    It was landing in «سایر پلن‌ها» at the bottom of the shop - filed with
    the leftovers, below everything a customer might pay for, which is the
    opposite of what a sample is for. Reported 2026-09-15: «تست هم نباید
    اون پایین نشون داده بشه و توی سایر پلن ها باید اون بالا بزنه تست
    رایگان».

    Returned on its own so the page can put it at the top, and excluded
    from the shelves so it is not in both places. Only the first one: a
    shop offering two different free samples is a shop with a
    configuration mistake, not a feature, and showing both would present
    the mistake as a choice.
    """
    for pkg in _shop_packages(db, owner):
        if getattr(pkg, "is_trial", False) and (pkg.connections or []):
            return pkg
    return None


def _shop_shelves(db: Session, owner: int | None) -> list[dict]:
    """The shop, already arranged into cards.

    Grouped on the server rather than in the page because the RULES are
    here: which packages this shop sells at all, which of them the Mini App
    is allowed to show, which shelves are switched on, and what order any
    of it goes in. Sending a flat list plus a group table and letting the
    page work it out would be the same logic written twice, in two
    languages, free to disagree.

    Three things this must get right, each of which is a way a reseller
    quietly loses sales:

    - Packages come from bot_router.list_packages, so the per-seller price
      overlay and the visibility rules are the SAME ones the bot answers
      with. Reading models.Package directly here would sell at the wrong
      price.
    - miniapp_enabled filters afterwards. It is an extra shelf switch, not
      a replacement for bot_enabled, which list_packages already applied.
    - An ungrouped package is NOT dropped. It lands under «سایر پلن‌ها»,
      because forgetting to pick a group is not a decision to stop selling
      something - and on the day this ships, every existing package is
      ungrouped.

    A shelf with nothing on it is omitted: an empty card is a promise the
    shop cannot keep.
    """
    packages = [p for p in _shop_packages(db, owner) if not getattr(p, "is_trial", False)]

    groups = (
        db.query(models.PackageGroup)
        .filter(
            hierarchy.owner_id_in_clause(models.PackageGroup.owner_admin_id, {owner}),
            models.PackageGroup.enabled.is_(True),
        )
        .order_by(models.PackageGroup.sort_order, models.PackageGroup.id)
        .all()
    )

    shelves: list[dict] = []
    for group in groups:
        on_shelf = [p for p in packages if getattr(p, "group_id", None) == group.id]
        if not on_shelf:
            continue
        shelves.append({
            "id": group.id,
            "name": group.name,
            "description": group.description,
            "packages": on_shelf,
        })

    known = {g.id for g in groups}
    # Note `not in known` rather than `is None`: a package whose group was
    # switched OFF would otherwise vanish from the shop entirely, which is
    # not what turning off a shelf means - it means that card is gone, not
    # that its contents are unsellable.
    loose = [p for p in packages if getattr(p, "group_id", None) not in known]
    if loose:
        shelves.append({
            "id": None,
            # Only worth a title when it is one card among several. On a
            # shop with no groups at all, «سایر پلن‌ها» would be naming the
            # only shelf in the room.
            "name": "سایر پلن‌ها" if shelves else "",
            "description": None,
            "packages": loose,
        })
    return shelves


def _bot_username(owner_admin_id: int | None) -> str | None:
    from ..telegram_bot import runner

    try:
        status = (
            runner.admin_bot_status(owner_admin_id)
            if owner_admin_id is not None
            else runner.get_status()
        )
        return status.get("bot_username")
    except Exception:
        # A missing @username costs the invite card a nicer link and
        # nothing else. It must never be able to take the whole page down.
        logger.debug("miniapp: no bot username for owner %s", owner_admin_id, exc_info=True)
        return None


def _shop_title(db: Session, owner_admin_id: int | None) -> str:
    if owner_admin_id is None:
        return ""
    admin = db.get(models.AdminUser, owner_admin_id)
    return admin.username if admin else ""


# The Mini App needs Telegram's own small script for the theme colours and
# initData. It is hosted on telegram.org, which is exactly the host that is
# blocked or throttled on the networks this product is sold into - so the
# page was depending, at load time, on the one domain its customers cannot
# reliably reach.
#
# Served from this panel instead. The backend fetches it once, through the
# same route the bots already use to reach Telegram (the WireGuard tunnel or
# the configured API proxy - see telegram_bot/runner.py), and keeps it in
# memory. The customer's browser then gets it from the same origin the page
# itself came from, which it has obviously just reached.
_TELEGRAM_SCRIPT_URL = "https://telegram.org/js/telegram-web-app.js"
_script_cache: dict[str, object] = {"body": None, "fetched_at": None}
_SCRIPT_TTL = dt.timedelta(hours=12)


@router.get("/telegram-web-app.js")
def telegram_web_app_script():
    from ..telegram_bot.runner import _lookup_telegram_api_proxy_url

    now = dt.datetime.utcnow()
    cached, at = _script_cache["body"], _script_cache["fetched_at"]
    if cached and at and now - at < _SCRIPT_TTL:
        return Response(content=cached, media_type="application/javascript")

    proxies = None
    proxy = _lookup_telegram_api_proxy_url()
    if proxy:
        proxies = {"http": proxy, "https": proxy}
    try:
        resp = requests.get(_TELEGRAM_SCRIPT_URL, timeout=20, proxies=proxies)
        resp.raise_for_status()
        body = resp.content
    except requests.RequestException as exc:
        logger.warning("miniapp: could not fetch Telegram's script (%s)", exc)
        if cached:
            # A stale copy is worth far more than none: without it the page
            # loses the theme and, on clients that only expose initData
            # through this script, its credential too.
            return Response(content=cached, media_type="application/javascript")
        # Valid, empty JavaScript. The page is built to survive its absence
        # (see pages/MiniApp.jsx), and a 502 here would only add a console
        # error to a page that is already coping.
        return Response(content=b"/* telegram-web-app.js unavailable */\n",
                        media_type="application/javascript")

    _script_cache["body"], _script_cache["fetched_at"] = body, now
    logger.info("miniapp: Telegram's script cached (%d bytes)", len(body))
    return Response(content=body, media_type="application/javascript")


@router.get("/home")
def home(visitor: dict = Depends(current_visitor), db: Session = Depends(get_db)):
    """Everything the first screen needs, in one request.

    One round trip rather than four, because this page opens on a phone on
    an Iranian mobile network - where the cost is per request, not per byte.
    """
    owner = visitor["owner_admin_id"]

    packages = _shop_shelves(db, owner)
    accounts = bot_router.list_users_by_telegram(visitor["telegram_id"], db=db, owner_admin_id=owner)
    payment = bot_router.get_payment_info(owner_admin_id=owner, db=db)

    # A customer can hold more than one account under the same Telegram id
    # (see models.User.telegram_id) - the bot shows a picker. Here they are
    # simply listed together, which is what a screen can do and a chat
    # cannot.
    #
    # PURCHASES, not connections. The first version listed every Connection,
    # which gave the customer a row per protocol saying "xray / فعال" and
    # nothing else - repeated, because one service bundles several - with no
    # quota, no usage and no expiry, since a Connection carries none of
    # those. What a customer means by "my services" is what they bought:
    # models.Purchase, which is exactly what the bot's «اکانت من» shows.
    services = []
    wallet = 0
    for account in accounts:
        wallet += getattr(account, "balance", 0) or 0
        username = getattr(account, "username", None)
        if not username:
            continue
        for purchase in bot_router.list_user_purchases(username, db=db, owner_admin_id=owner):
            services.append({
                "id": purchase.id,
                # The package's name as it was WHEN BOUGHT - renaming or
                # deleting the package later must not rewrite what someone
                # already paid for.
                "name": purchase.package_name_snapshot or "سرویس",
                "quota_bytes": purchase.quota_bytes,
                "used_bytes": purchase.used_bytes,
                "expire_at": purchase.expire_at,
                "status": purchase.status,
                "connection_count": purchase.connection_count,
                # A renewal already paid for but not yet started, because the
                # current one still has quota or days left (see
                # user_ops.renew_purchase's reservation queue). Worth showing:
                # otherwise a customer who has just renewed sees no change at
                # all and buys again.
                "reserved_quota_bytes": purchase.reserved_quota_bytes,
                "reserved_duration_days": purchase.reserved_duration_days,
                "account": username,
            })

    return {
        "shop": {
            "title": _shop_title(db, owner),
            # One entry per card. See _shop_shelves.
            "groups": packages,
            # For the invite card's share link. Read from the running bot's
            # own get_me() answer rather than stored anywhere, because a bot
            # can be renamed in BotFather at any time and a stale @username
            # produces an invite link that goes nowhere. None when the bot
            # is not currently running - the card falls back to sharing the
            # bare code, which still works when it is typed into the bot.
            "bot_username": _bot_username(owner),
            # Its own field, not a shelf - see _trial_package.
            "trial": _trial_package(db, owner),
        },
        "me": {
            "telegram_id": visitor["telegram_id"],
            "name": (visitor["user"].get("first_name") or "").strip(),
            "username": visitor["user"].get("username") or "",
            "wallet": wallet,
            "accounts": accounts,
        },
        "services": services,
        "payment": payment,
    }


# --------------------------------------------------------------- checkout
#
# Until now the Mini App could only be looked at. Every card was a dead
# rectangle - reported, accurately, as «نمیشه روش کلیک کرد»: there was no
# button anywhere in it except the three tabs.
#
# Both paths below are the SAME two the bot already offers, deliberately
# reusing the same functions rather than reimplementing them:
#   wallet  -> debit, then provision              (customer.py pay_with_balance)
#   receipt -> a row in the bot's pending queue   (customer.py receive_receipt)
# so a sale made from the Mini App is indistinguishable, afterwards, from
# one made in chat - same Purchase, same ledger entry, same «درخواست‌های در
# انتظار» screen the owner already checks.


def _own_account(db: Session, visitor: dict, username: str | None):
    """The visitor's own account, or None.

    This is the authorisation check for everything below, and it is the
    reason the username in the request body can be trusted at all: it is
    only ever accepted if it appears in the list of accounts that THIS
    Telegram id holds under THIS reseller. routers/bot.py's add_balance
    takes no owner_admin_id and would happily debit any username in the
    database, so nothing may reach it that has not passed through here.
    """
    accounts = bot_router.list_users_by_telegram(
        visitor["telegram_id"], db=db, owner_admin_id=visitor["owner_admin_id"]
    )
    if username:
        return next((a for a in accounts if a.username == username), None)
    return accounts[0] if accounts else None


def _package_or_404(db: Session, owner: int | None, package_id: int) -> dict:
    """From the list this shop actually sells, never db.get(Package).

    list_packages applies the per-seller price overlay and the visibility
    rules; reading the Package row directly would sell a hidden package, or
    sell it at the wrong price.
    """
    for pkg in _shop_packages(db, owner):
        if pkg.id == package_id:
            return pkg
    # Deliberately the same source the SHOP is drawn from, not
    # list_packages. A plan hidden with miniapp_enabled, or sitting on a
    # shelf that is switched off, must be unbuyable and not merely
    # invisible - otherwise the switch only hides the button, and the old
    # id keeps working for anyone who kept it.
    raise HTTPException(404, "این پلن دیگر برای فروش نیست.")


def _price_of(pkg) -> int:
    return int(getattr(pkg, "price", 0) or 0)


def _connections_of(pkg):
    return list(getattr(pkg, "connections", None) or [])


def _require_bundled(pkg) -> None:
    """A "plain" package - one the admin never gave a services bundle -
    needs the customer to pick a node and a protocol by hand, which the bot
    does over several questions (customer.py's pick_node/pick_protocol) and
    this screen does not ask yet. Selling one anyway would create a
    purchase with no connection in it: paid for, and nothing to connect to.
    Refused with the one sentence that tells the customer what to do
    instead."""
    if not _connections_of(pkg):
        raise HTTPException(400, "این پلن را فعلاً باید از داخل خود ربات بخرید.")


def _owner_bot_token(db: Session, owner_admin_id: int | None) -> str | None:
    """Whose bot speaks. A reseller's customer must hear from the reseller's
    own bot, not the shared one - to that customer the shared bot is a
    stranger, and may well be a bot they have never started, which cannot
    message them at all."""
    if owner_admin_id is None:
        return None  # shared bot - send_*_sync falls back to it on its own
    admin = db.get(models.AdminUser, owner_admin_id)
    if admin and admin.own_bot_token and admin.own_bot_enabled:
        return admin.own_bot_token
    return None


def _receipt_targets(db: Session, pending: dict) -> set[int]:
    """Deliberately the same rule as telegram_bot/handlers/customer.py's
    _notify_targets, arrived at the same way: the row's OWN owner_admin_id
    names the real owner, and the specific card that was shown may have its
    own watcher. config.approval_targets() is not consulted here - it reads
    a threading.local belonging to a bot thread, and this is a web worker.
    """
    targets: set[int] = set()
    owner_admin_id = pending.get("owner_admin_id")
    if owner_admin_id:
        admin = db.get(models.AdminUser, owner_admin_id)
        if admin and admin.telegram_id:
            targets.add(admin.telegram_id)
    else:
        # Ownerless (shared-panel) customer: the panel-wide bot admins.
        row = db.query(models.BotSettings).first()
        for raw in (row.admin_ids or "").split(",") if row else []:
            raw = raw.strip()
            if raw.isdigit():
                targets.add(int(raw))
    card_id = pending.get("payment_card_id")
    if card_id:
        card = db.get(models.PaymentCard, card_id)
        if card and card.approval_telegram_id:
            targets.add(card.approval_telegram_id)
    return targets


def _notify_receipt(db: Session, pending: dict, request_id: int, image: bytes,
                    title: str = "🧾 رسید پرداخت جدید (از مینی‌اپ)") -> None:
    """Best effort, and loud when it fails. A receipt nobody is shown is a
    customer waiting for ever, so a failure here is logged at warning level
    rather than swallowed - but it never fails the request: the row is
    already saved, and «درخواست‌های در انتظار» will show it whether or not
    the push notification arrived."""
    from ..telegram_bot import runner
    from ..telegram_bot.keyboards import approval_kb

    try:
        from ..telegram_bot.handlers.admin_pending import _pending_summary

        caption = title + "\n\n" + _pending_summary(pending)
    except Exception:
        logger.exception("miniapp: could not build the receipt caption")
        caption = f"{title} - درخواست #{request_id}"

    token = _owner_bot_token(db, pending.get("owner_admin_id"))
    targets = _receipt_targets(db, pending)
    if not targets:
        logger.warning("miniapp: receipt #%s has nobody to notify", request_id)
    for chat_id in targets:
        runner.send_photo_sync(
            chat_id, image, caption=caption, reply_markup=approval_kb(request_id), token=token
        )


def _notify_owner_of_sale(db: Session, owner: int | None, username: str, pkg, price: int) -> None:
    """A wallet purchase needs no approval, so without this the owner never
    hears about it at all - the first they would know of a sale is noticing
    the number move on a dashboard."""
    from ..telegram_bot import runner

    token = _owner_bot_token(db, owner)
    admin = db.get(models.AdminUser, owner) if owner else None
    if not admin or not admin.telegram_id:
        return
    runner.send_message_sync(
        admin.telegram_id,
        f"🛍 فروش جدید از مینی‌اپ\n\nکاربر: {username}\nپلن: {pkg.name}\nمبلغ: {price:,} تومان (از کیف پول)",
        token=token,
        parse_mode=None,
    )


class CheckoutRequest(BaseModel):
    package_id: int
    account: str | None = None
    comment: str | None = None


@router.post("/checkout")
def checkout(
    payload: CheckoutRequest,
    visitor: dict = Depends(current_visitor),
    db: Session = Depends(get_db),
):
    """Pay from the wallet. Refuses rather than half-succeeds.

    The order - debit first, provision second - is copied from
    customer.py's pay_with_balance and matters for the same reason: a
    failure after provisioning would be a free service, while a failure
    after the debit is recoverable, and is recovered (the refund below).
    """
    owner = visitor["owner_admin_id"]
    pkg = _package_or_404(db, owner, payload.package_id)
    _require_bundled(pkg)
    price = _price_of(pkg)

    account = _own_account(db, visitor, payload.account)

    # A free plan - the trial - is the one case where "you have no account"
    # is not a reason to refuse. The whole point of a sample is to reach
    # someone who has not signed up yet, and sending them back to the bot
    # to do it there would make the shop the long way round.
    #
    # Created through routers/bot.py's create_user, the same single choke
    # point the bot's own signup uses, so the trial's rules, the purchase
    # lock and the referral hook all apply exactly as they do in chat.
    if account is None and price == 0:
        created = bot_router.create_user(
            schemas.BotCreateUserRequest(
                username=f"tg{visitor['telegram_id']}",
                full_name=(visitor["user"].get("first_name") or "").strip() or None,
                quota_gb=float(getattr(pkg, "quota_gb", 0) or 0),
                expire_days=getattr(pkg, "duration_days", None),
                telegram_id=visitor["telegram_id"],
                connections=[
                    schemas.BotCreateConnectionSpec(
                        node_id=c.node_id, protocol=c.protocol, flow=getattr(c, "flow", "") or "",
                    )
                    for c in _connections_of(pkg)
                ],
                package_name=pkg.name,
                package_id=pkg.id,
                owner_admin_id=owner,
            ),
            db=db,
        )
        return {
            "status": "done",
            "message": "سرویس تست شما فعال شد. در «سرویس‌های من» ببینیدش.",
            "connections": [c.model_dump() for c in created.connections],
        }

    if account is None:
        raise HTTPException(400, "هنوز حسابی ندارید - اولین خرید را از داخل ربات انجام دهید.")
    if (account.balance or 0) < price:
        raise HTTPException(400, "موجودی کیف پول کافی نیست.")

    bot_router.add_balance(account.username, schemas.BotAddBalanceRequest(amount=-price), db=db)
    try:
        result = bot_router.purchase_package(
            account.username,
            schemas.BotPurchasePackageRequest(
                package_id=pkg.id,
                comment=payload.comment or None,
                paid_amount=price,
                payment_method="wallet",
            ),
            db=db,
            owner_admin_id=owner,
        )
    except Exception:
        # Put the money back. Without this the customer is charged for a
        # service that was never created, and finds out by comparing a
        # balance they were not watching.
        logger.exception("miniapp: purchase failed after debit - refunding %s", account.username)
        try:
            bot_router.add_balance(account.username, schemas.BotAddBalanceRequest(amount=price), db=db)
        except Exception:
            logger.exception("miniapp: REFUND FAILED for %s (%s tomans)", account.username, price)
        raise

    _notify_owner_of_sale(db, owner, account.username, pkg, price)
    return {
        "status": "done",
        "message": "خرید انجام شد. سرویس شما در «سرویس‌های من» است.",
        "connections": [c.model_dump() for c in result.connections],
    }


@router.post("/checkout/receipt")
async def checkout_receipt(
    package_id: int = Form(...),
    account: str | None = Form(None),
    comment: str | None = Form(None),
    photo: UploadFile = File(...),
    visitor: dict = Depends(current_visitor),
    db: Session = Depends(get_db),
):
    """Card-to-card: the receipt joins the queue the owner already works.

    Nothing is provisioned here. This writes the same pending_purchases row
    the bot writes (telegram_bot/storage.py) so it appears in the same
    «درخواست‌های در انتظار» list, with the same Approve/Reject buttons, and
    is approved by the same code. The Mini App deliberately gets no
    approval path of its own - a second queue is a second thing to forget
    to check.
    """
    from ..telegram_bot import storage

    owner = visitor["owner_admin_id"]
    pkg = _package_or_404(db, owner, package_id)
    _require_bundled(pkg)
    price = _price_of(pkg)

    image = await photo.read()
    if not image:
        raise HTTPException(400, "عکس رسید خوانده نشد.")
    if len(image) > 10 * 1024 * 1024:
        raise HTTPException(400, "حجم عکس بیش از حد است (حداکثر ۱۰ مگابایت).")

    account_row = _own_account(db, visitor, account)
    # Matches customer.py's own fallback for a customer with no account yet:
    # the account itself is created later, by the approval.
    target_username = account_row.username if account_row else f"tg{visitor['telegram_id']}"

    payment = bot_router.get_payment_info(owner_admin_id=owner, db=db)

    storage.init_db()
    request_id = storage.create_pending(
        telegram_id=visitor["telegram_id"],
        telegram_username=visitor["user"].get("username"),
        telegram_name=(visitor["user"].get("first_name") or "").strip(),
        kind="new",
        package={
            "id": pkg.id,
            "name": pkg.name,
            "quota_gb": getattr(pkg, "quota_gb", 0) or 0,
            "duration_days": getattr(pkg, "duration_days", None),
            "price": price,
        },
        target_username=target_username,
        final_price=price,
        payment_card_id=getattr(payment, "resolved_payment_card_id", None),
        comment=comment or None,
        # EXPLICIT, never the default. storage.create_pending falls back to
        # config.bot_owner_admin_id, and `config` is a threading.local whose
        # value belongs to whichever bot thread set it - this request is on
        # a web worker thread, where that value is meaningless. The receipt
        # would be filed under the wrong reseller, or none.
        owner_admin_id=owner,
    )

    # On a thread, not on the loop. Uploading a photo to Telegram over a
    # proxy takes seconds, and this endpoint is `async def` (it has to
    # await the upload) - so doing it inline would hold the whole event
    # loop, and every other request with it, for the duration.
    await asyncio.to_thread(_notify_receipt, db, storage.get_pending(request_id), request_id, image)
    return {
        "status": "pending",
        "request_id": request_id,
        "message": "رسید شما ثبت شد و برای بررسی ارسال شد. نتیجه در همین ربات اطلاع داده می‌شود.",
    }


@router.post("/topup/receipt")
async def topup_receipt(
    amount: int = Form(...),
    account: str | None = Form(None),
    photo: UploadFile = File(...),
    visitor: dict = Depends(current_visitor),
    db: Session = Depends(get_db),
):
    """Add credit to the wallet, by card-to-card receipt.

    The wallet tab used to show a card number under the heading «افزایش
    اعتبار» and stop there - a label where an action belongs, so there was
    nothing to press. This is the action.

    Identical in shape to checkout_receipt above, and deliberately writes
    the SAME row the bot's own top-up flow writes (kind="topup", the
    amount carried in a synthetic package with id 0 - see
    telegram_bot/handlers/customer.py's receive_topup_receipt). That is
    what makes it appear in «درخواست‌های در انتظار» and be approved by
    the existing code, which credits the wallet. Nothing here touches a
    balance: an unapproved receipt is a claim, not a payment.
    """
    from ..telegram_bot import storage

    owner = visitor["owner_admin_id"]

    # A bare `amount <= 0` would let 0 through as "free", and a huge number
    # through as a typo nobody catches until an admin approves it. The
    # ceiling is deliberately generous - it is a sanity bound, not a policy.
    if amount <= 0:
        raise HTTPException(400, "مبلغ را وارد کنید.")
    if amount > 500_000_000:
        raise HTTPException(400, "این مبلغ بیش از حد بزرگ است.")

    image = await photo.read()
    if not image:
        raise HTTPException(400, "عکس رسید خوانده نشد.")
    if len(image) > 10 * 1024 * 1024:
        raise HTTPException(400, "حجم عکس بیش از حد است (حداکثر ۱۰ مگابایت).")

    # Unlike a purchase, a top-up has nowhere to land without an account -
    # the approval credits models.User.balance, and there is no user yet to
    # credit. Said plainly rather than accepted and lost.
    account_row = _own_account(db, visitor, account)
    if account_row is None:
        raise HTTPException(400, "هنوز حسابی ندارید - اولین خرید را از داخل ربات انجام دهید.")

    payment = bot_router.get_payment_info(owner_admin_id=owner, db=db)

    storage.init_db()
    request_id = storage.create_pending(
        telegram_id=visitor["telegram_id"],
        telegram_username=visitor["user"].get("username"),
        telegram_name=(visitor["user"].get("first_name") or "").strip(),
        kind="topup",
        package={"id": 0, "name": f"افزایش اعتبار {amount:,} تومان",
                 "quota_gb": 0, "duration_days": None, "price": amount},
        target_username=account_row.username,
        final_price=amount,
        payment_card_id=getattr(payment, "resolved_payment_card_id", None),
        # Explicit, for the same reason as checkout_receipt: the default
        # reads a threading.local belonging to a bot thread.
        owner_admin_id=owner,
    )

    await asyncio.to_thread(
        _notify_receipt, db, storage.get_pending(request_id), request_id, image,
        "🧾 رسید افزایش اعتبار (از مینی‌اپ)",
    )
    return {
        "status": "pending",
        "request_id": request_id,
        "message": "رسید شما ثبت شد. پس از تأیید، اعتبار به کیف پول شما اضافه می‌شود.",
    }
