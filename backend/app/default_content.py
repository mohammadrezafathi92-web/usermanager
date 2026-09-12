"""Ready-made tutorials and adverts a panel can import with one button.

Deliberately a module at the top of the package rather than a data/
directory: .gitignore carries a bare `data/` rule (for the runtime volume
docker-compose mounts at /app/data), and that pattern matches a directory
of that name ANYWHERE - so a first attempt at app/data/defaults.py was
silently skipped by `git add`, committed as nothing, and took the backend
down on deploy with ModuleNotFoundError. Content that ships with the code
belongs beside the code.

Why a plain Python file rather than rows seeded at startup: this is content,
not schema. It is meant to be READ, edited in a text editor, and re-imported
- and an admin who edits or deletes an imported tutorial must not have it
quietly reappear on the next restart. So nothing here is written
automatically; it only ever lands in the database when someone presses
"ایمپورت پیش‌فرض" (see routers/tutorials.py and routers/ads.py).

Importing is additive and idempotent by TITLE: a title that already exists
for that owner is skipped, so pressing the button twice adds nothing the
second time and an admin's own edits are never overwritten. Deletions are
not remembered - re-importing brings a deleted entry back, because the
button means "give me the defaults I do not have" and hidden
tombstone state would surprise in the other direction.

The adverts use services/ads.py's placeholders ({package}, {price}, {bot},
...), which are rendered at SEND time - so an advert imported today keeps
showing the right price after a package is re-priced tomorrow. An advert
with a {package}/{price} placeholder needs a package attached to it before
it is useful; the import leaves that unset deliberately, because which
package to advertise is the admin's choice, not something a default can
guess.
"""
from __future__ import annotations

# --------------------------------------------------------------- tutorials
# text supports the same Telegram HTML the bot already sends.
DEFAULT_TUTORIALS: list[dict] = [
    {
        "title": "اتصال با WireGuard در اندروید",
        "text": (
            "۱. اپلیکیشن <b>WireGuard</b> را از گوگل‌پلی یا سایت رسمی نصب کنید.\n"
            "۲. فایل کانفیگی که از ربات گرفتید را ذخیره کنید.\n"
            "۳. در اپ روی <b>+</b> بزنید و گزینه‌ی «Import from file or archive» را انتخاب کنید.\n"
            "۴. فایل را انتخاب کنید و کلید کنار نام کانفیگ را روشن کنید.\n\n"
            "اگر وصل نشد: یک‌بار اینترنت گوشی را خاموش و روشن کنید، "
            "یا از اینترنت دیگری (مثلاً داده‌ی همراه به‌جای وای‌فای) امتحان کنید."
        ),
    },
    {
        "title": "اتصال با WireGuard در ویندوز",
        "text": (
            "۱. برنامه‌ی <b>WireGuard</b> را از سایت رسمی wireguard.com دانلود و نصب کنید.\n"
            "۲. روی <b>Import tunnel(s) from file</b> بزنید.\n"
            "۳. فایل کانفیگ را انتخاب کنید.\n"
            "۴. روی <b>Activate</b> بزنید.\n\n"
            "اگر خطای Permission گرفتید، برنامه را با «Run as administrator» باز کنید."
        ),
    },
    {
        "title": "اتصال با V2Ray / V2rayNG در اندروید",
        "text": (
            "۱. اپ <b>v2rayNG</b> را نصب کنید.\n"
            "۲. لینک اشتراک (Subscription) را از ربات کپی کنید.\n"
            "۳. در اپ به بخش <b>Subscription group setting</b> بروید و با <b>+</b> لینک را اضافه کنید.\n"
            "۴. از منو <b>Update subscription</b> را بزنید تا سرورها بیایند.\n"
            "۵. یک سرور را انتخاب و دکمه‌ی اتصال را بزنید.\n\n"
            "نکته: هر چند وقت یک‌بار «Update subscription» را بزنید تا سرورهای جدید اضافه شوند."
        ),
    },
    {
        "title": "اتصال با V2Box در آیفون",
        "text": (
            "۱. اپ <b>V2Box</b> را از اپ‌استور نصب کنید.\n"
            "۲. لینک اشتراک را از ربات کپی کنید.\n"
            "۳. در تب <b>Config</b> روی <b>+</b> بزنید و <b>Add Subscription</b> را انتخاب کنید.\n"
            "۴. لینک را جای‌گذاری و ذخیره کنید.\n"
            "۵. یک کانفیگ را انتخاب و اتصال را بزنید.\n\n"
            "اگر اپ در اپ‌استور ایران پیدا نشد، از اکانت اپ‌استور کشور دیگری استفاده کنید."
        ),
    },
    {
        "title": "اتصال با OpenVPN",
        "text": (
            "۱. اپ <b>OpenVPN Connect</b> را نصب کنید (اندروید، آیفون، ویندوز).\n"
            "۲. فایل <code>.ovpn</code> را از ربات دریافت کنید.\n"
            "۳. در اپ گزینه‌ی <b>Import Profile → File</b> را بزنید و فایل را انتخاب کنید.\n"
            "۴. نام کاربری و رمزی که ربات داده را وارد کنید.\n"
            "۵. اتصال را روشن کنید."
        ),
    },
    {
        "title": "چرا سرعتم کم شده؟",
        "text": (
            "چند دلیل رایج:\n\n"
            "• <b>حجم تمام شده</b> — از بخش «اکانت من» در ربات حجم باقی‌مانده را ببینید.\n"
            "• <b>شلوغی شب</b> — ساعات ۲۱ تا ۲۴ پرترافیک‌ترین زمان است.\n"
            "• <b>سرور نامناسب</b> — اگر چند سرور دارید، یکی دیگر را امتحان کنید.\n"
            "• <b>اینترنت خودتان</b> — یک‌بار بدون اتصال تست سرعت بگیرید تا مشخص شود.\n\n"
            "اگر هیچ‌کدام نبود، به پشتیبانی پیام بدهید."
        ),
    },
    {
        "title": "تمدید اکانت",
        "text": (
            "۱. در ربات وارد بخش <b>اکانت من</b> شوید.\n"
            "۲. سرویسی که می‌خواهید تمدید کنید را انتخاب کنید.\n"
            "۳. دکمه‌ی <b>تمدید</b> را بزنید و بسته را انتخاب کنید.\n"
            "۴. پس از پرداخت، حجم و تاریخ روی همان سرویس اضافه می‌شود.\n\n"
            "نکته: با تمدید، کانفیگ‌های فعلی‌تان عوض نمی‌شود و لازم نیست چیزی را دوباره اضافه کنید."
        ),
    },
]

# ----------------------------------------------------------------- adverts
# `body` goes through services/ads.py's render() at send time.
DEFAULT_ADS: list[dict] = [
    {
        "title": "معرفی کلی",
        "body": (
            "🚀 <b>اینترنت بدون قطعی، با سرعت واقعی</b>\n\n"
            "• پشتیبانی از WireGuard و V2Ray\n"
            "• اتصال روی موبایل، ویندوز و مک\n"
            "• تحویل آنی و خودکار\n"
            "• پشتیبانی پاسخگو\n\n"
            "همین حالا از {bot} سفارش بدهید."
        ),
    },
    {
        "title": "معرفی یک پکیج (نیاز به انتخاب پکیج)",
        "body": (
            "🔥 <b>{package}</b>\n\n"
            "📦 حجم: {quota}\n"
            "⏳ مدت: {days} روز\n"
            "💰 قیمت: {price} تومان\n\n"
            "تحویل آنی از {bot}"
        ),
    },
    {
        "title": "کد تخفیف (نیاز به انتخاب کد)",
        "body": (
            "🎁 <b>کد تخفیف ویژه</b>\n\n"
            "کد: <code>{code}</code>\n"
            "اعتبار تا: {code_expires}\n\n"
            "کد را هنگام خرید در {bot} وارد کنید."
        ),
    },
    {
        "title": "یادآوری تمدید",
        "body": (
            "⏰ <b>سرویس‌تان رو به پایان است؟</b>\n\n"
            "قبل از قطع شدن، از بخش «اکانت من» در {bot} تمدید کنید.\n"
            "با تمدید، کانفیگ‌های فعلی‌تان تغییر نمی‌کند."
        ),
    },
    {
        "title": "چرا ما",
        "body": (
            "❓ <b>چرا سرویس ما</b>\n\n"
            "• بدون محدودیت سرعت\n"
            "• چند سرور، قابل تعویض\n"
            "• قابل استفاده روی چند دستگاه\n"
            "• راهنمای کامل نصب داخل ربات\n\n"
            "{bot}"
        ),
    },
]
