from aiogram import Router, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery, User

from ..panel_bridge import api, ApiError
from ..callbacks import MenuCB
from ..admin_scope import resolve_admin_scope
from ..keyboards import main_menu_kb

router = Router(name="start")


def _display_name(tg_user: User, account: dict | None) -> str:
    """Picks the most meaningful name to greet someone by - the panel
    username (e.g. "tg266249955" for an auto-created account, or a raw
    MikroTik-imported login) is an internal identifier, not something a
    customer recognizes themselves by. Prefers, in order: the admin-entered
    full name on the account, the person's actual Telegram display name,
    their @username, and only falls back to the panel username if Telegram
    somehow gave us none of the above."""
    if account and account.get("full_name"):
        return account["full_name"]
    if tg_user.full_name:
        return tg_user.full_name
    if tg_user.username:
        return f"@{tg_user.username}"
    return account["username"] if account else str(tg_user.id)


async def _welcome_text(tg_user: User, scope: dict | None = None) -> str:
    if scope:
        if scope["is_full_admin"]:
            return "🤖 <b>پنل مدیریت ربات</b>\n\nاز منوی زیر یکی از گزینه‌ها را انتخاب کنید:"
        who = f" ({scope['username']})" if scope.get("username") else ""
        return (
            f"🤖 <b>پنل مدیریت گروه شما</b>{who}\n\n"
            "از منوی زیر یکی از گزینه‌ها را انتخاب کنید - فقط کاربران خودتان را می‌بینید:"
        )
    try:
        account = await api.get_user_by_telegram(tg_user.id)
    except ApiError:
        account = None
    if account:
        return (
            f"👋 خوش برگشتید <b>{_display_name(tg_user, account)}</b>\n\n"
            "از منوی زیر می‌تونید وضعیت حسابتون رو ببینید یا تمدید کنید."
        )
    return (
        f"👋 سلام {_display_name(tg_user, None)}! به ربات فروش اکانت خوش اومدید.\n\n"
        "اگه قبلا از ما اکانت نداشتید، از «🛒 خرید اکانت جدید» شروع کنید.\n"
        "اگه قبلا اکانت داشتید ولی این ربات شما رو نمی‌شناسه، از «🔗 وصل کردن حساب قبلی» استفاده کنید."
    )


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    scope = await resolve_admin_scope(message.from_user.id)
    text = await _welcome_text(message.from_user, scope)
    await message.answer(text, reply_markup=main_menu_kb(scope))


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    scope = await resolve_admin_scope(message.from_user.id)
    if scope and scope["is_full_admin"]:
        text = (
            "<b>راهنمای ادمین</b>\n\n"
            "➕ ساخت کاربر (/newuser) — یک کاربر جدید روی هر سرور/پروتکلی که بخواهید می‌سازد.\n"
            "📋 لیست کاربران (/users) — مرور و جستجوی کاربران؛ با زدن روی هرکدام وضعیت/مصرف و عملیات (فعال/غیرفعال، تمدید، ریست مصرف، حذف) در دسترس است.\n"
            "📥 درخواست‌های در انتظار (/pending) — رسیدهای پرداخت مشتری‌ها که هنوز تایید/رد نشده‌اند.\n"
            "📢 پیام همگانی (/broadcast) — ارسال یک پیام به همه کاربرانی که تلگرام‌شان به حساب وصل است.\n\n"
            "همه این‌ها از دکمه Menu کنار پیام هم در دسترس‌اند."
        )
    elif scope:
        text = (
            "<b>راهنمای ادمین گروه</b>\n\n"
            "➕ ساخت کاربر (/newuser) — یک کاربر جدید در گروه خودتان می‌سازد.\n"
            "📋 لیست کاربران من (/users) — فقط کاربران خودتان؛ با زدن روی هرکدام وضعیت/مصرف و عملیات (فعال/غیرفعال، تمدید، ریست مصرف، حذف) در دسترس است.\n\n"
            "همه این‌ها از دکمه Menu کنار پیام هم در دسترس‌اند."
        )
    else:
        text = (
            "<b>راهنما</b>\n\n"
            "👤 اکانت من (/account) — وضعیت، مصرف و اتصالات حساب شما.\n"
            "🛒 خرید اکانت جدید (/buy) — انتخاب پکیج، پرداخت کارت‌به‌کارت و ارسال رسید.\n"
            "💰 افزایش اعتبار (/topup) — شارژ کیف پول برای پرداخت فوری.\n"
            "📚 آموزش (/tutorials) — راهنمای نصب و اتصال سرویس‌ها.\n"
            "🔗 وصل کردن حساب قبلی (/link) — اگه قبلا از ادمین اکانت گرفته بودید.\n\n"
            "همه این‌ها از دکمه Menu کنار پیام هم در دسترس‌اند."
        )
    await message.answer(text, reply_markup=main_menu_kb(scope))


@router.callback_query(MenuCB.filter(F.action == "home"))
async def cb_home(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    scope = await resolve_admin_scope(call.from_user.id)
    text = await _welcome_text(call.from_user, scope)
    await call.message.edit_text(text, reply_markup=main_menu_kb(scope))
    await call.answer()


@router.callback_query(MenuCB.filter(F.action == "cancel"))
async def cb_cancel(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    scope = await resolve_admin_scope(call.from_user.id)
    text = await _welcome_text(call.from_user, scope)
    await call.message.edit_text("عملیات لغو شد.\n\n" + text, reply_markup=main_menu_kb(scope))
    await call.answer("لغو شد")


@router.callback_query(F.data == "noop")
async def cb_noop(call: CallbackQuery) -> None:
    await call.answer()
