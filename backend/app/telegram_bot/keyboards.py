from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .utils import fmt_date_jalali
from .callbacks import (
    MenuCB,
    AdminListPageCB,
    AdminUserCB,
    AdminServiceCB,
    AdminPkgPickCB,
    AdminCreatePkgCB,
    AdminRenewPkgCB,
    NodeCB,
    ProtocolCB,
    PackageCB,
    SessionCountCB,
    TutorialCB,
    TopupAmountCB,
    PayCB,
    ApprovalCB,
    ConnectionCB,
    DeleteCB,
    DeleteConfirmCB,
    PurchaseCB,
    RenameCB,
    SwitchAccountCB,
)

PAGE_SIZE = 8

PROTOCOL_LABELS = {
    "wireguard": "🔒 WireGuard",
    "openvpn": "🛡 OpenVPN",
    "l2tp": "🌐 L2TP/IPsec",
    "ikev2": "🛰 IKEv2/IPsec",
    "sstp": "🔐 SSTP",
    "pptp": "⚠️ PPTP (قدیمی)",
    "xray": "⚡ V2Ray/Xray",
    "softether": "🔷 SoftEther",
}


# Every toggleable customer main-menu button, in display order - keyed by
# the same MenuCB action string used below. Shown to the admin in Settings >
# ربات > منوی مشتری as a matching set of checkboxes (see
# routers/telegram_bot_settings.py's BotSettings.customer_menu_disabled_items)
# and used here to filter which buttons actually get built.
CUSTOMER_MENU_ITEMS = [
    # A colored SQUARE prefix on each item - requested 2026-09-23, through
    # several rounds of feedback landing on this:
    #  1) "رنگی‌تر باشه، تو چشم‌تر" (more colorful, more eye-catching) -
    #     first try was a circle prefix next to the old icon.
    #  2) "یه دایره ابی اومده کنارش اون نباشه خود گزینه رنگی بشه" (a blue
    #     circle showed up NEXT TO it - don't want that) - the circle
    #     replaced the old icon instead of sitting beside it.
    #  3) "خوده دکمه باید رنگی بشه" (the button itself should look
    #     colored) - upgraded circle to a bigger/more solid square, AND
    #     (misreading this instruction) dropped the text entirely on 4
    #     "important" buttons, leaving just a colored glyph.
    #  4) "جای اسم‌ها چهارتا ایکون رنگی اومده" (four colored icons showed
    #     up INSTEAD OF THE NAMES) - that #3 tradeoff was wrong: the Bot
    #     API (both InlineKeyboardButton and KeyboardButton) has no color
    #     field at all - text/emoji is the entire button, there is no way
    #     to tint a button's background regardless of what's typed into
    #     its label. A label with no text is just an unreadable button,
    #     not "a colored option". Reverted - every item keeps its square
    #     AND its name; the square is as close to "a colored button" as
    #     this API allows, full stop.
    ("cust_account", "🟦 اکانت من"),
    ("cust_usage", "🟪 مصرف سرویس‌ها"),
    ("cust_renew", "🟩 تمدید سرویس"),
    ("cust_buy", "🟧 خرید اکانت جدید"),
    ("cust_topup", "🟨 افزایش اعتبار"),
    ("cust_tutorials", "🟫 آموزش"),
    ("cust_referral", "🟥 دعوت دوستان"),
    ("cust_support", "⬜ پشتیبانی"),
    ("cust_link", "⬛ وصل کردن حساب قبلی"),
    ("cust_myid", "🟦 آیدی عددی من"),
]

# Same idea as CUSTOMER_MENU_ITEMS, one list per admin tier so main_menu_kb
# below and handlers/persistent_menu.py's admin bar always show the exact
# same buttons - see CUSTOMER_MENU_ITEMS's own comment for why that has to
# be one shared list instead of two that can drift apart. Two lists (not
# one filtered by a flag) because the seller tier isn't "the full list with
# some hidden" - "📋 لیست کاربران" vs "📋 لیست کاربران من" is a different
# label for what's still the same admin_list action.
ADMIN_MENU_ITEMS_FULL = [
    # Same colored-square prefix idea as CUSTOMER_MENU_ITEMS above - kept
    # the same color per action across both tiers below (admin_create is
    # 🟩 in both lists, etc.) so a seller promoted to admin sees a familiar
    # bar rather than everything reshuffling. Every button keeps its text,
    # same as the customer list now does - see CUSTOMER_MENU_ITEMS's own
    # comment for why an icon-only button was reverted.
    ("admin_create", "🟩 ساخت کاربر"),
    ("admin_list", "🟦 لیست کاربران"),
    ("admin_pending", "🟧 درخواست‌های در انتظار"),
    ("admin_broadcast", "🟪 پیام همگانی"),
    ("admin_dm", "🟨 پیام به یک کاربر"),
    ("admin_search", "⬜ جستجوی کاربر"),
    ("admin_stats", "🟫 گزارش فروش"),
    ("admin_history", "⬛ تاریخچه درخواست‌ها"),
]

ADMIN_MENU_ITEMS_SELLER = [
    ("admin_create", "🟩 ساخت کاربر"),
    ("admin_list", "🟦 لیست کاربران من"),
    ("admin_search", "⬜ جستجوی کاربر"),
]


async def main_menu_kb(scope: dict | None) -> InlineKeyboardMarkup:
    """`scope` is the dict returned by telegram_bot/admin_scope.py's
    resolve_admin_scope() - None for a regular customer, otherwise a dict
    whose `is_full_admin` flag picks between the full admin menu and the
    reduced one.

    That flag now follows the panel ROLE: a superadmin and a level-2 Admin
    both get the full menu, a Seller gets the reduced one. It used to mean
    "is in the bot's global admin_ids list", which had nothing to do with
    the hierarchy - so a level-2 Admin with a real panel account, their own
    customers and their own receipts saw three buttons, while a bare number
    typed into a settings field saw everything.

    The buttons are the same for both Admin tiers; what differs is the data
    behind them, scoped per account by the API (routers/bot.py's
    _visibility_filter). Menu and scope are deliberately NOT two separate
    decisions - that is how they drifted apart in the first place."""
    kb = InlineKeyboardBuilder()
    if scope and scope.get("is_full_admin"):
        for action, label in ADMIN_MENU_ITEMS_FULL:
            kb.button(text=label, callback_data=MenuCB(action=action))
        kb.adjust(2, 2, 2, 2)
    elif scope:
        for action, label in ADMIN_MENU_ITEMS_SELLER:
            kb.button(text=label, callback_data=MenuCB(action=action))
        kb.adjust(1)
    else:
        # local import - avoids a circular import at module load (panel_bridge
        # imports from routers, which don't import keyboards.py)
        from .panel_bridge import get_customer_menu_disabled_items_cached

        disabled = set(await get_customer_menu_disabled_items_cached())
        shown = 0
        for action, label in CUSTOMER_MENU_ITEMS:
            if action not in disabled:
                kb.button(text=label, callback_data=MenuCB(action=action))
                shown += 1
        # Two per row - the list is long enough (up to 10 items) that one
        # button per row pushed the bottom half off-screen on a phone. An
        # odd count leaves the LAST button full-width on its own row rather
        # than half-width next to empty space.
        rows = [2] * (shown // 2)
        if shown % 2:
            rows.append(1)
        kb.adjust(*(rows or [1]))
    return kb.as_markup()


async def persistent_menu_kb(scope: dict | None = None) -> ReplyKeyboardMarkup | None:
    """The bar pinned under the text box in a customer's chat.

    Same items, same order and the same «منوی مشتری» on/off switches as the
    inline menu above - deliberately ONE list (CUSTOMER_MENU_ITEMS), because
    two lists of shop buttons would drift apart the first time someone added
    a feature. See handlers/persistent_menu.py for how a tap gets routed.

    `scope` (telegram_bot/admin_scope.py's resolve_admin_scope() result)
    prepends that tier's ADMIN_MENU_ITEMS_FULL/SELLER labels ahead of the
    shop items instead of replacing them. Telegram allows exactly one
    ReplyKeyboardMarkup per chat, and start.py's cmd_start sends the shop
    bar to admins on purpose (so the panel owner can test their own shop
    without switching accounts) - a bar with admin-only items would
    silently take that away again the same way the very first version of
    this bar silently ate admin taps (see on_menu_tap's docstring). One
    combined bar keeps both true at once.

    None when the panel owner has switched every customer item off AND
    there are no admin items to show either (i.e. a plain customer with
    nothing enabled): an empty bar is worse than none, and Telegram will
    not accept one anyway.
    """
    from .panel_bridge import get_customer_menu_disabled_items_cached

    disabled = set(await get_customer_menu_disabled_items_cached())
    admin_items = []
    if scope and scope.get("is_full_admin"):
        admin_items = ADMIN_MENU_ITEMS_FULL
    elif scope:
        admin_items = ADMIN_MENU_ITEMS_SELLER
    labels = [label for _, label in admin_items] + [
        label for action, label in CUSTOMER_MENU_ITEMS if action not in disabled
    ]
    if not labels:
        return None
    rows = [
        [KeyboardButton(text=label) for label in labels[i:i + 2]]
        for i in range(0, len(labels), 2)
    ]
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,   # without this Telegram gives it half the screen
        is_persistent=True,     # stays open instead of collapsing behind an icon
        input_field_placeholder="از منوی پایین انتخاب کنید…",
    )


def cancel_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✖️ انصراف", callback_data=MenuCB(action="cancel"))
    return kb.as_markup()


def home_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🏠 منوی اصلی", callback_data=MenuCB(action="home"))
    return kb.as_markup()


def promo_skip_kb() -> InlineKeyboardMarkup:
    """Single "رد کردن" button shown alongside the referral/discount-code
    text-entry prompts in the purchase flow (see customer.py's
    _ask_for_receipt) - same MenuCB(action="promo_skip") handled by a
    different handler depending on which of the two FSM states is active."""
    kb = InlineKeyboardBuilder()
    kb.button(text="⏭ رد کردن", callback_data=MenuCB(action="promo_skip"))
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


def admin_user_detail_kb(username: str, enabled_status: bool, allow_reset: bool = False) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    toggle_text = "⛔️ غیرفعال‌سازی" if enabled_status else "✅ فعال‌سازی"
    kb.button(text=toggle_text, callback_data=AdminUserCB(action="toggle", username=username))
    kb.button(text="♻️ تمدید سرویس", callback_data=AdminUserCB(action="renew", username=username))
    kb.button(text="📦 افزودن پکیج", callback_data=AdminUserCB(action="addpkg", username=username))
    kb.button(text="💰 اعتبار کیف پول", callback_data=AdminUserCB(action="balance", username=username))
    kb.button(text="📤 ارسال مجدد کانفیگ", callback_data=AdminUserCB(action="sendcfg", username=username))
    # Superadmin only - see handlers/admin_users.py's _show_user_detail.
    if allow_reset:
        kb.button(text="🔄 ریست مصرف", callback_data=AdminUserCB(action="resetusage", username=username))
    kb.button(text="🗑 حذف کاربر", callback_data=AdminUserCB(action="delete", username=username))
    kb.button(text="🔃 بروزرسانی", callback_data=AdminUserCB(action="view", username=username))
    kb.button(text="🏠 منوی اصلی", callback_data=MenuCB(action="home"))
    kb.adjust(2, 2, 1, 2, 1, 1) if allow_reset else kb.adjust(2, 2, 1, 1, 1, 1)
    return kb.as_markup()


def admin_services_kb(username: str, purchases: list[dict], action: str) -> InlineKeyboardMarkup:
    """Lists a customer's independent services so the admin can act on ONE
    of them (see AdminServiceCB) - used by "تمدید سرویس", which must
    continue an existing service rather than create a new one."""
    kb = InlineKeyboardBuilder()
    for p in purchases:
        name = p.get("package_name_snapshot") or "سرویس"
        if p.get("quota_bytes"):
            left = max(0, p["quota_bytes"] - (p.get("used_bytes") or 0)) / (1024 ** 3)
            detail = f"{left:.1f}GB مانده"
        else:
            detail = "نامحدود"
        kb.button(
            text=f"{name} · {detail}",
            callback_data=AdminServiceCB(action=action, username=username, purchase_id=p["id"]),
        )
    kb.button(text="✖️ انصراف", callback_data=AdminUserCB(action="view", username=username))
    kb.adjust(1)
    return kb.as_markup()


def admin_packages_kb(username: str, packages: list[dict]) -> InlineKeyboardMarkup:
    """Package picker for giving an EXISTING customer another package from
    the bot's admin side (mirrors the panel's «افزودن پکیج»)."""
    kb = InlineKeyboardBuilder()
    for p in packages:
        # Same compact one-line format the customer picker uses.
        kb.button(
            text=package_button_label(p),
            callback_data=AdminPkgPickCB(username=username, package_id=p["id"]),
        )
    kb.button(text="✖️ انصراف", callback_data=AdminUserCB(action="view", username=username))
    kb.adjust(1)
    return kb.as_markup()


def admin_create_packages_kb(packages: list[dict]) -> InlineKeyboardMarkup:
    """Package picker shown while creating a BRAND NEW user via the bot's
    admin «➕ ساخت کاربر» flow - the FIRST question after the username, since
    the package decides the quota, the duration, the price and (when it
    bundles any) the servers themselves. Cancels back to the main menu since
    the user being created doesn't exist yet to "view"."""
    kb = InlineKeyboardBuilder()
    for p in packages:
        kb.button(text=package_button_label(p), callback_data=AdminCreatePkgCB(package_id=p["id"]))
    kb.button(text="✖️ انصراف", callback_data=MenuCB(action="cancel"))
    kb.adjust(1)
    return kb.as_markup()


def admin_renew_packages_kb(packages: list[dict], username: str) -> InlineKeyboardMarkup:
    """Package picker shown for the bot's admin «♻️ تمدید سرویس» flow -
    replaces the old free-text "<GB> <days>" prompt."""
    kb = InlineKeyboardBuilder()
    for p in packages:
        kb.button(text=package_button_label(p), callback_data=AdminRenewPkgCB(package_id=p["id"]))
    kb.button(text="✖️ انصراف", callback_data=AdminUserCB(action="view", username=username))
    kb.adjust(1)
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
        icon = "🌐" if n["type"] == "mikrotik" else "🔷" if n["type"] == "softether" else "⚡"
        kb.button(text=f"{icon} {n['name']}", callback_data=NodeCB(node_id=n["id"]))
    kb.button(text="✖️ انصراف", callback_data=MenuCB(action="cancel"))
    kb.adjust(1)
    return kb.as_markup()


# Which protocols each kind of node can carry - a MikroTik cannot serve
# Xray and an Xray node serves nothing else. Named constants rather than a
# literal inside the function because routers/packages.py holds the same
# split for the panel, and test_package_protocols.py compares the two: a
# protocol added to one side and forgotten on the other now fails a test
# instead of quietly disagreeing (which is how PPTP was nearly shipped
# offerable in the bot and not in the panel).
MIKROTIK_PROTOCOLS = ["wireguard", "openvpn", "l2tp", "ikev2", "sstp", "pptp"]
XRAY_PROTOCOLS = ["xray"]
SOFTETHER_PROTOCOLS = ["softether"]


def protocols_kb(node_type: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    protocols = (
        XRAY_PROTOCOLS if node_type == "xray"
        else SOFTETHER_PROTOCOLS if node_type == "softether"
        else MIKROTIK_PROTOCOLS
    )
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


def _fa_digits(value) -> str:
    return str(value).translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))


def package_button_label(p: dict) -> str:
    """One short line per package: just its NAME and its PRICE.

    The old label crammed name + quota + days + price between "|" pipes,
    which wrapped onto two lines on a phone and repeated numbers most
    package names already state ("۲۰ گیگ ۱ ماه"). The panel owner's call
    (2026-08-10): the name is the description, so the button only needs
    that plus what it costs - anything else belongs on the package's own
    detail//payment screen, not on a button."""
    name = (p.get("name") or "").strip()
    price = p.get("price") or 0
    price_txt = f"{_fa_digits(format(price, ','))} تومان" if price else "رایگان"
    return f"{name} · {price_txt}"


def packages_kb(packages: list[dict], kind: str) -> InlineKeyboardMarkup:
    """Numbered to match the cards in the message above (see
    utils.packages_message) - with the detail now in the text, the button
    is just "which of the ones I read about", and a bare name repeated out
    of order is harder to match up than a number."""
    kb = InlineKeyboardBuilder()
    for i, p in enumerate(packages, start=1):
        kb.button(
            text=f"{_fa_digits(i)}. {package_button_label(p)}",
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
                # models.Purchase.comment - the label a customer/admin gave
                # THIS service ("اکانت 1", or their own text) specifically
                # so two otherwise-identical purchases don't look like one.
                # Every connection sharing this batch belongs to the same
                # Purchase, so the first non-empty one found is it.
                "comment": None,
                "created_at": c.get("created_at") or "",
            }
            order.append(key)
        g = groups[key]
        g["connections"].append(c)
        if not g["comment"] and c.get("comment"):
            g["comment"] = c["comment"]
        created = c.get("created_at") or ""
        if created and (not g["created_at"] or created < g["created_at"]):
            g["created_at"] = created

    result = [groups[k] for k in order]
    result.sort(key=lambda g: g["created_at"], reverse=True)

    for g in result:
        conns = g["connections"]
        # Client-side hint only for whether to show "🗑" at all (every
        # connection quota/expiry enforcement already disabled - see
        # services/quota_manager.py's _enforce_purchase_limits/
        # _enforce_user_limits, which disable every connection of a service
        # the moment it goes expired/quota_exceeded). The actual gate lives
        # server-side (routers/bot.py's delete_purchase/delete_connection,
        # checked against Purchase.status/User.status directly) - a button
        # shown from a stale list just gets a polite error back instead of
        # silently deleting something still in use.
        g["deletable"] = bool(conns) and all(not c.get("enabled") for c in conns)
        # Jalali, not the raw ISO/Gregorian prefix - this string is
        # customer-facing (purchase-date line under "📊 مصرف" and every
        # purchase button label), and a Persian customer reading
        # "2026-07-23" has to convert it in their head.
        date_label = fmt_date_jalali(g["created_at"], with_time=False) if g["created_at"] else ""
        g["date_label"] = date_label  # kept separate from label - see standalone_usage_text, which
        # shows the package name on its own line instead of joined with the date (button labels
        # below still use the combined single-line `label`, since a button can't wrap nicely).
        suffix = f" ({len(conns)} سرویس)" if len(conns) > 1 else ""
        if g["comment"]:
            # The comment IS the point of this button - it is what the
            # customer actually typed (or the auto "اکانت N" fallback, see
            # services/user_ops._auto_service_label) to tell this purchase
            # apart from their others, so it leads even when a package name
            # is also known - the package name repeats on every purchase of
            # the same package and tells two of them apart from each other
            # exactly as well as showing nothing at all does.
            g["label"] = f"🧾 {g['comment']}{suffix} — {date_label}"
        elif g["package_name"]:
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
    submenu built by connections_list_kb(..., back_to_purchases=True).

    Each group also gets a small "✏️" button beside its main one (RenameCB,
    handlers/customer_account.py's cb_rename_start) - added 2026-09-23 so a
    customer can replace the auto "اکانت N" label with their own text
    without that disturbing the main button's existing tap behavior (single
    -> straight to the connection, multi -> the submenu). An already
    expired/exhausted group (g["deletable"] - see group_connections_by_purchase)
    also gets a "🗑" (DeleteCB, cb_delete_start) beside those two, added the
    same day, so a customer can clear out a service they can no longer use
    instead of it sitting in the list forever."""
    kb = InlineKeyboardBuilder()
    row_sizes = []
    for g in groups:
        if len(g["connections"]) == 1:
            kb.button(text=g["label"], callback_data=ConnectionCB(connection_id=g["connections"][0]["id"]))
        else:
            kb.button(text=g["label"], callback_data=PurchaseCB(key=g["key"]))
        kb.button(text="✏️", callback_data=RenameCB(key=g["key"]))
        row = 2
        if g["deletable"]:
            kb.button(text="🗑", callback_data=DeleteCB(key=g["key"]))
            row = 3
        row_sizes.append(row)
    # One link per CUSTOMER (covers every Xray/VLESS service combined - see
    # routers/subscription.py), not per purchase/connection, so it sits
    # here at the account level rather than inside any one group's submenu.
    kb.button(text="🔗 دریافت لینک ساب", callback_data=MenuCB(action="cust_sublink"))
    kb.button(text="🏠 منوی اصلی", callback_data=MenuCB(action="home"))
    # Each group's own row first (2 or 3, depending on whether it got a "🗑"),
    # then the two trailing full-width buttons - a fixed size would either
    # smash a group's buttons onto separate rows or misalign the ones next
    # to it.
    kb.adjust(*(row_sizes + [1, 1]))
    return kb.as_markup()


def delete_confirm_kb(key: str) -> InlineKeyboardMarkup:
    """"⚠️ آیا مطمئن هستید؟" screen opened by DeleteCB (see
    handlers/customer_account.py's cb_delete_start) before a service is
    actually removed - same "ask before an irreversible action" shape as
    the rest of this file's flows."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🗑 بله، حذف کن", callback_data=DeleteConfirmCB(key=key))
    kb.button(text="✖️ انصراف", callback_data=MenuCB(action="cust_account"))
    kb.adjust(1)
    return kb.as_markup()


def standalone_usage_text(connections: list[dict], expire_at=None) -> str:
    """Full standalone "📊 مصرف سرویس‌ها" view opened directly from the main
    menu (see MenuCB action="cust_usage") - the ONLY place a customer sees
    per-service usage numbers; "اکانت من" (_account_text in
    handlers/customer.py) deliberately does not repeat them (used to, via
    this function's now-removed usage_per_service_text sibling - it just
    duplicated this view and cluttered the account screen). Has to say
    something sensible for 0 or 1 service too, not just stay blank, since
    unlike that old sibling this is the ENTIRE message for this button.

    Grouped by purchase (same grouping as "👤 اکانت من" - see
    group_connections_by_purchase) so services bought together show up
    together with their own subtotal, instead of one flat list of every
    service the customer has ever bought. `expire_at`, when given, is shown
    once near the top in the Jalali calendar (see utils.fmt_date_jalali) -
    it's a single User-level field (not per-purchase), so it only needs
    showing once rather than repeated per group."""
    from .utils import fmt_bytes, fmt_date_jalali

    if not connections:
        return "📊 <b>مصرف سرویس‌ها</b>\n\nهنوز هیچ سرویسی برای شما فعال نشده."

    groups = group_connections_by_purchase(connections)
    lines = ["📊 <b>مصرف سرویس‌ها:</b>"]
    if expire_at:
        lines.append(f"📅 انقضا: {fmt_date_jalali(expire_at)}")
    grand_total = 0
    for g in groups:
        conns = g["connections"]
        group_total = sum(c.get("total_bytes") or 0 for c in conns)
        grand_total += group_total
        lines.append("")
        # The comment (see group_connections_by_purchase) leads exactly as
        # it does in the button label above - it's the customer's own name
        # for this purchase. Package name, when known, follows as a second,
        # more technical line rather than being dropped - unlike the button
        # label there's room here to show both.
        heading = g["comment"] or g["package_name"]
        if heading:
            lines.append(f"🧾 <b>{heading}</b>")
            if g["comment"] and g["package_name"]:
                lines.append(g["package_name"])
            if g["date_label"]:
                lines.append(f"تاریخ خرید: {g['date_label']}")
        if len(conns) == 1:
            single_line = g["label"] if not heading else (
                f"{'✅' if conns[0].get('enabled') else '⛔️'} "
                + PROTOCOL_LABELS.get(conns[0]["type"], conns[0]["type"])
                + (f" — {conns[0]['node_name']}" if (conns[0].get("node_name") or "").strip() else "")
            )
            lines.append(f"{single_line}: {fmt_bytes(group_total)}")
        else:
            if not heading:
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
