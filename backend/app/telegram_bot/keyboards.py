from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .callbacks import (
    MenuCB,
    AdminListPageCB,
    AdminUserCB,
    NodeCB,
    ProtocolCB,
    PackageCB,
    SessionCountCB,
    TutorialCB,
    TopupAmountCB,
    PayCB,
    ApprovalCB,
    ConnectionCB,
    PurchaseCB,
    SwitchAccountCB,
)

PAGE_SIZE = 8

PROTOCOL_LABELS = {
    "wireguard": "🔒 WireGuard",
    "openvpn": "🛡 OpenVPN",
    "l2tp": "🌐 L2TP/IPsec",
    "ikev2": "🛰 IKEv2/IPsec",
    "sstp": "🔐 SSTP",
    "xray": "⚡ V2Ray/Xray",
}


def main_menu_kb(scope: dict | None) -> InlineKeyboardMarkup:
    """`scope` is the dict returned by telegram_bot/admin_scope.py's
    resolve_admin_scope() - None for a regular customer, otherwise a dict
    with an `is_full_admin` flag that picks between the full admin menu
    (pending approvals + broadcast included) and the scoped-down menu a
    linked group-admin gets (their own users only, no pending/broadcast -
    those stay exclusive to the bot's global admin list)."""
    kb = InlineKeyboardBuilder()
    if scope and scope.get("is_full_admin"):
        kb.button(text="➕ ساخت کاربر", callback_data=MenuCB(action="admin_create"))
        kb.button(text="📋 لیست کاربران", callback_data=MenuCB(action="admin_list"))
        kb.button(text="📥 درخواست‌های در انتظار", callback_data=MenuCB(action="admin_pending"))
        kb.button(text="📢 پیام همگانی", callback_data=MenuCB(action="admin_broadcast"))
        kb.adjust(2, 2)
    elif scope:
        kb.button(text="➕ ساخت کاربر", callback_data=MenuCB(action="admin_create"))
        kb.button(text="📋 لیست کاربران من", callback_data=MenuCB(action="admin_list"))
        kb.adjust(1)
    else:
        kb.button(text="👤 اکانت من", callback_data=MenuCB(action="cust_account"))
        kb.button(text="📊 مصرف سرویس‌ها", callback_data=MenuCB(action="cust_usage"))
        kb.button(text="🔄 تمدید سرویس", callback_data=MenuCB(action="cust_renew"))
        kb.button(text="🛒 خرید اکانت جدید", callback_data=MenuCB(action="cust_buy"))
        kb.button(text="💰 افزایش اعتبار", callback_data=MenuCB(action="cust_topup"))
        kb.button(text="📚 آموزش", callback_data=MenuCB(action="cust_tutorials"))
        kb.button(text="🔗 وصل کردن حساب قبلی", callback_data=MenuCB(action="cust_link"))
        kb.button(text="🆔 آیدی عددی من", callback_data=MenuCB(action="cust_myid"))
        kb.adjust(1)
    return kb.as_markup()


def cancel_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✖️ انصراف", callback_data=MenuCB(action="cancel"))
    return kb.as_markup()


def home_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🏠 منوی اصلی", callback_data=MenuCB(action="home"))
    return kb.as_markup()


# ------------------------------------------------------------------ admin
def admin_users_list_kb(items: list[dict], page: int, total: int, search: str | None) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for u in items:
        status_icon = {"active": "🟢", "disabled": "🔴", "quota_exceeded": "🟠", "expired": "⚫️"}.get(u["status"], "⚪️")
        kb.button(text=f"{status_icon} {u['username']}", callback_data=AdminUserCB(action="view", username=u["username"]))
    kb.adjust(1)

    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    nav_row = []
    s = search or "-"
    if page > 1:
        nav_row.append(InlineKeyboardButton(text="⬅️ قبلی", callback_data=AdminListPageCB(page=page - 1, search=s).pack()))
    nav_row.append(InlineKeyboardButton(text=f"{page}/{total_pages}", callback_data="noop"))
    if page < total_pages:
        nav_row.append(InlineKeyboardButton(text="بعدی ➡️", callback_data=AdminListPageCB(page=page + 1, search=s).pack()))
    if nav_row:
        kb.row(*nav_row)

    kb.row(InlineKeyboardButton(text="🏠 منوی اصلی", callback_data=MenuCB(action="home").pack()))
    return kb.as_markup()


def admin_user_detail_kb(username: str, enabled_status: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    toggle_text = "⛔️ غیرفعال‌سازی" if enabled_status else "✅ فعال‌سازی"
    kb.button(text=toggle_text, callback_data=AdminUserCB(action="toggle", username=username))
    kb.button(text="♻️ تمدید / افزودن حجم", callback_data=AdminUserCB(action="renew", username=username))
    kb.button(text="🔄 ریست مصرف", callback_data=AdminUserCB(action="resetusage", username=username))
    kb.button(text="🗑 حذف کاربر", callback_data=AdminUserCB(action="delete", username=username))
    kb.button(text="🔃 بروزرسانی", callback_data=AdminUserCB(action="view", username=username))
    kb.button(text="🏠 منوی اصلی", callback_data=MenuCB(action="home"))
    kb.adjust(1, 1, 1, 1, 1, 1)
    return kb.as_markup()


def confirm_delete_kb(username: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ بله، حذف کن", callback_data=AdminUserCB(action="delete_confirm", username=username))
    kb.button(text="✖️ انصراف", callback_data=AdminUserCB(action="view", username=username))
    kb.adjust(2)
    return kb.as_markup()


def nodes_kb(nodes: list[dict]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for n in nodes:
        icon = "🌐" if n["type"] == "mikrotik" else "⚡"
        kb.button(text=f"{icon} {n['name']}", callback_data=NodeCB(node_id=n["id"]))
    kb.button(text="✖️ انصراف", callback_data=MenuCB(action="cancel"))
    kb.adjust(1)
    return kb.as_markup()


def protocols_kb(node_type: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    protocols = ["xray"] if node_type == "xray" else ["wireguard", "openvpn", "l2tp", "ikev2", "sstp"]
    for p in protocols:
        kb.button(text=PROTOCOL_LABELS.get(p, p), callback_data=ProtocolCB(protocol=p))
    kb.button(text="✖️ انصراف", callback_data=MenuCB(action="cancel"))
    kb.adjust(1)
    return kb.as_markup()


# --------------------------------------------------------------- customer
def session_count_label(count: int) -> str:
    if count <= 0:
        return "♾️ نامحدود"
    if count == 1:
        return "👤 تک کاربر"
    return f"👥 {count} کاربر"


def session_count_kb(counts: list[int], kind: str) -> InlineKeyboardMarkup:
    """Step shown before the package list itself when the currently
    available packages don't all share one Package.max_concurrent_sessions
    value - lets the customer filter straight to "تک کاربر"/"۲ کاربر"/...
    packages instead of scrolling through every package regardless of how
    many people are meant to share it. `counts` is the SORTED list of
    distinct values actually present among the packages (0 stands in for
    "نامحدود"/None - see _start_package_picker)."""
    kb = InlineKeyboardBuilder()
    for c in counts:
        kb.button(text=session_count_label(c), callback_data=SessionCountCB(kind=kind, count=c))
    kb.button(text="✖️ انصراف", callback_data=MenuCB(action="cancel"))
    kb.adjust(1)
    return kb.as_markup()


def packages_kb(packages: list[dict], kind: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for p in packages:
        quota_txt = f"{p['quota_gb']}GB" if p["quota_gb"] else "نامحدود"
        days_txt = f"{p['duration_days']} روز" if p.get("duration_days") else "بدون انقضا"
        price_txt = f"{p['price']:,} تومان" if p["price"] else "رایگان"
        kb.button(
            text=f"{p['name']} | {quota_txt} | {days_txt} | {price_txt}",
            callback_data=PackageCB(kind=kind, package_id=p["id"]),
        )
    kb.button(text="✖️ انصراف", callback_data=MenuCB(action="cancel"))
    kb.adjust(1)
    return kb.as_markup()


def receipt_choice_kb(show_balance: bool, price: int) -> InlineKeyboardMarkup:
    """Shown right before asking for a payment receipt - if the customer
    already has enough wallet balance, offers an instant "pay from
    balance" button that skips the receipt/admin-approval wait entirely."""
    kb = InlineKeyboardBuilder()
    if show_balance:
        kb.button(text=f"💰 پرداخت فوری از اعتبار ({price:,} تومان)", callback_data=PayCB(method="balance"))
    kb.button(text="✖️ انصراف", callback_data=MenuCB(action="cancel"))
    kb.adjust(1)
    return kb.as_markup()


def topup_amounts_kb(presets: list[int]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for amount in presets:
        kb.button(text=f"{amount:,} تومان", callback_data=TopupAmountCB(amount=amount))
    kb.button(text="✏️ مبلغ دلخواه", callback_data=TopupAmountCB(amount=0))
    kb.button(text="✖️ انصراف", callback_data=MenuCB(action="cancel"))
    kb.adjust(1)
    return kb.as_markup()


def tutorials_kb(tutorials: list[dict]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for t in tutorials:
        kb.button(text=f"📄 {t['title']}", callback_data=TutorialCB(tutorial_id=t["id"]))
    kb.button(text="🏠 منوی اصلی", callback_data=MenuCB(action="home"))
    kb.adjust(1)
    return kb.as_markup()


def connections_list_kb(connections: list[dict], back_to_purchases: bool = False) -> InlineKeyboardMarkup:
    """One button per service - tapping a specific one is what actually
    sends that service's config/link/QR (see handlers/customer.py's
    cb_view_connection), instead of every service being dumped into the
    chat automatically and unlabeled. Used both as the top-level "👤 اکانت
    من" list (when a customer only has ungrouped/standalone connections -
    see group_connections_by_purchase) and as the submenu opened by tapping
    one multi-service purchase in purchases_kb (back_to_purchases=True gives
    it a "🔙 بازگشت به خریدها" button instead of jumping straight home)."""
    kb = InlineKeyboardBuilder()
    for c in connections:
        label = PROTOCOL_LABELS.get(c["type"], c["type"])
        status = "✅" if c.get("enabled") else "⛔️"
        node_name = (c.get("node_name") or "").strip()
        text = f"{status} {label}" + (f" — {node_name}" if node_name else "")
        kb.button(text=text, callback_data=ConnectionCB(connection_id=c["id"]))
    if back_to_purchases:
        kb.button(text="🔙 بازگشت به خریدها", callback_data=MenuCB(action="cust_account"))
    else:
        kb.button(text="🏠 منوی اصلی", callback_data=MenuCB(action="home"))
    kb.adjust(1)
    return kb.as_markup()


def group_connections_by_purchase(connections: list[dict]) -> list[dict]:
    """Groups a customer's connections by which purchase created them
    together (Connection.purchase_batch, snapshotted at provisioning time -
    see services/user_ops.py), newest purchase first, so "👤 اکانت من" can
    show one button per purchase instead of a flat list of every service.
    Connections with no batch (added one at a time, or created before this
    feature existed) each become their own single-connection group, same as
    the old flat-list behavior for them. Returns a list of dicts:
    {"key": str, "connections": [...], "label": str} - "key" is stable
    across re-sorts/re-fetches (unlike a list position), see PurchaseCB."""
    groups: dict[str, dict] = {}
    order: list[str] = []
    for c in connections:
        key = c.get("purchase_batch") or f"c{c['id']}"
        if key not in groups:
            groups[key] = {
                "key": key,
                "connections": [],
                "package_name": c.get("package_name"),
                "created_at": c.get("created_at") or "",
            }
            order.append(key)
        g = groups[key]
        g["connections"].append(c)
        created = c.get("created_at") or ""
        if created and (not g["created_at"] or created < g["created_at"]):
            g["created_at"] = created

    result = [groups[k] for k in order]
    result.sort(key=lambda g: g["created_at"], reverse=True)

    for g in result:
        conns = g["connections"]
        date_label = str(g["created_at"])[:10] if g["created_at"] else ""
        if g["package_name"]:
            suffix = f" ({len(conns)} سرویس)" if len(conns) > 1 else ""
            g["label"] = f"🧾 {g['package_name']}{suffix} — {date_label}"
        elif len(conns) > 1:
            g["label"] = f"🧾 خرید {date_label} ({len(conns)} سرویس)"
        else:
            proto_label = PROTOCOL_LABELS.get(conns[0]["type"], conns[0]["type"])
            status = "✅" if conns[0].get("enabled") else "⛔️"
            node_name = (conns[0].get("node_name") or "").strip()
            g["label"] = f"{status} {proto_label}" + (f" — {node_name}" if node_name else "")
    return result


def purchases_kb(groups: list[dict]) -> InlineKeyboardMarkup:
    """Top-level "👤 اکانت من" keyboard - one button per purchase group (see
    group_connections_by_purchase). Tapping a single-service group sends
    that service directly (handled in handlers/customer.py exactly like the
    old flat ConnectionCB list); tapping a multi-service group opens the
    submenu built by connections_list_kb(..., back_to_purchases=True)."""
    kb = InlineKeyboardBuilder()
    for g in groups:
        if len(g["connections"]) == 1:
            kb.button(text=g["label"], callback_data=ConnectionCB(connection_id=g["connections"][0]["id"]))
        else:
            kb.button(text=g["label"], callback_data=PurchaseCB(key=g["key"]))
    kb.button(text="🏠 منوی اصلی", callback_data=MenuCB(action="home"))
    kb.adjust(1)
    return kb.as_markup()


def usage_per_service_text(connections: list[dict]) -> str:
    """Renders the "📊 مصرف هر سرویس" section appended to the account view -
    each connection's own lifetime usage (Connection.total_bytes), as
    opposed to the single combined total already shown higher up in
    _account_text. Only meaningful when there's more than one service,
    otherwise it just repeats the combined total for no benefit."""
    if len(connections) < 2:
        return ""
    from .utils import fmt_bytes

    lines = ["\n📊 <b>مصرف هر سرویس:</b>"]
    for c in connections:
        label = PROTOCOL_LABELS.get(c["type"], c["type"])
        node_name = (c.get("node_name") or "").strip()
        name = f"{label} — {node_name}" if node_name else label
        lines.append(f"• {name}: {fmt_bytes(c.get('total_bytes') or 0)}")
    return "\n".join(lines)


def standalone_usage_text(connections: list[dict]) -> str:
    """Full standalone "📊 مصرف سرویس‌ها" view opened directly from the main
    menu (see MenuCB action="cust_usage") - unlike usage_per_service_text
    above (a supplementary section only shown when there's 2+ services,
    appended under the full "اکانت من" view), this is the ENTIRE message
    for this button, so it has to say something sensible for 0 or 1
    service too, not just stay blank.

    Grouped by purchase (same grouping as "👤 اکانت من" - see
    group_connections_by_purchase) so services bought together show up
    together with their own subtotal, instead of one flat list of every
    service the customer has ever bought."""
    from .utils import fmt_bytes

    if not connections:
        return "📊 <b>مصرف سرویس‌ها</b>\n\nهنوز هیچ سرویسی برای شما فعال نشده."

    groups = group_connections_by_purchase(connections)
    lines = ["📊 <b>مصرف سرویس‌ها:</b>"]
    grand_total = 0
    for g in groups:
        conns = g["connections"]
        group_total = sum(c.get("total_bytes") or 0 for c in conns)
        grand_total += group_total
        lines.append("")
        if len(conns) == 1:
            # Single-service "purchase" - g["label"] already IS the
            # status+name line (see group_connections_by_purchase), no need
            # to repeat it as a header above a one-line breakdown.
            lines.append(f"{g['label']}: {fmt_bytes(group_total)}")
        else:
            lines.append(f"<b>{g['label']}</b>")
            for c in conns:
                label = PROTOCOL_LABELS.get(c["type"], c["type"])
                node_name = (c.get("node_name") or "").strip()
                name = f"{label} — {node_name}" if node_name else label
                used = c.get("total_bytes") or 0
                status = "✅" if c.get("enabled") else "⛔️"
                lines.append(f"{status} {name}: {fmt_bytes(used)}")
            lines.append(f"جمع این خرید: {fmt_bytes(group_total)}")
    if len(groups) > 1:
        lines.append(f"\n<b>جمع کل:</b> {fmt_bytes(grand_total)}")
    return "\n".join(lines)


def account_picker_kb(users: list[dict]) -> InlineKeyboardMarkup:
    """Shown when a customer's telegram id resolves to more than one panel
    account (see telegram_bot/handlers/customer.py's _resolve_account) -
    lets them pick which one they mean before continuing whatever action
    (viewing "اکانت من", renewing, topping up, ...) triggered the lookup."""
    kb = InlineKeyboardBuilder()
    for u in users:
        label = u.get("full_name") or u["username"]
        balance = u.get("balance") or 0
        kb.button(
            text=f"👤 {label} ({u['username']}) — {balance:,} تومان",
            callback_data=SwitchAccountCB(username=u["username"]),
        )
    kb.button(text="✖️ انصراف", callback_data=MenuCB(action="cancel"))
    kb.adjust(1)
    return kb.as_markup()


def approval_kb(request_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ تایید و فعال‌سازی", callback_data=ApprovalCB(action="approve", request_id=request_id))
    kb.button(text="❌ رد کردن", callback_data=ApprovalCB(action="reject", request_id=request_id))
    kb.adjust(2)
    return kb.as_markup()
