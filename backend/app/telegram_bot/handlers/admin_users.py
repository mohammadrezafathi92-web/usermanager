from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from ..panel_bridge import api, ApiError
from ..callbacks import (
    MenuCB, AdminListPageCB, AdminUserCB, AdminServiceCB, AdminPkgPickCB,
    AdminCreatePkgCB, AdminRenewPkgCB, NodeCB, ProtocolCB,
)
from ..admin_scope import resolve_admin_scope
from ..keyboards import (
    admin_users_list_kb,
    admin_user_detail_kb,
    admin_services_kb,
    admin_packages_kb,
    admin_create_packages_kb,
    admin_renew_packages_kb,
    confirm_delete_kb,
    nodes_kb,
    protocols_kb,
    home_kb,
    cancel_kb,
)
from ..states import AdminCreateUserStates, AdminRenewStates, AdminSearchStates, AdminBalanceStates
from ..utils import fmt_bytes, fmt_date, STATUS_LABELS
from ..connection_sender import send_connections

router = Router(name="admin_users")


async def _admin_scope_filter(event):
    """Lets BOTH a full/config bot admin AND a linked group-admin (see
    ../admin_scope.py) through - injects the resolved scope as
    `acting_scope` into every handler below, which threads its
    `owner_admin_id` through every api call so a group-admin only ever
    sees/touches their own group's users. A regular customer (scope is
    None) never reaches this router at all."""
    scope = await resolve_admin_scope(event.from_user.id)
    if not scope:
        return False
    return {"acting_scope": scope}


router.message.filter(_admin_scope_filter)
router.callback_query.filter(_admin_scope_filter)


def _user_detail_text(user: dict, purchases: list[dict] | None = None) -> str:
    """Per-SERVICE view (fixed 2026-08-10): each purchase carries its own
    quota/expiry now (see models.Purchase), and the user-level
    used_bytes/expire_at stopped moving for purchase-linked connections
    after the migration - showing only those made the bot display a frozen,
    misleading number. The user line is kept for the wallet/telegram info
    plus any legacy connections still on the shared pool."""
    lines = [
        f"👤 <b>{user['username']}</b>",
        f"وضعیت: {STATUS_LABELS.get(user['status'], user['status'])}",
        f"موجودی اعتبار: {user.get('balance', 0):,} تومان",
    ]
    if user.get("telegram_id"):
        lines.append(f"تلگرام: <code>{user['telegram_id']}</code>")

    conns = user.get("connections") or []
    legacy = [c for c in conns if not c.get("purchase_batch")]

    if purchases:
        lines.append(f"\n<b>سرویس‌ها ({len(purchases)}):</b>")
        for p in purchases:
            name = p.get("package_name_snapshot") or "سرویس"
            quota = fmt_bytes(p["quota_bytes"]) if p.get("quota_bytes") else "نامحدود"
            used = fmt_bytes(p.get("used_bytes") or 0)
            status = STATUS_LABELS.get(p.get("status"), p.get("status"))
            lines.append(f"\n• <b>{name}</b> — {status}")
            lines.append(f"  مصرف: {used} / {quota}")
            lines.append(f"  انقضا: {fmt_date(p.get('expire_at'))}")
            if p.get("comment"):
                lines.append(f"  📝 {p['comment']}")
            if p.get("reserved_quota_bytes") or p.get("reserved_duration_days"):
                parts = []
                if p.get("reserved_quota_bytes"):
                    parts.append(fmt_bytes(p["reserved_quota_bytes"]))
                if p.get("reserved_duration_days"):
                    parts.append(f"{p['reserved_duration_days']} روز")
                lines.append("  ⏳ تمدید رزروشده: " + " + ".join(parts))
            lines.append(f"  اتصالات: {p.get('connection_count', 0)}")

    if legacy:
        # Only meaningful while the customer still has connections outside
        # any Purchase - that's what the user-level pool still governs.
        lines.append("\n<b>سرویس اشتراکی (قدیمی):</b>")
        lines.append(
            f"  مصرف: {fmt_bytes(user['used_bytes'])} / "
            f"{fmt_bytes(user['total_quota_bytes']) if user['total_quota_bytes'] else 'نامحدود'}"
        )
        lines.append(f"  انقضا: {fmt_date(user.get('expire_at'))}")
        lines.append(f"  اتصالات: {len(legacy)}")

    if not conns:
        lines.append("\nهنوز هیچ اتصالی ندارد.")
    return "\n".join(lines)


# ------------------------------------------------------------- create user
@router.callback_query(MenuCB.filter(F.action == "admin_create"))
async def cb_admin_create(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminCreateUserStates.waiting_username)
    await call.message.edit_text("نام کاربری برای حساب جدید را بفرستید:", reply_markup=cancel_kb())
    await call.answer()


@router.message(Command("newuser"))
async def cmd_admin_create(message: Message, state: FSMContext) -> None:
    """Slash-command shortcut for "➕ ساخت کاربر"."""
    await state.clear()
    await state.set_state(AdminCreateUserStates.waiting_username)
    await message.answer("نام کاربری برای حساب جدید را بفرستید:", reply_markup=cancel_kb())


@router.message(Command("users"))
async def cmd_admin_list(message: Message, state: FSMContext, acting_scope: dict) -> None:
    """Slash-command shortcut for "📋 لیست کاربران". Registered here, before
    any of this router's state catch-all handlers (waiting_username,
    picking_package, ...) so it always works as an escape hatch mid-flow -
    see customer.py's matching comment for the full rationale."""
    await state.clear()
    await _show_user_list(message, page=1, search=None, owner_admin_id=acting_scope["owner_admin_id"])


# The package comes FIRST, and the servers come out of the package.
#
# The old order was username -> server -> protocol -> package, which asked
# the admin to hand-pick one node and one protocol and then pick a package
# anyway - so a package that bundles WireGuard + OpenVPN + Xray (perfectly
# ordinary in «پکیج‌ها») produced a user with exactly ONE of them, whichever
# the admin happened to tap, while the panel's own ساخت کاربر form and the
# customer purchase flow both provisioned the whole bundle. Same panel, same
# package, two different accounts depending on where it was created.
# Reported 2026-09-12: "چرا ساخت کاربر در ادمین به این صورته؟ باید یه روند
# درست مثل پنل داشته باشه و از پکیج‌ها استفاده بشه".
#
# Now it mirrors routers/users.py's create_user and customer.py's
# pick_package exactly: pick the package, and if it bundles servers those
# are what gets built. The node/protocol questions survive only as the
# fallback for a "plain" package that bundles nothing - without them such a
# package would create an account with no service at all.
@router.message(AdminCreateUserStates.waiting_username)
async def admin_create_username(message: Message, state: FSMContext, acting_scope: dict) -> None:
    username = (message.text or "").strip()
    if not username or " " in username:
        await message.answer("نام کاربری معتبر نیست (بدون فاصله بفرستید). دوباره تلاش کنید:")
        return
    try:
        packages = await api.list_packages(owner_admin_id=acting_scope["owner_admin_id"])
    except ApiError as exc:
        await message.answer(f"خطا: {exc}", reply_markup=home_kb())
        await state.clear()
        return
    if not packages:
        await message.answer("هیچ پکیجی تعریف نشده است - ابتدا از پنل یک پکیج بسازید.", reply_markup=home_kb())
        await state.clear()
        return
    await state.update_data(new_username=username, packages={str(p["id"]): p for p in packages})
    await state.set_state(AdminCreateUserStates.picking_package)
    await message.answer(
        f"کاربر «{username}» با کدام پکیج ساخته شود؟", reply_markup=admin_create_packages_kb(packages)
    )


def _package_connection_specs(package: dict) -> list[dict]:
    """The server+protocol combos the admin bundled into this package in
    «پکیج‌ها» - the same list admin_pending.py builds when approving a
    customer's receipt, so a user created by an admin and one bought by a
    customer end up with identical services."""
    return [
        {"node_id": c["node_id"], "protocol": c["protocol"], "flow": c.get("flow") or ""}
        for c in (package.get("connections") or [])
    ]


async def _finish_admin_create(call: CallbackQuery, state: FSMContext, acting_scope: dict, bot,
                               package: dict, connections: list[dict]) -> None:
    data = await state.get_data()
    try:
        user = await api.create_user(
            username=data["new_username"],
            quota_gb=package.get("quota_gb") or 0,
            expire_days=package.get("duration_days") or None,
            connections=connections,
            owner_admin_id=acting_scope["owner_admin_id"],
            package_id=package["id"],
            package_name=package.get("name"),
        )
    except ApiError as exc:
        await call.message.edit_text(f"خطا در ساخت کاربر: {exc}", reply_markup=home_kb())
        await state.clear()
        return
    await state.clear()
    await call.message.edit_text("✅ کاربر ساخته شد:\n\n" + _user_detail_text(user), reply_markup=home_kb())
    if user.get("connections"):
        await send_connections(bot, call.from_user.id, user["connections"])


@router.callback_query(AdminCreatePkgCB.filter(), AdminCreateUserStates.picking_package)
async def admin_create_pick_package(call: CallbackQuery, callback_data: AdminCreatePkgCB, state: FSMContext, acting_scope: dict, bot) -> None:
    data = await state.get_data()
    packages = data.get("packages") or {}
    package = packages.get(str(callback_data.package_id)) or packages.get(callback_data.package_id)
    if not package:
        await call.answer("این پکیج دیگر در دسترس نیست.", show_alert=True)
        return
    await state.update_data(package_id=package["id"])

    specs = _package_connection_specs(package)
    if specs:
        await call.answer("در حال ساخت کاربر...")
        await _finish_admin_create(call, state, acting_scope, bot, package, specs)
        return

    # Plain package, no bundled services - fall back to asking, same as
    # customer.py's pick_package does for exactly this case.
    try:
        nodes = await api.list_nodes()
    except ApiError as exc:
        await call.message.edit_text(f"خطا: {exc}", reply_markup=home_kb())
        await state.clear()
        await call.answer()
        return
    if not nodes:
        await call.message.edit_text(
            "این پکیج هیچ سرویسی داخلش تعریف نشده و هیچ سروری هم در پنل نیست - "
            "یا از «پکیج‌ها» سرویس‌های پکیج را مشخص کنید یا یک سرور اضافه کنید.",
            reply_markup=home_kb(),
        )
        await state.clear()
        await call.answer()
        return
    await state.update_data(nodes={str(n["id"]): n for n in nodes})
    await state.set_state(AdminCreateUserStates.picking_node)
    await call.message.edit_text(
        f"پکیج: {package.get('name') or '-'}\n"
        "این پکیج سرویس از پیش‌تعریف‌شده ندارد - روی کدام سرور ساخته شود؟",
        reply_markup=nodes_kb(nodes),
    )
    await call.answer()


@router.callback_query(NodeCB.filter(), AdminCreateUserStates.picking_node)
async def admin_pick_node(call: CallbackQuery, callback_data: NodeCB, state: FSMContext) -> None:
    data = await state.get_data()
    nodes = data.get("nodes") or {}
    node = nodes.get(str(callback_data.node_id)) or nodes.get(callback_data.node_id)
    if not node:
        await call.answer("سرور پیدا نشد", show_alert=True)
        return
    await state.update_data(node_id=node["id"], node_name=node["name"])
    await state.set_state(AdminCreateUserStates.picking_protocol)
    await call.message.edit_text(f"سرور: {node['name']}\nپروتکل را انتخاب کنید:", reply_markup=protocols_kb(node["type"]))
    await call.answer()


@router.callback_query(ProtocolCB.filter(), AdminCreateUserStates.picking_protocol)
async def admin_pick_protocol(call: CallbackQuery, callback_data: ProtocolCB, state: FSMContext, acting_scope: dict, bot) -> None:
    data = await state.get_data()
    packages = data.get("packages") or {}
    package = packages.get(str(data.get("package_id"))) or packages.get(data.get("package_id"))
    if not package:
        await call.message.edit_text("این پکیج دیگر در دسترس نیست.", reply_markup=home_kb())
        await state.clear()
        await call.answer()
        return
    await call.answer("در حال ساخت کاربر...")
    await _finish_admin_create(
        call, state, acting_scope, bot, package,
        [{"node_id": data["node_id"], "protocol": callback_data.protocol}],
    )


# --------------------------------------------------------------- list/search
async def _show_user_list(target, page: int, search: str | None, owner_admin_id: int | None) -> None:
    try:
        result = await api.list_users(page=page, search=search, owner_admin_id=owner_admin_id)
    except ApiError as exc:
        await target.answer(f"خطا: {exc}")
        return
    items = result["items"]
    label = "کاربران" if owner_admin_id is None else "کاربران من"
    text = f"📋 {label} ({result['total']} نفر)" if not search else f"نتایج جستجو برای «{search}» ({result['total']})"
    if not items:
        text = "کاربری پیدا نشد."
    kb = admin_users_list_kb(items, page, result["total"], search)
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=kb)
    else:
        await target.answer(text, reply_markup=kb)


@router.callback_query(MenuCB.filter(F.action == "admin_list"))
async def cb_admin_list(call: CallbackQuery, state: FSMContext, acting_scope: dict) -> None:
    await state.clear()
    await _show_user_list(call, page=1, search=None, owner_admin_id=acting_scope["owner_admin_id"])
    await call.answer()


@router.callback_query(AdminListPageCB.filter())
async def cb_admin_list_page(call: CallbackQuery, callback_data: AdminListPageCB, acting_scope: dict) -> None:
    search = None if callback_data.search == "-" else callback_data.search
    await _show_user_list(call, page=callback_data.page, search=search, owner_admin_id=acting_scope["owner_admin_id"])
    await call.answer()


# ------------------------------------------------------------- user detail

def _is_panel_owner(acting_scope: dict) -> bool:
    """Only the superadmin is never charged for anything - see
    _show_user_detail's allow_reset."""
    return (acting_scope or {}).get("role") == "superadmin"

async def _show_user_detail(target, username: str, owner_admin_id: int | None,
                            allow_reset: bool = False) -> None:
    """allow_reset: show the «ریست مصرف» button. Off unless the caller is
    the panel owner - zeroing a used-up quota hands the whole thing back,
    which is selling it again, and the bot has no package picker on that
    button to charge it against (the panel does - see
    admin_billing.require_package_to_grant). A reseller uses «تمدید سرویس»
    here instead, which has always required a package."""
    try:
        user = await api.get_user(username, owner_admin_id=owner_admin_id)
    except ApiError as exc:
        await target.answer(f"خطا: {exc}")
        return
    try:
        purchases = await api.list_purchases(username, owner_admin_id=owner_admin_id)
    except ApiError:
        purchases = []
    kb = admin_user_detail_kb(username, user["status"] == "active", allow_reset=allow_reset)
    text = _user_detail_text(user, purchases)
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=kb)
    else:
        await target.answer(text, reply_markup=kb)


@router.callback_query(AdminUserCB.filter(F.action == "view"))
async def cb_user_view(call: CallbackQuery, callback_data: AdminUserCB, state: FSMContext, acting_scope: dict) -> None:
    await state.clear()
    await _show_user_detail(call, callback_data.username, acting_scope["owner_admin_id"], allow_reset=_is_panel_owner(acting_scope))
    await call.answer()


@router.callback_query(AdminUserCB.filter(F.action == "toggle"))
async def cb_user_toggle(call: CallbackQuery, callback_data: AdminUserCB, acting_scope: dict) -> None:
    owner_admin_id = acting_scope["owner_admin_id"]
    try:
        user = await api.get_user(callback_data.username, owner_admin_id=owner_admin_id)
        await api.set_enabled(callback_data.username, user["status"] != "active", owner_admin_id=owner_admin_id)
    except ApiError as exc:
        await call.answer(f"خطا: {exc}", show_alert=True)
        return
    await _show_user_detail(call, callback_data.username, owner_admin_id, allow_reset=_is_panel_owner(acting_scope))
    await call.answer("انجام شد")


@router.callback_query(AdminUserCB.filter(F.action == "resetusage"))
async def cb_user_reset(call: CallbackQuery, callback_data: AdminUserCB, acting_scope: dict) -> None:
    owner_admin_id = acting_scope["owner_admin_id"]
    # Hiding the button is not enough - an old message still carries a live
    # callback. See _show_user_detail's allow_reset for why this costs money.
    if not _is_panel_owner(acting_scope):
        await call.answer(
            "صفر کردن مصرف یعنی دوباره دادن همان حجم، پس باید از «♻️ تمدید سرویس» "
            "با انتخاب پکیج انجام شود تا هزینه‌اش حساب شود.",
            show_alert=True,
        )
        return
    try:
        await api.reset_usage(callback_data.username, owner_admin_id=owner_admin_id)
    except ApiError as exc:
        await call.answer(f"خطا: {exc}", show_alert=True)
        return
    await _show_user_detail(call, callback_data.username, owner_admin_id, allow_reset=_is_panel_owner(acting_scope))
    await call.answer("مصرف ریست شد")


@router.callback_query(AdminUserCB.filter(F.action == "delete"))
async def cb_user_delete_ask(call: CallbackQuery, callback_data: AdminUserCB) -> None:
    await call.message.edit_text(
        f"از حذف کامل کاربر «{callback_data.username}» مطمئن هستید؟", reply_markup=confirm_delete_kb(callback_data.username)
    )
    await call.answer()


@router.callback_query(AdminUserCB.filter(F.action == "delete_confirm"))
async def cb_user_delete_confirm(call: CallbackQuery, callback_data: AdminUserCB, acting_scope: dict) -> None:
    try:
        await api.delete_user(callback_data.username, owner_admin_id=acting_scope["owner_admin_id"])
    except ApiError as exc:
        await call.answer(f"خطا: {exc}", show_alert=True)
        return
    await call.message.edit_text(f"🗑 کاربر «{callback_data.username}» حذف شد.", reply_markup=home_kb())
    await call.answer("حذف شد")


# ------------------------------------------------------------------ renew
# تمدید = ادامه‌ی همان سرویس (same connections/credentials, new package
# queued behind what's left - see routers/bot.py's renew_service). The admin
# picks WHICH service first whenever the customer has more than one; with
# exactly one it's auto-targeted, and a customer still on the legacy shared
# pool falls through to the old user-level renew.
@router.callback_query(AdminUserCB.filter(F.action == "renew"))
async def cb_user_renew_ask(call: CallbackQuery, callback_data: AdminUserCB, state: FSMContext, acting_scope: dict) -> None:
    username = callback_data.username
    try:
        purchases = await api.list_purchases(username, owner_admin_id=acting_scope["owner_admin_id"])
    except ApiError:
        purchases = []

    if len(purchases) > 1:
        await call.message.edit_text(
            "کدام سرویس تمدید شود؟",
            reply_markup=admin_services_kb(username, purchases, action="renew"),
        )
        await call.answer()
        return

    await _ask_renew_package(call.message, state, username, purchases[0]["id"] if len(purchases) == 1 else None, acting_scope)
    await call.answer()


@router.callback_query(AdminServiceCB.filter(F.action == "renew"))
async def cb_service_renew_ask(call: CallbackQuery, callback_data: AdminServiceCB, state: FSMContext, acting_scope: dict) -> None:
    await _ask_renew_package(call.message, state, callback_data.username, callback_data.purchase_id, acting_scope)
    await call.answer()


async def _ask_renew_package(target_message, state: FSMContext, username: str, purchase_id: int | None, acting_scope: dict) -> None:
    # Package-only from here on (2026-09-08): the admin bot used to ask the
    # admin to type "<add_gb> <add_days>" free-text (plus a "send just 3"
    # escape hatch to reset usage, redundant with the dedicated "🔄 ریست
    # مصرف" button on the user's own detail screen). Renewal now always
    # picks one of the panel's own packages, same as creating a user.
    try:
        packages = await api.list_packages(owner_admin_id=acting_scope["owner_admin_id"])
    except ApiError as exc:
        await target_message.edit_text(f"خطا: {exc}", reply_markup=home_kb())
        return
    if not packages:
        await target_message.edit_text("هیچ پکیجی تعریف نشده است - ابتدا از پنل یک پکیج بسازید.", reply_markup=home_kb())
        return
    await state.set_state(AdminRenewStates.picking_package)
    await state.update_data(username=username, purchase_id=purchase_id)
    await target_message.edit_text(
        "کدام پکیج برای تمدید این سرویس اضافه شود؟",
        reply_markup=admin_renew_packages_kb(packages, username),
    )


@router.callback_query(AdminRenewPkgCB.filter(), AdminRenewStates.picking_package)
async def admin_renew_pick_package(call: CallbackQuery, callback_data: AdminRenewPkgCB, state: FSMContext, acting_scope: dict) -> None:
    data = await state.get_data()
    username = data["username"]
    purchase_id = data.get("purchase_id")
    await call.answer("در حال تمدید...")
    try:
        packages = await api.list_packages(owner_admin_id=acting_scope["owner_admin_id"])
    except ApiError as exc:
        await call.message.edit_text(f"خطا: {exc}", reply_markup=home_kb())
        await state.clear()
        return
    package = next((p for p in packages if p["id"] == callback_data.package_id), None)
    if not package:
        await call.message.edit_text("این پکیج دیگر در دسترس نیست.", reply_markup=home_kb())
        await state.clear()
        return
    add_gb = package.get("quota_gb") or 0
    add_days = package.get("duration_days") or 0
    try:
        if purchase_id:
            await api.renew_service(
                username, purchase_id, add_gb=add_gb, add_days=add_days,
                owner_admin_id=acting_scope["owner_admin_id"], package_id=package["id"],
            )
        else:
            await api.renew(
                username, add_gb=add_gb, add_days=add_days,
                owner_admin_id=acting_scope["owner_admin_id"], package_id=package["id"],
            )
    except ApiError as exc:
        await call.message.edit_text(f"خطا: {exc}", reply_markup=home_kb())
        await state.clear()
        return
    await state.clear()
    await call.message.edit_text("✅ سرویس تمدید شد.")
    await _show_user_detail(call.message, username, acting_scope["owner_admin_id"], allow_reset=_is_panel_owner(acting_scope))


# ------------------------------------------------------- افزودن پکیج
# Admin-side counterpart of the panel's «افزودن پکیج» - gives an EXISTING
# customer a brand-new, independently-enforced service (see
# user_ops.apply_package_as_purchase) rather than merging into what they
# already have.
@router.callback_query(AdminUserCB.filter(F.action == "addpkg"))
async def cb_user_add_package(call: CallbackQuery, callback_data: AdminUserCB, acting_scope: dict) -> None:
    try:
        packages = await api.list_packages(owner_admin_id=acting_scope["owner_admin_id"])
    except ApiError as exc:
        await call.answer(f"خطا: {exc}", show_alert=True)
        return
    if not packages:
        await call.answer("پکیجی تعریف نشده است", show_alert=True)
        return
    await call.message.edit_text(
        f"کدام پکیج به «{callback_data.username}» اضافه شود؟",
        reply_markup=admin_packages_kb(callback_data.username, packages),
    )
    await call.answer()


@router.callback_query(AdminPkgPickCB.filter())
async def cb_add_package_pick(call: CallbackQuery, callback_data: AdminPkgPickCB, acting_scope: dict, bot) -> None:
    await call.answer("در حال ساخت سرویس...")
    try:
        result = await api.purchase_package(
            callback_data.username, callback_data.package_id,
            owner_admin_id=acting_scope["owner_admin_id"],
        )
    except ApiError as exc:
        await call.message.edit_text(f"خطا: {exc}", reply_markup=home_kb())
        return
    await call.message.edit_text("✅ پکیج اضافه شد.")
    # Hand the admin the new configs right away - almost always the next
    # thing they need in order to pass them on to the customer.
    if result.get("connections"):
        await send_connections(bot, call.from_user.id, result["connections"])
    await _show_user_detail(call.message, callback_data.username, acting_scope["owner_admin_id"], allow_reset=_is_panel_owner(acting_scope))


# ------------------------------------------------------- اعتبار کیف پول
@router.callback_query(AdminUserCB.filter(F.action == "balance"))
async def cb_user_balance_ask(call: CallbackQuery, callback_data: AdminUserCB, state: FSMContext) -> None:
    await state.set_state(AdminBalanceStates.waiting_amount)
    await state.update_data(username=callback_data.username)
    await call.message.edit_text(
        "مبلغ را به تومان بفرستید.\n\n"
        "عدد مثبت = افزایش اعتبار، عدد منفی = کسر اعتبار.\n"
        "مثلا: <code>50000</code> یا <code>-20000</code>",
        reply_markup=cancel_kb(),
    )
    await call.answer()


@router.message(AdminBalanceStates.waiting_amount, F.text)
async def admin_balance_amount(message: Message, state: FSMContext, acting_scope: dict) -> None:
    data = await state.get_data()
    username = data["username"]
    try:
        amount = int((message.text or "").strip().replace(",", ""))
    except ValueError:
        await message.answer("فقط عدد بفرستید. مثلا: 50000")
        return
    if amount == 0:
        await message.answer("مبلغ نمی‌تواند صفر باشد.")
        return
    try:
        user = await api.add_balance(username, amount)
    except ApiError as exc:
        await message.answer(f"خطا: {exc}", reply_markup=home_kb())
        await state.clear()
        return
    await state.clear()
    verb = "اضافه شد به" if amount > 0 else "کسر شد از"
    await message.answer(
        f"✅ {abs(amount):,} تومان {verb} کیف پول «{username}».\n"
        f"موجودی فعلی: {user.get('balance', 0):,} تومان"
    )
    await _show_user_detail(message, username, acting_scope["owner_admin_id"], allow_reset=_is_panel_owner(acting_scope))


# ------------------------------------------------- ارسال مجدد کانفیگ
@router.callback_query(AdminUserCB.filter(F.action == "sendcfg"))
async def cb_user_send_configs(call: CallbackQuery, callback_data: AdminUserCB, acting_scope: dict, bot) -> None:
    """Re-sends every config this customer has - to the CUSTOMER when they
    have a linked telegram account, otherwise to the admin who asked (so
    they can forward it by hand)."""
    try:
        user = await api.get_user(callback_data.username, owner_admin_id=acting_scope["owner_admin_id"])
    except ApiError as exc:
        await call.answer(f"خطا: {exc}", show_alert=True)
        return
    conns = user.get("connections") or []
    if not conns:
        await call.answer("این کاربر اتصالی ندارد", show_alert=True)
        return

    target_id = user.get("telegram_id") or call.from_user.id
    to_customer = bool(user.get("telegram_id"))
    await call.answer("در حال ارسال...")
    try:
        await send_connections(bot, target_id, conns)
    except Exception:
        await call.message.answer("❌ ارسال کانفیگ‌ها ناموفق بود.", reply_markup=home_kb())
        return
    await call.message.answer(
        f"✅ {len(conns)} کانفیگ برای مشتری ارسال شد."
        if to_customer
        else f"✅ {len(conns)} کانفیگ برای شما ارسال شد (این کاربر تلگرام وصل‌شده ندارد).",
    )


# ------------------------------------------------------------ جستجو
@router.callback_query(MenuCB.filter(F.action == "admin_search"))
async def cb_admin_search_start(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminSearchStates.waiting_username)
    await call.message.edit_text(
        "بخشی از نام کاربری را بفرستید تا جستجو کنم:",
        reply_markup=cancel_kb(),
    )
    await call.answer()


@router.message(AdminSearchStates.waiting_username, F.text)
async def admin_search_query(message: Message, state: FSMContext, acting_scope: dict) -> None:
    query = (message.text or "").strip()
    await state.clear()
    if not query:
        await message.answer("چیزی وارد نشد.", reply_markup=home_kb())
        return
    await _show_user_list(message, page=1, search=query, owner_admin_id=acting_scope["owner_admin_id"])


# ------------------------------------------------------- گزارش فروش
@router.callback_query(MenuCB.filter(F.action == "admin_stats"))
async def cb_admin_stats(call: CallbackQuery, acting_scope: dict) -> None:
    try:
        stats = await api.get_sales_stats(owner_admin_id=acting_scope["owner_admin_id"])
    except ApiError as exc:
        await call.answer(f"خطا: {exc}", show_alert=True)
        return
    if not stats:
        await call.answer("آماری موجود نیست", show_alert=True)
        return

    def _row(label: str, key: str) -> str:
        row = stats.get(key) or {}
        return f"{label}: {row.get('total', 0):,} تومان ({row.get('count', 0)} فروش)"

    text = "\n".join([
        "📊 <b>گزارش فروش</b>",
        "",
        _row("امروز", "today"),
        _row("۷ روز اخیر", "week"),
        _row("۳۰ روز اخیر", "month"),
        "",
        f"کاربران: {stats.get('users_total', 0)} (فعال: {stats.get('users_active', 0)})",
    ])
    await call.message.edit_text(text, reply_markup=home_kb())
    await call.answer()
