import logging
import uuid

from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from ..panel_bridge import api, ApiError
from ..callbacks import MenuCB, ApprovalCB
from ..config import config
from ..keyboards import approval_kb, home_kb
from .. import storage
from .customer import send_package_extras
from ..connection_sender import send_connections

logger = logging.getLogger("telegram_bot")

router = Router(name="admin_pending")
# Router-level filter, same pattern as admin_users.py/admin_broadcast.py -
# every handler below already double-checks is_admin() individually too,
# but this makes the "customers can never reach admin actions in this
# router" guarantee explicit and structural instead of relying on every
# handler remembering its own check.
router.message.filter(lambda m: config.is_admin(m.from_user.id))
router.callback_query.filter(lambda c: config.is_admin(c.from_user.id))


def _pending_summary(p: dict) -> str:
    who = f"@{p['telegram_username']}" if p.get("telegram_username") else p.get("telegram_name") or str(p["telegram_id"])
    kind_txt = {"new": "خرید جدید", "renew": "تمدید", "topup": "افزایش اعتبار", "link": "اتصال حساب قبلی"}.get(p["kind"], p["kind"])
    lines = [
        f"#{p['id']} — {kind_txt}",
        f"مشتری: {who} (<code>{p['telegram_id']}</code>)",
    ]
    if p["kind"] == "link":
        lines.append(f"می‌خواهد به حساب «{p['target_username']}» وصل شود.")
    elif p["kind"] == "topup":
        lines.append(f"مبلغ: {p['price']:,} تومان")
        lines.append(f"حساب مقصد: {p['target_username']}")
    else:
        lines.append(f"پکیج: {p['package_name']} — {p['quota_gb'] or 'نامحدود'}GB / {p['duration_days'] or '∞'} روز")
        if p.get("discount_amount"):
            lines.append(f"مبلغ: <s>{p['price']:,}</s> {p.get('final_price', p['price']):,} تومان (🎟 کد {p.get('discount_code')} — {p['discount_amount']:,} تومان تخفیف)")
        else:
            lines.append(f"مبلغ: {p['price']:,} تومان")
        if p.get("referral_code"):
            lines.append(f"🎁 کد دعوت وارد شده: {p['referral_code']}")
        if p["kind"] == "new":
            if p.get("node_name"):
                lines.append(f"سرور/پروتکل: {p['node_name']} / {p['protocol']}")
            else:
                lines.append("سرویس‌ها: طبق پکیج (چند سرویس همراه)")
        else:
            lines.append(f"حساب مقصد: {p['target_username']}")
    return "\n".join(lines)


@router.callback_query(MenuCB.filter(F.action == "admin_pending"), lambda c: config.is_admin(c.from_user.id))
async def cb_admin_pending(call: CallbackQuery) -> None:
    items = storage.list_pending()
    if not items:
        await call.message.edit_text("درخواست در انتظاری وجود ندارد.", reply_markup=home_kb())
        await call.answer()
        return
    await call.message.edit_text(f"📥 {len(items)} درخواست در انتظار تایید:", reply_markup=home_kb())
    for p in items:
        await call.message.answer(_pending_summary(p), reply_markup=approval_kb(p["id"]))
    await call.answer()


@router.message(Command("pending"))
async def cmd_admin_pending(message: Message) -> None:
    """Slash-command shortcut for "📥 درخواست‌های در انتظار"."""
    if not config.is_admin(message.from_user.id):
        return
    items = storage.list_pending()
    if not items:
        await message.answer("درخواست در انتظاری وجود ندارد.", reply_markup=home_kb())
        return
    await message.answer(f"📥 {len(items)} درخواست در انتظار تایید:", reply_markup=home_kb())
    for p in items:
        await message.answer(_pending_summary(p), reply_markup=approval_kb(p["id"]))


@router.callback_query(ApprovalCB.filter())
async def cb_approval(call: CallbackQuery, callback_data: ApprovalCB, bot: Bot) -> None:
    if not config.is_admin(call.from_user.id):
        await call.answer("دسترسی ندارید", show_alert=True)
        return

    pending = storage.get_pending(callback_data.request_id)
    if not pending or pending["status"] != "pending":
        await call.answer("این درخواست قبلا رسیدگی شده است", show_alert=True)
        return

    # Atomically claim this request before doing anything else - if two
    # admins tap Approve/Reject on the same request at nearly the same time
    # (or one admin double-taps on a slow connection), only the first claim
    # wins; the loser gets a clean "already handled" instead of both
    # provisioning/crediting the customer twice. See storage.claim_pending.
    if not storage.claim_pending(pending["id"]):
        await call.answer("این درخواست همین الان توسط ادمین دیگری در حال بررسی است", show_alert=True)
        return

    async def _finish(result_text: str):
        if call.message.photo:
            await call.message.edit_caption(caption=(call.message.caption or "") + "\n\n" + result_text)
        else:
            await call.message.edit_text((call.message.text or "") + "\n\n" + result_text)

    if callback_data.action == "reject":
        storage.set_status(pending["id"], "rejected")
        await _finish("❌ رد شد.")
        reject_msg = (
            "متاسفانه درخواست اتصال حساب شما تایید نشد. اگر واقعا صاحب آن حساب هستید، برای پیگیری با پشتیبانی در تماس باشید."
            if pending["kind"] == "link"
            else "متاسفانه پرداخت شما تایید نشد. برای پیگیری با پشتیبانی در تماس باشید."
        )
        try:
            await bot.send_message(pending["telegram_id"], reject_msg, reply_markup=home_kb())
        except Exception:
            pass
        await call.answer("رد شد")
        return

    # approve
    pkg = None
    if pending["kind"] in ("new", "renew"):
        # Look the package back up in full - only its id/name/quota/price
        # were snapshotted into the pending row, but "new" purchases of a
        # bundled-services package need the connections list, and both
        # kinds need custom_message/files for the post-purchase extras sent
        # below.
        try:
            packages = await api.list_packages()
        except ApiError as exc:
            storage.release_pending(pending["id"])
            await call.answer(f"خطا: {exc}", show_alert=True)
            return
        pkg = next((p for p in packages if p["id"] == pending["package_id"]), None)

    new_connections = None
    try:
        if pending["kind"] == "new":
            if pending["node_id"]:
                # plain package - single manually-picked service
                connections = [{"node_id": pending["node_id"], "protocol": pending["protocol"]}]
            else:
                # package with bundled services (see customer.py pick_package)
                connections = [
                    {"node_id": c["node_id"], "protocol": c["protocol"], "flow": c.get("flow") or ""}
                    for c in ((pkg or {}).get("connections") or [])
                ]

            # A "new" purchase request can come from a customer who ALREADY
            # has a linked account (buying an additional package on top of
            # what they have - see customer.py's _ask_for_receipt, which
            # sets target_username to their existing username whenever
            # they're linked, regardless of "new" vs "renew"). create_user
            # below would reject that with "username already exists", so
            # check first and, if they already exist, add this package's
            # services to their existing account instead (mirrors exactly
            # what pay_with_balance's instant-checkout "new" path does).
            existing_user = None
            try:
                existing_user = await api.get_user(pending["target_username"])
            except ApiError:
                existing_user = None

            user = None
            if existing_user:
                batch = uuid.uuid4().hex
                new_connections = []
                for c in connections:
                    conn = await api.add_connection(
                        pending["target_username"], c["node_id"], c["protocol"], flow=c.get("flow") or "",
                        purchase_batch=batch, package_name=(pkg or {}).get("name"),
                    )
                    new_connections.append(conn)
                if pending["quota_gb"] or pending["duration_days"]:
                    user = await api.renew(
                        pending["target_username"],
                        add_gb=pending["quota_gb"] or 0,
                        add_days=pending["duration_days"] or 0,
                        package_id=(pkg or {}).get("id"),
                    )
            else:
                user = await api.create_user(
                    username=pending["target_username"],
                    full_name=pending.get("telegram_name") or pending.get("telegram_username"),
                    quota_gb=pending["quota_gb"],
                    expire_days=pending["duration_days"],
                    telegram_id=pending["telegram_id"],
                    connections=connections,
                    package_name=(pkg or {}).get("name"),
                    package_id=(pkg or {}).get("id"),
                )
                new_connections = user["connections"]
                # Brand-new account - this is the ONE choke point new
                # customers are created through (see customer.py's purchase
                # flow), so it's the only place a referral code can ever be
                # redeemed. Best-effort: a bad/expired code shouldn't block
                # the purchase that already succeeded.
                if pending.get("referral_code"):
                    try:
                        await api.apply_referral(pending["target_username"], pending["referral_code"])
                    except ApiError:
                        pass
            if pending.get("discount_code"):
                try:
                    await api.redeem_discount(pending["discount_code"], pending["target_username"], pending["price"])
                except ApiError:
                    pass
            customer_msg = f"✅ پرداخت شما تایید شد!\n\nنام کاربری: <code>{pending['target_username']}</code>"
            if user and (user.get("loyalty_reward_credit") or user.get("loyalty_reward_gb")):
                from .customer import _loyalty_reward_text  # local import avoids a circular import at module load
                customer_msg += "\n\n" + _loyalty_reward_text(user)
        elif pending["kind"] == "renew":
            user = await api.renew(
                pending["target_username"], add_gb=pending["quota_gb"], add_days=pending["duration_days"],
                package_id=(pkg or {}).get("id"),
            )
            if pending.get("discount_code"):
                try:
                    await api.redeem_discount(pending["discount_code"], pending["target_username"], pending["price"])
                except ApiError:
                    pass
            customer_msg = f"✅ پرداخت شما تایید شد و حساب «{pending['target_username']}» تمدید شد."
            if user.get("loyalty_reward_credit") or user.get("loyalty_reward_gb"):
                from .customer import _loyalty_reward_text  # local import avoids a circular import at module load
                customer_msg += "\n\n" + _loyalty_reward_text(user)
        elif pending["kind"] == "link":
            # Security-gated version of the old instant customer.py
            # link_telegram call - only actually links once an admin has
            # visually confirmed this Telegram person really owns the
            # target account, since a username alone proves nothing.
            await api.link_telegram(pending["target_username"], pending["telegram_id"])
            customer_msg = f"✅ درخواست شما تایید شد. حساب «{pending['target_username']}» به شما وصل شد."
        else:  # topup
            user = await api.add_balance(pending["target_username"], pending["price"])
            customer_msg = (
                f"✅ پرداخت شما تایید شد و {pending['price']:,} تومان به اعتبار حساب «{pending['target_username']}» اضافه شد.\n"
                f"موجودی فعلی: {user['balance']:,} تومان."
            )
    except ApiError as exc:
        storage.release_pending(pending["id"])
        await call.answer(f"خطا: {exc}", show_alert=True)
        return

    storage.set_status(pending["id"], "approved")
    await _finish("✅ تایید و فعال‌سازی شد.")
    # customer_msg intentionally has no keyboard here - if connections/files
    # follow it below, THEY become the last messages in the chat, so the
    # "🏠 منوی اصلی" button has to live on whatever truly is the last
    # message sent (see the unconditional trailing send below), otherwise
    # the customer is left with no visible buttons at the bottom of the chat.
    try:
        await bot.send_message(pending["telegram_id"], customer_msg)
    except Exception:
        pass
    if new_connections:
        await send_connections(bot, pending["telegram_id"], new_connections)
    if pkg:
        await send_package_extras(bot, pending["telegram_id"], pkg)
    try:
        await bot.send_message(pending["telegram_id"], "🏠 منو:", reply_markup=home_kb())
    except Exception:
        pass
    await call.answer("تایید شد")
