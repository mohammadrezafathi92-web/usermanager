# بررسی کامل کد پروژه usermanager

- **تاریخ:** ۲۰۲۶-۰۸-۱۵
- **نسخهٔ پروژه:** 1.3.0
- **محدودهٔ بررسی:** بکاند (FastAPI + SQLAlchemy/SQLite)، ربات تلگرام، فرانت (React/Vite/Tailwind)، DevOps و استقرار
- **نوع بررسی:** باگها و مشکلات منطقی، امنیت، کیفیت کد و نگهداری، نقشهٔ معماری

---

## ۱. خلاصهٔ مدیریتی

پروژه یک پنل فروش VPN یکپارچه است که همهٔ سرویسها را در یک پردازهٔ FastAPI جمع کرده: ربات تلگرام (فروش/پشتیبانی/ادمین)، سرور RADIUS برای OpenVPN/L2TP، پولر مصرف سهمیه، بکآپ برنامهریزیشده و سلسلهمراتب ۳سطحی (superadmin → admin → seller). ساختار کلی تمیز، مستند و دارای فلسفهٔ دفاعی خوبی است و همهٔ ۱۱ باگِ ثبتشده در `BUG_AUDIT_2026-07-26.md` برطرف شدهاند.

با این حال **۴ ریسک بحرانی** وجود دارد که اگر بهزودی رفع نشوند، یک استقرار ساده میتواند بهطور کامل به خطر بیفتد:

1. **کلید JWT عمومی بهعنوان مقدار پیشفرض** (`change-this-secret-in-production`) — جعل توکن ادمین
2. **رمز پیشفرض ادمین `admin123`** با رفتار fail-open
3. **مونت docker.sock + اجرا بهصورت root** در کانتینر وب
4. **قابلیت HA بهعنوان ابزار خروج کامل دیتابیس** توسط هر ادمین سطح ۲

---

## ۲. نقشهٔ معماری

### ۲.۱ پردازهٔ واحد، سه سرویس پسزمینه
هر سه سرویس طولانی (پولر سهمیه، سرور RADIUS، ربات تلگرام) داخل همان پردازهٔ FastAPI و ایزوله از درخواست/پاسخ اجرا میشوند:
- **پولر سهمیه** (`services/quota_manager.poll_all`) — هر ۳۰ ثانیه نودها را بازدید میکند، دلتای مصرف را به `used_bytes` اضافه میکند و اتصالاتی که سهمیه/تاریخ انقضایشان تمام شده را غیرفعال میکند. نقطهٔ چوکِ اعمال محدودیت.
- **سرور RADIUS** (`services/radius_server.py`) — احراز و حساب OpenVPN/L2TP روی تِرِد و سوکت جداگانه.
- **ربات تلگرام** (`telegram_bot/runner.py`) — با event loop مخصوص خودش؛ از طریق `panel_bridge.py` مستقیم (بدون HTTP) یا در حالت مستقل از طریق `remote_bridge.py` + API کلید (`/api/bot`) به دیتابیس دسترسی دارد.

### ۲.۲ سه سیستم موازی «چه کسی تماس گرفته»
1. **پنل:** JWT Bearer (`deps.get_current_admin`)
2. **API ربات/بیرونی:** هدر ثابت `X-API-Key` (جدول `ApiKey`) — بدون مفهوم نشست ادمین
3. **پنل عمومی مشتری** (`routers/subscription.py`): هیچ هدر/توکنی، فقط `subscription_token` طولانیِ unguessable در URL

### ۲.۳ سلسلهمراتب ۳سطحی
`seller` با چکباکسهای `permissions.PERMISSION_CHOICES`، `admin` (سطح ۲، دسترسی کامل پنل ولی محدود به درخت خود)، `superadmin`. همهٔ مسیرها باید از `hierarchy.owned_admin_ids()`/`role()` عبور کنند، نه اینکه منطق را دوباره بازنویسی کنند.

### ۲.۴ مدل دوسطحی سهمیه
سهمیه/انقضا در دو سطح مستقل پیگیری میشود: `User.total_quota_bytes/used_bytes/expire_at` (سطح کاربر) و `Purchase` (سطح پکیج، برای «افزودن پکیج» روی کاربر موجود). کدی که مصرف/انقضا را اعمال میکند باید هر دو حالت را پوشش دهد.

---

## ۳. یافتههای امنیتی

### ۳.۱ بحرانی (CRITICAL)

| # | مشکل | مکان | تأثیر | راهحل |
|---|------|------|-------|--------|
| C1 | کلید JWT پیشفرضِ عمومی و شناختهشده | `backend/app/config.py:12` | جعل توکن ادمین → دسترسی کامل به همهٔ اعتبارنامههای نود | هنگام boot اگر SECRET_KEY مقدار پیشفرض بود، از شروع سرویس خودداری کن (fail-closed) یا کلید تصادفی بساز و ذخیره کن |
| C2 | رمز پیشفرض ادمین `admin123` | `config.py:23`, `main.py:327-334` | ورود راحت به پنل در اولین نصب | رمز تصادفی بساز و یکبار چاپ کن، یا اجباریکردن override |
| C3 | docker.sock + دایرکتوری پروژه + اجرا بهصورت root | `docker-compose.yml:30-31`, `backend/Dockerfile` | یک نفوذ = کنترل کامل داکرِ هاست | کاربر غیر-root؛ حذف یا محدودکردن مونت سوکت؛ مونت read-only |
| C4 | HA قابلاستفاده توسط ادمین سطح ۲ برای خروج دیتابیس | `routers/panel_settings.py:34,63-86,348-372`, `main.py:584-623` | هر ادمین سطح ۲ (یا نشست سرقتشده) میتواند `ha_peer_url` را به سرور خودش بزند و کل اسنپشات (همهٔ اعتبارنامهها) را دریافت کند | محدودکردن فیلدهای `ha_*` به superadmin؛ استفاده از `secrets.compare_digest`؛ رد URLهای غیر HTTPS؛ امضای HMAC روی اسنپشات |

### ۳.۲ بالا (HIGH)

| # | مشکل | مکان | راهحل |
|---|------|------|--------|
| H1 | CORS پیشفرض `*` + `allow_credentials=True` | `config.py:45-47` | محدودکردن به origin های واقعی پنل |
| H2 | بدون TLS در استقرار پیشفرض؛ پورت 8000 مستقیم باز + Swagger بدون لاگین | `docker-compose.yml:42`, `frontend/nginx.conf` | انتشار نکردن 8000 (یا باند به `127.0.0.1`)؛ TLS پیشفرض؛ JWT در Cookie امن بهجای localStorage |
| H3 | MariaDB روی `0.0.0.0:3306` با رمز پیشفرض `changeme` | `docker-compose.yml:57-72` | حذف پورتنگاشتِ هاست؛ fail-if-unset بهجای `changeme` |
| H4 | بایپس scope نودها در پکیجها و bot API | `routers/packages.py:81-92,137-171`, `bot.py:664-677` | اعتبارسنجی `spec.node_id` با `accessible_node_ids` در همهٔ مسیرها |
| H5 | `add_balance` و `link_telegram` در bot API بدون scope | `routers/bot.py:648-661,776-816` | افزودن `owner_admin_id` و اعمال آن سمت سرور (نه caller-supplied) |
| H6 | حذف/تخفیف ادمین سطح ۲، فروشندهها را به سطح ۲ ارتقا میدهد | `routers/admins.py:314-337,621-651` | تعیین جانشین صریح، یا غیرفعالکردن فروشندههای یتیم |
| H7 | پسورد PPP متن ساده در DB و بکآپهایی که از تلگرام رد میشوند | `services/backup.py` | رمزنگاری بکآپها؛ عدمدرج ستونهای حساس در بکآپ |

### ۳.۳ متوسط (MEDIUM)

| # | مشکل | مکان | راهحل |
|---|------|------|--------|
| M1 | ریست کانتر نود بهعنوان مصرف کامل حساب میشود (double-counting) | `services/user_ops.py` `_apply_delta` | تشخیص ریست (new < old) → صفر، نه کل مجموع |
| M2 | ریتلیت لاگین قابل دور زدن با هدر قابل جعل `X-Real-IP`؛ بدون قفل per-username | `routers/auth.py:29-39` | استخراج IP از سوکت هنگام نبود پروکسی مورداعتماد؛ throttling هر-کاربر |
| M3 | N+1 در لیست کاربران و پولر | `schemas.py:458-487` → `models.py:605-680` | `selectinload`/`joinedload` و کوئریهای گروهی |
| M4 | `PRAGMA foreign_keys` غیرفعال در SQLite | `services/database.py` | رویداد connect برای فعالکردن pragma |
| M5 | بدون راستیآزمایی TLS/SSH در اتصال به نودها | `mikrotik_client.py:97`, `threexui_client.py:132`, `xray_client.py:63` | قابلیت پینکردن سرتیفیکت/هوستکی بهصورت per-node |
| M6 | نشت فایل موقت export در `/tmp` | `routers/users.py`, `services/accounting.py` | `TemporaryDirectory` یا پاکسازی در `finally` |
| M7 | تایمینگ اُریکل لاگین | `routers/auth.py:67-68` | اجرای dummy verify برای username های ناموجود |
| M8 | کد تخفیف تکراری → خطای 500 | `routers/discount_codes.py` | catch کردن `IntegrityError` → 400 |
| M9 | بدون `.dockerignore` — دیتابیس زنده در build context | `backend/`, `frontend/` | افزودن `.dockerignore` |

### ۳.۴ پایین (LOW)

- **L1:** متون HTML ربات escape نمیشوند (`admin_users.py:52`, `start.py:46`) → رد شدن پیام (DoS موضعی)
- **L2:** ارجاع به پرمیژنهای منسوخ (`nodes.py` → `delete_nodes` دیگر در PERMISSION_CHOICES نیست)
- **L3:** `delete_node` پرمیژن `delete_nodes` را اجرا نمیکند
- **L4:** فایل `root@88.218.18.156` (اسکریپت نصب GlitchTip، نه کلید SSH!) در git رفته؛ شامل `POSTGRES_HOST_AUTH_METHOD: trust`
- **L5:** `install.sh` فایلهای `.env` را 0644 مینویسد و رمز ادمین را در حالت non-interactive چاپ میکند
- **L6:** `update.sh` بدون بکآپ/نقطهٔ بازگشت/چک درخت کثیف
- **L7:** `sed` جایگزینی پورت بدون اعتبارسنجی عددی و بدون امنیت در برابر تزریق (`install.sh:378-382`)
- **L8:** دانلودهای زمان اجرا بدون checksum (docker-compose، get.docker.com)
- **L9:** `BUG_AUDIT_2026-07-26.md` قدیمی است و ستون وضعیت ندارد

---

## ۴. باگها و مشکلات منطقی

| # | مشکل | مکان |
|---|------|------|
| B1 | double-counting پس از ریست کانتر نود (همان M1) | `user_ops._apply_delta` |
| B2 | race در رزرو IP کلاینت WireGuard؛ بدون constraint یکتا روی `(node_id, wg_client_address)` | `_wg_reserve_ips` |
| B3 | `_charge_admin_for_package` قبل از ساخت کاربر commit میکند (پنجرهٔ crash) | `user_ops` |
| B4 | `apply_referral_code` race → پاداش دوباره | `user_ops` |
| B5 | HA: تعویض DB زنده با `os.replace` در حالی که تِرِدهای RADIUS/poll ممکن است اتصال داشته باشند + حذف sidecar های `-wal`/`-shm` | `main.py` |
| B6 | بکآپها (SQLite online backup / mysqldump) بدون رمزنگاری | `services/backup.py` |
| B7 | حذف ادمین، ردیفهای `DiscountCode` را یتیم میکند (نامرئی برای همه) | `routers/admins.py` |
| B8 | پیام Stop قدیمیتر از Interim جدید → تفسیر بهعنوان ریست و اضافهشدن کل مجموع | `services/radius_server.py` (باقیماندهٔ LOW) |

---

## ۵. کیفیت کد و نگهداری

### نقاط قوت
- مدل سهمیه واقعاً درست است: افزایش اتمی (`col = col + delta`)، اعمال مستقل در سطح Purchase، بازگشت وجه هنگام فعالسازی ناموفق
- قفلِ `claim_pending` (CAS) جلوی double-approval را میگیرد
- معماری دو بریج ربات (in-process/HTTP) شفاف است؛ کد handlers نمیداند از کدام بریج استفاده میکند
- انضباط قوی در scoping وبپنل (`_get_scoped_node`/`owned_admin_ids` با 404-not-403)
- مدیریت محرمانهها خوب: بدون توکن در پاسخها، `secrets.token_urlsafe(32)`، `.env`، محدودیت آپلود

### ضعفها
- فایلهای غولپیکر: `user_ops.py` (۱۰۵KB)، `Settings.jsx` (۷۹KB)، `UserDetail.jsx` (۷۵KB)، `models.py` (۹۵KB)
- بدون تست، لینتر، CI (فقط py_compile)
- کد مرده/نظرهای منسوخ (مثلاً `api/client.js:30-31`)
- `BUG_AUDIT` بروزرسانی نشده — چند موردش حل شده ولی «باز» خوانده میشود

---

## ۶. DevOps و استقرار

- **کالبدشکافی:** همهچیز از طریق Docker Compose؛ backend با docker.sock و دایرکتوری پروژه بهصورت read-write و root
- پورتهای باز: 80 (پنل)، 8000 (API مستقیم + Swagger)، 1812/1813/UDP (RADIUS)، 3306 (فقط با پروفایل mariadb)
- بدون TLS پیشفرض؛ README پشت HTTPS گذاشتن را بهعهدهٔ کاربر میگذارد
- نسخههای base image بدون digest دقیق؛ بدون `.dockerignore`
- نکتهٔ مثبت: `x-logging` با چرخش لاگ (۳×۲۰MB) مشکل پرشدن دیسک ۲۰۲۶-۰۸-۰۹ را حل کرده

---

## ۷. نقشهٔ راه اصلاح (اولویتپیشنهادی)

**فاز ۱ — فوری (بحرانی):**
1. C1: fail-closed برای SECRET_KEY پیشفرض
2. C2: رمز پیشفرض ادمین → تصادفی/اجباری
3. C4: محدودکردن فیلدهای HA به superadmin + `compare_digest` + رد HTTP
4. C3: کاربر غیر-root + بازنگری مونت docker.sock

**فاز ۲ — مهم (HIGH):**
5. CORS دقیق، حذف پورت 8000 از هاست، TLS
6. MariaDB بدون `changeme` و بدون پورت هاست
7. اعتبارسنجی scope نودها در پکیجها/ربات
8. `owner_admin_id` اجباری در `add_balance`/`link_telegram`
9. سیاست جانشین/تعطیلی برای فروشندههای یتیم

**فاز ۳ — کیفی (MEDIUM):**
10. رفع double-counting پولر، N+1 ها، foreign_keys pragma، پاکسازی tempfile، catch کد تخفیف، timing oracle

**فاز ۴ — پاکسازی (LOW):**
11. حذف/تغییر نام فایل `root@88.218.18.156`، بروزرسانی BUG_AUDIT با ستون وضعیت، `.dockerignore`، `chmod 600` فایلهای `.env`، escape کردن HTML ربات

---

## ۸. منابع و یادداشتها

- تمام ارجاعهای `file:line` از نسخهٔ فعلی (۱.۳.۰) است
- چند ادعا (PAP `PwDecrypt` در pyrad، رفتار MS-CHAPv2 در ربات واقعی) باید در محیط زنده راستیآزمایی شوند
- README بهطور صریح برخی ریسکها (کلید JWT عمومی، بدون TLS، AutoAddPolicy) را «عمدی» اعلام کرده — این موارد تغییر سیاست میخواهند، نه فقط کد
