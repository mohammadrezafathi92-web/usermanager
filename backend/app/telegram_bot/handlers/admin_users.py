from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from ..panel_bridge import api, ApiError
from ..callbacks import MenuCB, AdminListPageCB, AdminUserCB, NodeCB, ProtocolCB
from ..admin_scope import resolve_admin_scope
from ..keyboards import (
    admin_users_list_kb,
    admin_user_detail_kb,
    confirm_delete_kb,
    nodes_kb,
    protocols_kb,
    home_kb,
    cancel_kb,
)
from ..states import AdminCreateUserStates, AdminRenewStates
from ..utils import fmt_bytes, fmt_date, STATUS_LABELS

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


def _user_detail_text(user: dict) -> str:
    lines = [
        f"👤 <b>{user['username']}</b>",
        f"وضعیت: {STATUS_LABELS.get(user['status'], user['status'])}",
        f"مصرف: {fmt_bytes(user['used_bytes'])} / {fmt_bytes(user['total_quota_bytes']) if user['total_quota_bytes'] else 'نامحدود'}",
        f"انقضا: {fmt_date(user.get('expire_at'))}",
        f"موجودی اعتبار: {user.get('balance', 0):,} تومان",
    ]
    if user.get("telegram_id"):
        lines.append(f"تلگرام: <code>{user['telegram_id']}</code>")
    if user["connections"]:
        lines.append("\n<b>اتصالات:</b>")
        for c in user["connections"]:
            state = "فعال" if c["enabled"] else "غیرفعال"
            lines.append(f"• {c['type']} روی {c['node_name']} ({state})")
            if c.get("link"):
                lines.append(f"  <code>{c['link']}</code>")
    else:
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
    waiting_quota, ...) so it always works as an escape hatch mid-flow -
    see customer.py's matching comment for the full rationale."""
    await state.clear()
    await _show_user_list(message, page=1, search=None, owner_admin_id=acting_scope["owner_admin_id"])


@router.message(AdminCreateUserStates.waiting_username)
async def admin_create_username(message: Message, state: FSMContext) -> None:
    username = (message.text or "").strip()
    if not username or " " in username:
        await message.answer("نام کاربری معتبر نیست (بدون فاصله بفرستید). دوباره تلاش کنید:")
        return
    try:
        nodes = await api.list_nodes()
    except ApiError as exc:
        await message.answer(f"خطا: {exc}", reply_markup=home_kb())
        await state.clear()
        return
    if not nodes:
        await message.answer("هیچ سروری (نودی) در پنل تعریف نشده است.", reply_markup=home_kb())
        await state.clear()
        return
    await state.update_data(new_username=username, nodes={n["id"]: n for n in nodes})
    await state.set_state(AdminCreateUserStates.picking_node)
    await message.answer("این کاربر روی کدام سرور ساخته شود؟", reply_markup=nodes_kb(nodes))


@router.callback_query(NodeCB.filter(), AdminCreateUserStates.picking_node)
async def admin_pick_node(call: CallbackQuery, callback_data: NodeCB, state: FSMContext) -> None:
    data = await state.get_data()
    node = data["nodes"].get(callback_data.node_id) or data["nodes"].get(str(callback_data.node_id))
    if not node:
        await call.answer("سرور پیدا نشد", show_alert=True)
        return
    await state.update_data(node_id=node["id"], node_name=node["name"])
    await state.set_state(AdminCreateUserStates.picking_protocol)
    await call.message.edit_text(f"سرور: {node['name']}\nپروتکل را انتخاب کنید:", reply_markup=protocols_kb(node["type"]))
    await call.answer()


@router.callback_query(ProtocolCB.filter(), AdminCreateUserStates.picking_protocol)
async def admin_pick_protocol(call: CallbackQuery, callback_data: ProtocolCB, state: FSMContext) -> None:
    await state.update_data(protocol=callback_data.protocol)
    await state.set_state(AdminCreateUserStates.waiting_quota)
    await call.message.edit_text("حجم مصرفی (GB) را بفرستید (برای نامحدود 0 بفرستید):", reply_markup=cancel_kb())
    await call.answer()


@router.message(AdminCreateUserStates.waiting_quota)
async def admin_create_quota(message: Message, state: FSMContext) -> None:
    try:
        quota_gb = float((message.text or "0").strip())
    except ValueError:
        await message.answer("یک عدد بفرستید (مثلا 20 یا 0 برای نامحدود):")
        return
    await state.update_data(quota_gb=quota_gb)
    await state.set_state(AdminCreateUserStates.waiting_days)
    await message.answer("تعداد روز اعتبار را بفرستید (برای بدون‌انقضا 0 بفرستید):", reply_markup=cancel_kb())


@router.message(AdminCreateUserStates.waiting_days)
async def admin_create_days(message: Message, state: FSMContext, acting_scope: dict) -> None:
    try:
        days = int((message.text or "0").strip())
    except ValueError:
        await message.answer("یک عدد صحیح بفرستید (مثلا 30 یا 0 برای بدون‌انقضا):")
        return
    data = await state.get_data()
    try:
        user = await api.create_user(
            username=data["new_username"],
            quota_gb=data["quota_gb"],
            expire_days=days or None,
            connections=[{"node_id": data["node_id"], "protocol": data["protocol"]}],
            owner_admin_id=acting_scope["owner_admin_id"],
        )
    except ApiError as exc:
        await message.answer(f"خطا در ساخت کاربر: {exc}", reply_markup=home_kb())
        await state.clear()
        return
    await state.clear()
    await message.answer("✅ کاربر ساخته شد:\n\n" + _user_detail_text(user), reply_markup=home_kb())


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
async def _show_user_detail(target, username: str, owner_admin_id: int | None) -> None:
    try:
        user = await api.get_user(username, owner_admin_id=owner_admin_id)
    except ApiError as exc:
        await target.answer(f"خطا: {exc}")
        return
    kb = admin_user_detail_kb(username, user["status"] == "active")
    text = _user_detail_text(user)
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=kb)
    else:
        await target.answer(text, reply_markup=kb)


@router.callback_query(AdminUserCB.filter(F.action == "view"))
async def cb_user_view(call: CallbackQuery, callback_data: AdminUserCB, state: FSMContext, acting_scope: dict) -> None:
    await state.clear()
    await _show_user_detail(call, callback_data.username, acting_scope["owner_admin_id"])
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
    await _show_user_detail(call, callback_data.username, owner_admin_id)
    await call.answer("انجام شد")


@router.callback_query(AdminUserCB.filter(F.action == "resetusage"))
async def cb_user_reset(call: CallbackQuery, callback_data: AdminUserCB, acting_scope: dict) -> None:
    owner_admin_id = acting_scope["owner_admin_id"]
    try:
        await api.reset_usage(callback_data.username, owner_admin_id=owner_admin_id)
    except ApiError as exc:
        await call.answer(f"خطا: {exc}", show_alert=True)
        return
    await _show_user_detail(call, callback_data.username, owner_admin_id)
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
@router.callback_query(AdminUserCB.filter(F.action == "renew"))
async def cb_user_renew_ask(call: CallbackQuery, callback_data: AdminUserCB, state: FSMContext) -> None:
    await state.set_state(AdminRenewStates.waiting_values)
    await state.update_data(username=callback_data.username)
    await call.message.edit_text(
        "مقدار حجم اضافه (GB) و تعداد روز اضافه را با فاصله بفرستید.\nمثلا: <code>20 30</code>\n(برای صفر کردن مصرف فعلی هم، بعدش عدد ۳ رو تنها بفرستید)",
        reply_markup=cancel_kb(),
    )
    await call.answer()


@router.message(AdminRenewStates.waiting_values)
async def admin_renew_values(message: Message, state: FSMContext, acting_scope: dict) -> None:
    data = await state.get_data()
    username = data["username"]
    parts = (message.text or "").split()
    reset_usage = parts == ["3"]
    add_gb, add_days = 0.0, 0
    if not reset_usage:
        try:
            add_gb = float(parts[0]) if len(parts) > 0 else 0
            add_days = int(parts[1]) if len(parts) > 1 else 0
        except ValueError:
            await message.answer("فرمت درست نیست. مثلا: 20 30")
            return
    try:
        user = await api.renew(
            username, add_gb=add_gb, add_days=add_days, reset_usage=reset_usage,
            owner_admin_id=acting_scope["owner_admin_id"],
        )
    except ApiError as exc:
        await message.answer(f"خطا: {exc}", reply_markup=home_kb())
        await state.clear()
        return
    await state.clear()
    await message.answer("✅ بروزرسانی شد:\n\n" + _user_detail_text(user), reply_markup=home_kb())
