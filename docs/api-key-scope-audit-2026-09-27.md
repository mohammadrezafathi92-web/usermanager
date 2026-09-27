# تحلیل: عدم وجود owner/scope روی X-API-Key (`/api/bot/*`)

**مرحله:** ۳ (تحلیل + reproduction test فقط - طبق دستور، بدون enforcement/migration)
**وضعیت:** تحلیل تمام‌شده، منتظر تایید طراحی قبل از پیاده‌سازی
**تست بازتولید:** `backend/tests/test_bot_api_key_cross_tenant.py` (۱۲ assertion، همه سبز روی HEAD فعلی)
**رمز مادر:** کاملاً خارج از scope این تحلیل - هیچ فایل/کدی مربوط به آن بررسی یا لمس نشد.

## خلاصه‌ی مشکل

`models.ApiKey` (`backend/app/models.py:412`) یک ردیف تخت است: `id, label, key, enabled, created_at, last_used_at` - هیچ `owner_admin_id`/scope ندارد. `deps.get_bot_api_key` (`backend/app/deps.py:114`) فقط بررسی می‌کند که کلید معتبر و enabled است؛ همان یک شیء `ApiKey` برای هر endpoint روی `routers/bot.py` یکسان است.

اسکوپ واقعی دیتا (کدام تنانت) روی اکثر endpointها توسط یک پارامتر `owner_admin_id` تعیین می‌شود که **مستقیم از query/payload خوانده می‌شود** - نه از هویت کلید. `_visibility_filter`/`_get_user_or_404` (خطوط ۱۵۱ و ۲۷۱) این پارامتر را به‌درستی و کامل به فیلتر hierarchy تبدیل می‌کنند - منطق فیلتر خودش درست است؛ مشکل این است که **هیچ‌چیز بررسی نمی‌کند این caller اصلاً حق دارد این `owner_admin_id` را ادعا کند یا نه.**

## کالرهای واقعی (تایید‌شده با خواندن کد)

| کالر | مسیر | نحوه‌ی احراز هویت | owner_admin_id چطور تعیین می‌شود |
|---|---|---|---|
| ربات مشترک داخلی (in-process) | `telegram_bot/panel_bridge.py` صدا می‌زند مستقیم توابع پایتون `routers/bot.py` را - **بدون HTTP، بدون X-API-Key اصلاً** | ندارد (in-process) | `panel_bridge._scope()`: `config.bot_owner_admin_id` (که برای ربات مشترک `None` است) مگر caller خودش صریح بدهد |
| ربات اختصاصی admin/seller (in-process) | همان `panel_bridge.py`، thread جدا با `config.bot_owner_admin_id` مخصوص خودش (`telegram_bot/runner.py: start_admin_bot`) | ندارد (in-process) | همان `_scope()` - چون این ترد `config.bot_owner_admin_id` را روی id همان ادمین ست کرده |
| Remote bot (فقط حالت global/shared - تایید شد `routers/remote_bot.py` هیچ مفهوم per-admin ندارد و فقط یک `BotSettings` سراسری دارد) | `telegram_bot/remote_bridge.py` - HTTP واقعی با `X-API-Key` | یک کلید `ApiKey` تازه، فقط هنگام deploy ساخته می‌شود (`routers/remote_bot.py:90`)، قبلی‌اش فقط `disable` می‌شود | **هیچ owner_admin_id به‌صورت hardcode در env سرور دوم نمی‌رود** - همان کد `panel_bridge`-معادل با `bot_owner_admin_id=None` صدا می‌زند، یعنی remote bot همیشه global/بدون‌مقیاس است |
| Integration شخص ثالث | مستند در README («API ربات مشتری») - HTTP با `X-API-Key` ساخته‌شده از Settings → کلیدهای API (`routers/api_keys.py`، فقط superadmin) | هر مقداری که خودش در query/payload بگذارد - **کاملاً بدون محدودیت** |

**نتیجه‌ی کلیدی:** طبق مستندسازی خودِ `routers/api_keys.py` («Confirmed with the panel owner that these keys are only ever created/used by the superadmin themself»)، این یک تصمیم قبلاً پذیرفته‌شده بوده که کلیدها فقط دست سوپرادمین باشند - نه یک oversight. اما چون خودِ کلید هیچ scope ندارد، **همان یک کلید معتبر (هرکسی که آن را داشته باشد - سوپرادمین، یک اسکریپت شخص ثالث، یا هرکسی که آن را leak کرده) می‌تواند به‌جای هر ادمین/فروشنده‌ای در سراسر پنل عمل کند**، بدون اینکه چیزی در سیستم این را «غیرعادی» تشخیص دهد. حتی اگر تهدید صرفاً «سوپرادمین با ابزار شخص ثالث خودش» باشد، این مسیر از خودِ session وب سوپرادمین **مجوز بیشتری** دارد: طبق `hierarchy.py`، سوپرادمین در پنل وب فقط مشتریان خودش را می‌بیند، ولی همین یک کلید API می‌تواند با گذاشتن `owner_admin_id` دلخواه، به‌جای هر ادمین دیگری هم عمل کند - چیزی که هیچ صفحه‌ای در پنل وب به او نمی‌دهد.

## جدول endpoint به endpoint

«Scope فعلی» یعنی چه چیزی امروز واقعاً اعمال می‌شود. «امکان Cross-tenant» با ✅/❌/⚠️ (⚠️ = فقط اگر caller عمداً/اشتباهاً owner_admin_id نادرست/خالی بفرستد).

| Endpoint | Method | Caller(ها) | Scope فعلی | Scope صحیح | Cross-tenant | Compat risk |
|---|---|---|---|---|---|---|
| `/nodes` | GET | همه | **هیچ‌کدام** - همه‌ی نودهای enabled | نودها زیرساخت مشترک‌اند نه داده‌ی تنانت - این احتمالاً واقعاً باید global بماند (نودها را ادمین‌ها به هم اختصاص می‌دهند، نه مالک می‌شوند) | ❌ (by design, ولی مستند نبود) | هیچ - مستندسازی کافی است |
| `/packages` | GET | همه | `owner_admin_id` caller-supplied، ولی منطق فیلتر داخلش کامل و درست است (hierarchy) | باید فقط بگذارد caller `owner_admin_id`ای را ادعا کند که کلیدش واقعاً برایش صادر شده | ⚠️ | کم - همان فیلتر می‌ماند، فقط ورودی محدود می‌شود |
| `/payment-info` | GET | همه | مثل packages | مثل packages | ⚠️ | کم |
| `/payment-cards/{card_id}` | GET | همه | **هیچ‌کدام** - `card_id` مستقیم `db.get` | باید ownership کارت بررسی شود | ✅ | کم - فقط یک admin/seller مشروع همین را صدا می‌زند |
| `/payment-cards/{card_id}/record-payment` | POST | admin_pending.py (تایید رسید) | **هیچ‌کدام** | باید ownership کارت بررسی شود | ✅ | کم |
| `/sales-stats` | GET | admin menu | مثل packages (`owner_admin_id`) | مثل packages | ⚠️ | کم |
| `/customer-menu-config` | GET | همه بات‌ها | global by design (یک `BotSettings` سراسری) | همین‌طور بماند | ❌ (by design) | هیچ |
| `/tutorials` | GET | همه | مثل packages | مثل packages | ⚠️ | کم |
| `/packages/{id}/files/{id}/download` | GET | remote bot | **هیچ‌کدام** - فایل با id مستقیم | باید تعلق فایل به پکیج‌های در دسترسِ caller بررسی شود | ✅ | کم |
| `/tutorials/{id}/media/{id}/download` | GET | remote bot | **هیچ‌کدام** | مثل بالا | ✅ | کم |
| `/tutorials/{id}/software/{id}/download` | GET | remote bot | **هیچ‌کدام** | مثل بالا | ✅ | کم |
| `/admin-by-telegram/{tg_id}` | GET | ربات داخلی (تشخیص group-admin) | چیزی به caller بازنمی‌گرداند که تنانت خاصی را افشا کند به‌جز خودِ admin match‌شده | فعلاً بی‌خطر - خودِ AdminUser hierarchy | ❌ | هیچ |
| `/admin-username/{admin_id}` | GET | admin_pending.py | **هیچ‌کدام** - هر `admin_id`ای پذیرفته می‌شود | فقط یک username برمی‌گرداند، افشای کم | ⚠️ (کم‌اهمیت) | هیچ |
| `/telegram-user-ids` | GET | broadcast (📢 پیام همگانی) | مثل packages | مثل packages | ⚠️ (**پرخطر اگر رعایت نشود** - broadcast به مشتریان تنانت دیگر) | کم |
| `/users` (POST = create_user) | POST | admin_pending.py، bot خرید | `owner_admin_id` مستقیم در payload، **بدون هیچ اعتبارسنجی** | باید فقط owner_admin_idِ مجاز برای این کلید پذیرفته شود | ✅ (اثبات‌شده در تست) | **متوسط** - این ONE choke point ساخت کاربر جدید است؛ باید با احتیاط طراحی شود |
| `/users/{u}/purchase-package` | POST | خرید پکیج دوم | `owner_admin_id` caller-supplied | مثل بالا | ⚠️ | کم |
| `/referral/apply` | POST | بعد از create_user | فقط `username` - از طریق آن کاربر hierarchy در دسترس نیست مستقیم | نیاز به بررسی مالکیت user | ⚠️ (کم‌اهمیت - فقط reward می‌دهد) | کم |
| `/discount/validate`, `/discount/redeem` | POST | خرید | `owner_admin_id` caller-supplied | مثل packages | ⚠️ | کم |
| `/users` (GET = list_users) | GET | admin منو | `owner_admin_id` caller-supplied | مثل packages | ⚠️ (**پرخطر** - کل لیست مشتریان تنانت دیگر) | کم |
| `/users/by-telegram/{id}` | GET | /start | `owner_admin_id` caller-supplied | مثل packages | ⚠️ | کم |
| `/users/by-telegram/{id}/all` | GET | account picker | `owner_admin_id` caller-supplied | مثل packages | ⚠️ | کم |
| `/users/{username}` (GET) | GET | همه‌جا | `owner_admin_id` caller-supplied (`_get_user_or_404`) | مثل packages | ✅ (اثبات‌شده) | کم |
| `/users/{u}/link-telegram` | POST | «وصل کردن حساب قبلی» | **هیچ پارامتر owner_admin_id در signature اصلاً وجود ندارد** | باید اضافه و اجباری شود | ✅ (اثبات‌شده - جدی‌ترین مورد، هم‌ارز account takeover) | **متوسط** - باید بررسی شود چند caller واقعی این را بدون owner صدا می‌زنند |
| `/users/{u}/connections` (POST) | POST | افزودن سرویس دستی | `owner_admin_id` caller-supplied | مثل packages | ⚠️ | کم |
| `/users/{u}/purchases` (GET) | GET | «کدام سرویس؟» picker | `owner_admin_id` caller-supplied | مثل packages | ⚠️ | کم |
| `/users/{u}/purchases/{id}/rename` | POST | تغییر نام سرویس | `owner_admin_id` caller-supplied | مثل packages | ⚠️ | کم |
| `/users/{u}/purchases/{id}` (DELETE) | DELETE | حذف خودِ مشتری | `owner_admin_id` caller-supplied + بررسی expired/quota_exceeded | مثل packages | ⚠️ | کم |
| `/users/{u}/connections/{id}` (DELETE) | DELETE | مثل بالا | مثل بالا | مثل packages | ⚠️ | کم |
| `/users/{u}/subscription-link` | GET | «لینک ساب» | `owner_admin_id` caller-supplied | مثل packages | ⚠️ | کم |
| `/miniapp-button-text`, `/panel-public-url` | GET | runner.py | global by design (یک تنظیمات سراسری) | همین‌طور بماند | ❌ (by design) | هیچ |
| `/users/{u}/purchases/{id}/renew` | POST | تمدید سرویس مشخص | `owner_admin_id` caller-supplied | مثل packages | ⚠️ | کم |
| `/users/{u}/renew` | POST | تمدید legacy | `owner_admin_id` caller-supplied | مثل packages | ⚠️ | کم |
| `/users/{u}/reset-usage` | POST | ریست مصرف | `owner_admin_id` caller-supplied | مثل packages | ⚠️ | کم |
| `/users/{u}/set-enabled` | POST | فعال/غیرفعال دستی | `owner_admin_id` caller-supplied | مثل packages | ✅ (اثبات‌شده) | کم |
| `/users/{u}/add-balance` | POST | شارژ کیف پول/پرداخت از کیف پول | **هیچ پارامتر owner_admin_id در signature اصلاً وجود ندارد** | باید اضافه و اجباری شود | ✅ (اثبات‌شده - جدی‌ترین مورد، دستکاری مستقیم پول) | **متوسط** - پرکاربردترین endpoint نوشتنی است، باید با دقت پوشش داده شود |
| `/users/{u}` (DELETE) | DELETE | حذف کاربر | `owner_admin_id` caller-supplied | مثل packages | ⚠️ | کم |

## بررسی ویژه‌ی موارد خواسته‌شده

- **`add_balance`, `link_telegram`**: تایید شد - **صفر** پارامتر scope در امضای هر دو تابع و در `panel_bridge.py`'s interface هم همین‌طور. این دو مورد را نمی‌شود با «caller باید owner_admin_id درست بفرستد» حل کرد چون اصلاً جایی برای فرستادنش نیست - باید امضای endpoint و اسکیمای payload عوض شود.
- **`create_user`**: `payload.owner_admin_id` مستقیم به `user_ops.create_user_record` می‌رود، بدون تایید اینکه caller حق ساختن زیر آن ادمین را دارد.
- **`list_nodes`**: کاملاً global، بدون owner_admin_id در امضا. تصمیم طراحی: نودها زیرساخت مشترک‌اند (مثل `permissions.py`'s node access model که جدا از owner_admin_id کار می‌کند) - این احتمالاً «صحیح» است، نه یک باگ، ولی باید صریح مستند/تایید شود که همین‌طور بماند.
- **`set-enabled/reset-usage/delete/renew`**: همه `owner_admin_id` Optional caller-supplied دارند و از `_get_user_or_404` رد می‌شوند - منطق فیلتر درست است، مشکل فقط «چه کسی حق دارد کدام owner_admin_id را ادعا کند».
- **Payment cards و `record-payment`**: هیچ بررسی مالکیت کارت وجود ندارد - یک کلید می‌تواند شماره کارت هر ادمین دیگری را با فقط داشتن `card_id` بخواند، یا bookkeeping چرخش کارت او را با payment جعلی دستکاری کند.
- **`owner_admin_id` در query vs payload**: هر دو حالت با همین یک اصل مشکل دارند - محل قرارگیری پارامتر فرقی نمی‌کند، مسئله این است که هیچ‌کدام در برابر هویت کلید تایید نمی‌شوند.

## مشکل جداگانه: plaintext key + نمایش در `ApiKeyOut`

`models.ApiKey.key` plaintext در DB ذخیره می‌شود؛ `schemas.ApiKeyOut` (خط ۸۱۰) همین مقدار خام را در `GET /api/api-keys` (لیست کامل) هم برمی‌گرداند - یعنی هر کسی که یک بار به پنل ادمین (یا یک backup دیتابیس) دسترسی پیدا کند، تمام کلیدهای فعال را plaintext می‌بیند، نه فقط لحظه‌ی ساخت. این جدا از مشکل scope است ولی از همان مسیر طراحی (migration به hash + نمایش یک‌بار) قابل حل است.

## طراحی پیشنهادی (فقط طرح - هنوز پیاده‌سازی نشده)

**اصل کلیدی که Product تاکید کرده باید حفظ شود: نه shared bot نه remote bot حتی چند ثانیه قطع نشوند.**

1. **Migration additive روی `ApiKey`:**
   - `owner_admin_id` (nullable - `NULL` = «global»، دقیقاً معادل رفتار امروزِ remote bot/shared bot)
   - `key_hash` (ستون جدید) + نگه‌داشتن موقت `key` plaintext برای دوره‌ی گذار
   - `key_prefix`/`last4` برای نمایش در UI بدون افشای کامل کلید
   - `created_by_admin_id` (اختیاری، فقط برای لاگ)

2. **Compatibility mode (فاز اول - enforcement خاموش):**
   - کلیدهای موجود همه `owner_admin_id = NULL` می‌گیرند (دقیقاً رفتار فعلی‌شان - چون امروز واقعاً global عمل می‌کنند) - **هیچ کلید قدیمی از کار نمی‌افتد.**
   - `get_bot_api_key` فقط `owner_admin_id` خودِ کلید را روی خروجی‌اش حمل می‌کند؛ endpointها هنوز فعلاً به caller-supplied owner_admin_id هم اعتماد می‌کنند (رفتار فعلی، بدون تغییر) - این فاز فقط **زیرساخت** را می‌گذارد، رفتار را عوض نمی‌کند.
   - لاگ (نه رد) هر باری که caller-supplied owner_admin_id با کلید.owner_admin_id (وقتی کلید غیر-NULL است) فرق دارد - برای دیدن قبل از enforcement که آیا اصلاً چنین الگویی در ترافیک واقعی وجود دارد.

3. **Enforcement (فاز دوم - بعد از یک دوره‌ی observation):**
   - اگر کلید `owner_admin_id` غیر-NULL دارد: caller-supplied owner_admin_id باید یا خالی باشد (پیش‌فرض = owner خودِ کلید) یا دقیقاً برابر با آن، وگرنه 403.
   - اگر کلید `owner_admin_id = NULL` است (کلیدهای legacy + remote bot + shared bot): رفتار فعلی بدون تغییر (چون این دقیقاً معنای «global» است که از قبل هم داشتند).
   - `link_telegram`/`add_balance`: پارامتر `owner_admin_id` اضافه و از همان مسیر بررسی می‌شود.
   - Payment cards: بررسی `card.owner_admin_id` در برابر کلید.

4. **Lifecycle:**
   - Remote bot: کلید تازه‌اش (`routers/remote_bot.py:90`) همچنان `owner_admin_id=NULL` می‌گیرد - چون امروز هم global است؛ تغییری در deploy/stop لازم نیست.
   - کلیدهای شخص ثالث آینده: از UI می‌شود هنگام ساخت یک admin مشخص انتخاب کرد (اختیاری) تا از روز اول scoped باشند.
   - Rotation: یک فیلد `rotated_from_id` یا صرفاً ساخت کلید جدید + غیرفعال‌کردن قدیمی (همان الگویی که `remote_bot.py` از قبل برای خودش دارد) کافی است، بدون نیاز به مکانیزم جدید.
   - Plaintext: فقط لحظه‌ی create نمایش داده شود؛ `ApiKeyOut` بعد از آن `key_prefix`/`last4` برگرداند، نه `key` کامل.

## ریسک deploy این تحلیل (مرحله ۳، همین commit)

**صفر.** هیچ کد production تغییر نکرد - فقط یک فایل تست (`test_bot_api_key_cross_tenant.py`) و همین سند اضافه شد. `shared bot` و `remote bot` برای حتی یک ثانیه هم قطع نشدند چون هیچ فایل اجراشونده‌ای لمس نشده.

## باز برای مرحله بعد

- تایید طراحی بالا (به‌خصوص فاز compatibility/enforcement و lifecycle) قبل از نوشتن migration واقعی.
- تصمیم صریح درباره‌ی `/nodes` (global بماند یا نه).
- تصمیم درباره‌ی این‌که آیا panel-level UI برای «ساخت کلید مخصوص یک ادمین» در همین فاز لازم است یا بعداً.
