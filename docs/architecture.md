# نقشه کلی پروژه یوزر منیجر

مرجع یک‌نگاهی کل سیستم - برای دیدن جای هر قابلیت و مسیر داده‌ها.
(آخرین به‌روزرسانی: 2026-08-08 · طرح حساب‌داری: `accounting-design.md`)

```mermaid
flowchart TB
    subgraph L1["۱ · مشتری نهایی"]
        CUST["مشتری VPN<br/>خرید / تمدید / شارژ کیف پول"]
        SUB["صفحه اشتراک عمومی<br/>/s/token بدون لاگین"]
    end

    subgraph L2["۲ · رابط‌ها"]
        PANEL["پنل وب React<br/>داشبورد · کاربران · سرورها · پکیج‌ها<br/>حساب‌داری · کد تخفیف · آموزش · تنظیمات · ادمین‌ها"]
        BOT["ربات تلگرام aiogram<br/>فروش + تایید رسید<br/>داخلی یا سرور دوم (X-API-Key)"]
    end

    subgraph L3["۳ · هسته FastAPI"]
        AUTH["هویت و سلسله‌مراتب<br/>سوپرادمین ← ادمین ← فروشنده"]
        USERS["کاربران و سرویس‌ها<br/>Purchase مستقل · سهمیه/انقضا"]
        MONEY["مالی<br/>دفتر کل · کارت‌ها · تخفیف · کیف پول · اعتبار"]
        PKG["پکیج‌ها<br/>قیمت مشتری/همکاری/فروشنده"]
        MAINT["نگه‌داری<br/>بکاپ+HA · نصب ربات سرور دوم · کلید API"]
    end

    subgraph L4["۴ · سرویس‌های زمینه"]
        RADIUS["سرور RADIUS داخلی<br/>احراز · محدودیت همزمانی"]
        POLL["پایش مصرف<br/>قطع خودکار · تمدید رزروی"]
        SCHED["زمان‌بند<br/>نوتیف · بکاپ · پاک‌سازی"]
    end

    subgraph L5["۵ · زیرساخت"]
        MIK["نودهای MikroTik<br/>WG · OVPN · L2TP · IKEv2 · SSTP"]
        XRAY["نودهای Xray / 3X-UI<br/>VLESS / VMess / Trojan"]
        DB[("دیتابیس<br/>SQLite یا MySQL/MariaDB")]
    end

    CUST --> BOT
    CUST --> SUB
    SUB --> USERS
    PANEL --> AUTH
    BOT --> AUTH
    AUTH --> USERS
    AUTH --> MONEY
    USERS --> PKG
    USERS --> POLL
    MONEY --> DB
    USERS --> DB
    MAINT --> DB
    POLL --> MIK
    POLL --> XRAY
    RADIUS --> MIK
    USERS --> MIK
    USERS --> XRAY
    SCHED --> POLL
```

## لایه‌ها در یک نگاه

**رابط‌ها.** پنل وب React (سرو از کانتینر nginx) و ربات تلگرام. ربات یا در همان کانتینر بک‌اند اجرا می‌شود (`panel_bridge` - فراخوانی مستقیم) یا روی سرور دوم (`remote_bridge` - HTTP به `/api/bot/*` با X-API-Key؛ استقرار از تنظیمات ← «نصب ربات روی سرور دیگر» با SSH). **نکته عملیاتی مهم: آپدیت سرور اصلی، سرور دومِ ربات را خودکار آپدیت نمی‌کند - باید نصب ربات را دوباره زد.**

**هسته.** FastAPI + SQLAlchemy. سلسله‌مراتب سه‌سطحی (`services/hierarchy.py`) روی همه‌چیز اعمال می‌شود: هر ادمین فقط درخت خودش را می‌بیند. هر خرید یک `Purchase` مستقل با سهمیه/انقضای خودش است (سرویس‌های قدیمیِ استخر مشترک با مهاجرت یک‌باره‌ی `services/purchase_migration.py` تبدیل شده‌اند - 2026-08-09). **تمدید = ادامه‌ی همان سرویس**: پکیج جدید روی همان Purchase رزرو می‌شود (نه سرویس جدید)؛ ربات هنگام تمدیدِ مشتریِ چندسرویسه اول می‌پرسد کدام سرویس. بخش مالی همه رویدادها را در دفتر کل (`ledger_entries`) ثبت می‌کند - جزئیات در `accounting-design.md`.

**سرویس‌های زمینه.** سرور RADIUS داخلی برای احراز PPP/L2TP/IKEv2/SSTP و محدودیت اتصال همزمان؛ پایش دوره‌ای مصرف که سهمیه/انقضا را اجرا و تمدیدهای رزروی را فعال می‌کند؛ زمان‌بند APScheduler برای نوتیف روزانه، بکاپ زمان‌بندی‌شده و HA.

**زیرساخت.** نودهای MikroTik (API روتر) و Xray/3X-UI (SSH یا API). دیتابیس SQLite پیش‌فرض یا MySQL/MariaDB (انتخاب هنگام نصب)؛ ستون‌های جدید در استارتاپ خودکار مهاجرت می‌شوند.

## پرونده‌های کلیدی برای هر حوزه

| حوزه | فایل‌های اصلی |
|---|---|
| مدل‌های داده | `backend/app/models.py` |
| ساخت/تمدید/Purchase | `backend/app/services/user_ops.py` · `routers/users.py` |
| حساب‌داری | `services/accounting.py` · `routers/accounting.py` · `frontend/src/pages/Accounting.jsx` |
| کارت‌های پرداخت | `services/payment_cards.py` |
| ربات | `backend/app/telegram_bot/` (handlers، bridges، storage) |
| RADIUS و پایش | `services/radius_server.py` · `services/quota_manager.py` |
| نودها | `services/mikrotik_client.py` · `services/xray_client.py` · `services/threexui_client.py` |
| صفحه ساب مشتری | `routers/subscription.py` · `frontend/src/pages/Subscription.jsx` |
| قالب OpenVPN پکیج | `services/link_builder.py` (`render_ovpn_template`) · `models.Package.ovpn_template` |
| مانیتور منابع نودها | `services/node_monitor.py` · `routers/nodes.py` (`/nodes/resources`) |
| بکاپ/HA | `services/backup.py` |
| استقرار ربات سرور دوم | `services/remote_deploy.py` · `routers/remote_bot.py` |
