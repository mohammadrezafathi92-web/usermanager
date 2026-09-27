# تحلیل: عدم وجود owner/scope روی X-API-Key (`/api/bot/*`)

**مرحله:** ۳ - تحلیل ✅ / reproduction test ✅ / طراحی BotPrincipal ✅ (بازبینی دوم، ۱۰ نقص رفع شد) ✅ / **Phase A (schema+backfill) ✅ پیاده‌سازی شد، یک باگ P2 (بازبینی سوم) رفع شد** - همچنان بدون enforcement، بدون اتصال به `deps`/`panel_bridge`/routerها، بدون push.
**وضعیت:** تحلیل، جدول endpointها، طراحی نسخه‌ی دوم، Phase A، و رفع باگ backfill همه تایید/پیاده‌سازی شده‌اند. منتظر گزارش/تایید بعدی برای Phase B.
**تست‌ها (همه سبز روی HEAD فعلی، هیچ‌کدام push نشده):**
- `backend/tests/test_bot_api_key_cross_tenant.py` (۱۲ assertion) - اثبات cross-tenant با یک کلید معمولی.
- `backend/tests/test_bot_auth_principal.py` (۶۲ assertion) - تست واحد ماژول طراحی `services/bot_auth.py` (هنوز به هیچ endpointای وصل نشده).
- `backend/tests/test_bot_inprocess_trust_characterization.py` (۸ assertion) - رفتار امروزِ کالر in-process (`panel_bridge.py`) را مستقل از HTTP مستند می‌کند.
- `backend/tests/test_api_key_migration.py` (۳۴ assertion) - Phase A را روی دیتابیس قدیمی/تازه/اجرای دوباره‌ی startup + دو رگرسیون «ستون غیرضروری گم است»/«ستون ضروری گم است» تست می‌کند + با `inspect.getsource` تایید می‌کند `deps`/`routers/bot.py`/`panel_bridge.py` هنوز `bot_auth` را import نمی‌کنند.
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

## طراحی نسخه‌ی اول (رد شد - فقط برای سابقه)

نسخه‌ی اول این سند فقط یک `owner_admin_id` nullable روی `ApiKey` پیشنهاد داده بود. Product این را با ۳ نقص معماری رد کرد:

1. `owner_admin_id=NULL` هم‌زمان چند معنی دارد (legacy، global، remote bot، بدون‌مالک، integration سوپرادمین) - همان ابهامی که خودِ باگ از آن به وجود آمده بود.
2. فقط owner scope کافی نیست - یک کلید scoped هنوز داخل تنانت خودش به همه‌چیز (کیف پول، پرداخت، broadcast) دسترسی داشت.
3. enforcement اگر پخش بین endpointها باشد، دوباره یک endpoint (مثل `add_balance`) فراموش می‌شود.

نسخه‌ی دوم پایین همین بخش هر سه را با `key_type` صریح، `capabilities`، و یک `BotPrincipal` مرکزی حل می‌کند.

## طراحی نسخه‌ی دوم (بازبینی دوم Product - ۱۰ نقص برطرف شد، کد نوشته و تست شده)

**اصل کلیدی که Product تاکید کرده باید حفظ شود: نه shared bot نه remote bot حتی چند ثانیه قطع نشوند.** این نسخه با همین اصل طراحی شده - Phase A زیر رفتار هیچ کلید موجودی را عوض نمی‌کند.

کد طراحی در `backend/app/services/bot_auth.py` نوشته و با `backend/tests/test_bot_auth_principal.py` (۶۲ assertion) تست شده - **ولی از هیچ‌جای production صدا زده نمی‌شود** (تایید شد با `grep` که هیچ فایل دیگری آن را import نمی‌کند). یعنی خودِ طراحی همین الان قابل بررسی/اجراست، بدون این‌که کوچک‌ترین اثری روی رفتار زنده داشته باشد.

**بازبینی دوم Product ۱۰ نقص در این طراحی پیدا کرد؛ هر ۱۰ مورد در کد و تست‌ها اصلاح شد:**

| # | نقص | اصلاح |
|---|---|---|
| ۱ | `is_scoped` فقط بر اساس وجود `owner_admin_id` تصمیم می‌گرفت - کلید با `scope_enforced=false` ممکن بود ناخواسته محدود شود | فیلد `scope_enforced` به `BotPrincipal` اضافه شد؛ `is_scoped` حالا `valid AND owner_admin_id IS NOT NULL AND scope_enforced` است |
| ۲ | فقط owner scope کافی نبود - کلید scoped داخل تنانتش به همه‌چیز دسترسی داشت | ۸ capability + `DEFAULT_CAPABILITIES_BY_KEY_TYPE` (قبلاً هم بود، دوباره تایید و تکمیل شد) |
| ۳ | `frozenset(capabilities)` روی یک رشته‌ی JSON، آن را کاراکتر‌به‌کاراکتر می‌خواند | `parse_capabilities`/`serialize_capabilities` نوشته شد - JSON واقعی، حذف تکرار، ترتیب پایدار، fail-closed روی هر ورودی خراب |
| ۴ | `key_type` ناشناخته fail-open بود (پیش‌فرض = همه‌ی capabilityها) | فقط `NULL` صریحاً به `legacy_global` تبدیل می‌شود؛ هر مقدار دیگر خارج از `KeyType.KNOWN` یک principal با `valid=False` (fail-closed کامل) می‌سازد |
| ۵ | ترکیب نامعتبر key_type/owner می‌توانست به principal بدون‌محدودیت تبدیل شود | `KeyType.REQUIRES_OWNER`/`FORBIDS_OWNER` + بررسی صریح در `from_api_key` - نقض هرکدام یعنی `valid=False` |
| ۶ | پیام ۴۰۳ می‌توانست label کلید یا owner مجاز را افشا کند | یک پیام عمومی ثابت (`_DENIED_MESSAGE`) برای هر ۴۰۳؛ جزئیات فقط به لاگ می‌رود |
| ۷ | فرمت ذخیره‌سازی capabilities مشخص نبود | JSON canonical (مرتب‌شده، بدون تکرار) + تست NULL/رشته‌ی خراب/لیست خالی |
| ۸ | نرمال‌سازی/رد whitespace در کلید، و تست duplicate hash، تعریف نشده بود | `hash_api_key`/`verify_api_key` هر ورودی دارای whitespace را صریحاً رد می‌کنند (نه strip خاموش)؛ یک تست unique-constraint روی جدول scratch نشان می‌دهد collision رد می‌شود |
| ۹ | telemetry تعریف نشده بود | `_log_decision` در هر ۴ تابع مرکزی صدا زده می‌شود - endpoint، key id/type، claimed owner، نتیجه؛ هرگز کلید خام یا (در پاسخ HTTP) label |
| ۱۰ | `link_telegram` هم‌سطح `customer_write` بود | capability جدای `identity_write` اضافه شد - `tenant_integration` پیش‌فرض آن را ندارد، `remote_shared_bot` دارد (چون ربات تعاملی خودش «وصل کردن حساب قبلی» را صدا می‌زند) |

نکته‌ی مهم: یافته‌ی bypass در `panel_bridge._scope()` (مستند در `test_bot_inprocess_trust_characterization.py`) **عمداً دست‌نخورده ماند** - اصلاح آن طبق دستور صریح Product به Phase C موکول شده، همراه با تست معکوس‌شده و rollout کنترل‌شده، نه در همین مرحله‌ی طراحی.

### ۱. `key_type` صریح - رفع ابهام NULL

```text
KeyType:
  LEGACY_GLOBAL      # هر کلید موجود امروز - بدون تغییر: unscoped، همه‌ی capabilityها
  REMOTE_SHARED_BOT  # کلید routers/remote_bot.py:90 - تایید شد فقط برای ربات مشترک/global است
  TENANT_INTEGRATION # کلید جدید، owner_admin_id اجباری در ساخت
  GLOBAL_INTEGRATION # کلید جدید بدون تنانت - نیازمند مسیر ساخت جدا + تایید رمز سوپرادمین (Phase B)
```

هر ۴ نوع کد شده در `bot_auth.KeyType`. `BotPrincipal.from_api_key()` امروز (چون ستون‌های واقعی هنوز روی DB نیستند) هر کلید موجود را دقیقاً `LEGACY_GLOBAL` با `owner_admin_id=None` و همه‌ی capabilityها می‌بیند - یعنی «صادقانه» همان چیزی که امروز واقعاً هست، نه یک حدس.

### ۲. Capability scopes - جدا از owner

```text
customer_read, customer_write, wallet_write, payment_read,
payment_write, files_read, broadcast, admin_lookup
```

هر ۸ مورد در `bot_auth.py` تعریف شده‌اند. `DEFAULT_CAPABILITIES_BY_KEY_TYPE` دقیقاً طبق دستور:
- `TENANT_INTEGRATION` پیش‌فرض فقط `customer_read`+`customer_write` می‌گیرد - **نه** `wallet_write`، **نه** `broadcast`، **نه** `payment_write` (با تست تایید شده).
- `REMOTE_SHARED_BOT` دقیقاً همان capabilityهایی را می‌گیرد که ربات تعاملی واقعاً صدا می‌زند (بر اساس متدهای `remote_bridge.py`).
- `LEGACY_GLOBAL`/`GLOBAL_INTEGRATION` همه‌چیز - چون امروز هم همین‌طورند.

`require_bot_capability(principal, capability)` تنها چیزی است که یک endpoint باید صدا بزند - برای `add_balance` یعنی `require_bot_capability(principal, WALLET_WRITE)`، بدون نیاز به هیچ پارامتر owner_admin_id در امضای خودِ endpoint. این دقیقاً رفع مشکل «add_balance/link_telegram اصلاً جایی برای owner_admin_id ندارند» است - چون capability check اصلاً به آن پارامتر نیاز ندارد.

### ۳. `BotPrincipal` مرکزی - enforcement در یک نقطه

```python
@dataclass(frozen=True)
class BotPrincipal:
    key_id: Optional[int]
    key_type: str
    owner_admin_id: Optional[int]
    capabilities: frozenset[str]
    label: str
    is_internal: bool = False
```

چهار تابع مرکزی، هرکدام تست‌شده روی سناریوهای واقعی hierarchy (نه فرضی):

- `resolve_claimed_owner(principal, claimed_owner_id)` - جایگزین همان چیزی که هر endpoint امروز خودش با `owner_admin_id: Optional[int] = None` انجام می‌دهد؛ برای principal بدون‌اسکوپ رفتار امروز را عینا حفظ می‌کند، برای principal اسکوپ‌دار روی claim نامعتبر 403 می‌دهد (نه override خاموش).
- `require_bot_capability(principal, capability)`.
- `require_bot_user_access(db, principal, user)` - از همان `hierarchy.can_see_user` که پنل وب استفاده می‌کند، پس یک بات اسکوپ‌شده هرگز بیشتر از session وب همان تنانت نمی‌بیند.
- `require_bot_node_access(db, principal, node)` - از همان `hierarchy.accessible_node_ids` که `routers/nodes.py:99` استفاده می‌کند (پایین‌تر بیشتر توضیح داده شده).

هر ۴ تابع با سناریوهای واقعی (سوپرادمین/ادمین/فروشنده، کاربر بی‌مالک، نود owned/granted/بی‌ربط) در `test_bot_auth_principal.py` تست شده‌اند.

### ۴. کالر in-process - همان authorization service، نه یک نسخه‌ی جدا

طبق پیشنهاد خودتان (گزینه‌ی ۱): `panel_bridge.py` باید یک `BotPrincipal.internal(config.bot_owner_admin_id)` بسازد و همان چهار تابع بالا را صدا بزند - نه یک مسیر enforcement جدا برای HTTP و یک مسیر دیگر (یا هیچ) برای in-process.

`BotPrincipal.internal()` در `bot_auth.py` همین الان کد شده: ربات مشترک (`owner_admin_id=None`) بدون‌اسکوپ می‌ماند (دقیقاً رفتار امروز)، ربات اختصاصی یک ادمین (`owner_admin_id=X`) اسکوپ‌دار می‌شود و از همان `resolve_claimed_owner`/`require_bot_user_access` رد می‌شود که HTTP هم رد می‌شود.

**یافته‌ی مهمی که با تست جدید `test_bot_inprocess_trust_characterization.py` (۸ assertion) تایید شد:** مسیر in-process امروز حتی از HTTP هم ضعیف‌تر است - `panel_bridge.py`'s `_scope()` فقط یک **پیش‌فرض** است، نه یک **سقف**؛ اگر کد handler صریح یک `owner_admin_id` دیگر بفرستد، `_scope()` هیچ مانعی ایجاد نمی‌کند (تست ثابت کرد ربات اختصاصی admin_a می‌تواند با فرستادن صریح `owner_admin_id=admin_b.id` مستقیم زیر admin_b کاربر بسازد). این دقیقاً همان چیزی است که `BotPrincipal`/`resolve_claimed_owner` حل می‌کند - چون دیگر فرقی نمی‌کند این claim از کجا آمده، تابع مرکزی رد می‌کند.

### تصمیم درباره‌ی `/nodes` - هنوز حل‌نشده، عمداً

طبق دستور، `/nodes` دیگر «global by design» اعلام نشده. تایید شد:
- `hierarchy.accessible_node_ids` (`services/hierarchy.py:274`) دقیقاً منطق را دارد: سوپرادمین=`None` (بی‌محدودیت)، ادمین=اتحاد نودهای خودش+grant‌شده، فروشنده=`set()` (هیچ دسترسی مستقیم).
- `routers/nodes.py:99` (`list_nodes` پنل وب) دقیقاً همین را اعمال می‌کند.
- `routers/bot.py:321` (`GET /api/bot/nodes`) هیچ‌کدام را اعمال نمی‌کند - همه‌ی نودهای enabled را برمی‌گرداند.

`require_bot_node_access` در `bot_auth.py` نوشته و تست شده (سناریوی owned/granted/بی‌ربط/فروشنده همه پوشش داده شد) **ولی عمداً به هیچ‌جا وصل نیست**. علتش دقیقاً همان نکته‌ای که خودتان اشاره کردید: مسیر خرید پکیج ساده‌ی shared bot امروز به دیدن همه‌ی نودها متکی است تا مشتری یکی را دستی انتخاب کند (`telegram_bot/handlers/customer.py`'s `pick_node`). این باید طراحی جدای خودش را بگیرد (این pick_node باید از مالک پکیج/دسترسی‌های همان تنانت نود استخراج کند، نه از کل پنل) - **در این مرحله فقط helper آماده شده، endpoint هنوز دست نخورده.**

## Migration پیشنهادی (۴ فاز، طبق دستور)

### Phase A - زیرساخت، بدون تغییر رفتار (طراحی/کد آماده، هنوز apply نشده روی models.py)

ستون‌های additive روی `ApiKey`:

```text
owner_admin_id     nullable
key_type           str, default 'legacy_global'
capabilities       متن JSON یا CSV از لیست بالا (nullable = "همه‌چیز"، برای legacy)
key_hash           نتیجه‌ی sha256، nullable تا لحظه‌ی backfill
key_prefix / key_last4   برای نمایش در UI
created_by_admin_id      nullable، فقط لاگ
scope_enforced     bool, default false
```

- کلیدهای موجود: `key_type='legacy_global'`، `owner_admin_id=NULL`، `scope_enforced=false`.
- ستون قدیمی `key` (plaintext) **حذف/nullable نمی‌شود** - migration این پروژه فقط additive است (`main.py`'s `_auto_migrate_missing_columns`)؛ `key_hash` کنارش اضافه می‌شود، نه جایگزینش.
- `BotPrincipal`/چهار تابع مرکزی: **همین الان آماده و تست‌شده در `bot_auth.py`** - چیزی که باقی می‌ماند صرفاً وصل‌کردن `BotPrincipal.from_api_key`/`.internal()` به `deps.get_bot_api_key`/`panel_bridge.py` است، نه نوشتن از صفر.
- Telemetry: لاگ (نه رد) هر بار caller-supplied owner_admin_id با principal.owner_admin_id فرق دارد وقتی `scope_enforced=false` - قبل از فاز C ببینیم این الگو در ترافیک واقعی اصلاً رخ می‌دهد یا نه.

### Phase B - کلیدهای جدید

- ساخت کلید جدید در UI پیش‌فرض `tenant_integration` است؛ owner و capability موقع ساخت مشخص می‌شود.
- `global_integration` مسیر جدا، هشدار صریح، `require_confirm_password` (همان الگوی موجود `routers/api_keys.py`'s `delete_key`).
- کلید remote bot به‌صورت خودکار `key_type='remote_shared_bot'` می‌گیرد (تغییر کوچک در `routers/remote_bot.py:90`، بدون تغییر رفتار چون همچنان unscoped با همه‌ی capabilityهای لازم است).

### Phase C - Enforcement

- فقط برای کلید با `scope_enforced=true` (کلیدهای جدید Phase B + هر کلید legacy که عمداً ارتقا داده شود) `resolve_claimed_owner`/`require_bot_capability`/`require_bot_user_access`/`require_bot_node_access` واقعاً صدا زده می‌شوند.
- کلیدهای legacy با `scope_enforced=false` دقیقاً مثل امروز کار می‌کنند - **بدون قطعی.**
- `panel_bridge.py` هم‌زمان به `BotPrincipal.internal()` وصل می‌شود - نه بعداً - تا دو مسیر enforcement جدا (HTTP قوی، in-process ضعیف) دوباره ایجاد نشود.
- `add_balance`، `link_telegram`، payment cards، downloadها، همه از همین مسیر مرکزی رد می‌شوند - نه یک‌به‌یک.

### Phase D - پایان دوره‌ی legacy

- کلیدهای legacy تشویق/الزام به rotation.
- Plaintext از پاسخ API حذف (`ApiKeyOut` فقط `key_prefix`/`key_last4`، `key` کامل فقط لحظه‌ی create).
- ستون `key` قدیمی در DB با یک مقدار غیرقابل‌استفاده جایگزین شود (نه حذف ستون - additive-only).
- حالت «legacy unrestricted» در نهایت خاموش شود (تصمیم جدا، بعد از این‌که همه واقعاً rotate کردند).

## Hash کلید

طبق دستور: SHA-256 deterministic، نه bcrypt (کلید API خودش پرآنتروپی است، نه یک رمز کوتاه انسانی - هزینه‌ی CPU بالای bcrypt هیچ محافظتی اضافه نمی‌کند و فقط هر درخواست را کند می‌کند). کد شده در `bot_auth.hash_api_key`/`verify_api_key` (`hashlib.sha256` + `hmac.compare_digest`)، تست شده در `test_bot_auth_principal.py`.

## ریسک deploy این مرحله (طراحی، همین commit جدید)

**صفر.** `backend/app/services/bot_auth.py` وجود دارد ولی هیچ فایل دیگری آن را import نمی‌کند (تایید شد با `grep`) - یعنی حتی import کردن این ماژول هم اثری روی هیچ request زنده‌ای ندارد. هیچ ستونی به `models.py` اضافه نشده (پس auto-migration پروژه چیزی روی DB واقعی اجرا نمی‌کند). `shared bot`/`remote bot` لمس نشدند.

## باز برای مرحله بعد

- تایید نهایی طراحی نسخه‌ی دوم (این بخش) - اگر تایید شد، مرحله‌ی بعد Phase A واقعی است: **فقط** افزودن ستون‌ها به `models.py` (additive - auto-migration خودش را اعمال می‌کند) + backfill امن/idempotent (`main.py`'s `_backfill_api_key_hashes`).
  > **اصلاح ۲۰۲۶-۰۹-۲۷ (بازبینی سوم Product):** نسخه‌ی قبلی همین خط اشتباهاً می‌گفت وصل‌کردن `BotPrincipal.from_api_key`/`.internal()` به `deps.get_bot_api_key`/`panel_bridge.py` هم بخشی از «Phase A واقعی» است. **این اشتباه بود.** طبق تایید صریح Product، Phase A **کاملاً schema/backfill-only** است - هیچ اتصالی به `deps.get_bot_api_key`، هیچ routerای، یا `panel_bridge.py` در این فاز برقرار نمی‌شود؛ وصل‌کردن `BotPrincipal` به این‌ها (و روشن‌کردن هرگونه enforcement) تماماً به **Phase C** موکول شده است.
- طراحی جدای `/nodes` + مسیر `pick_node` برای shared bot (چه چیزی باید ببیند وقتی هیچ owner مشخصی ندارد).
- تصمیم درباره‌ی UI ساخت کلید (Phase B) - در همین دور یا جدا.

## دو Guardrail ثبت‌شده برای مراحل بعد (بازبینی سوم Product)

1. **کلیدهای `tenant_integration` در Phase B تا زمان enforcement نباید فعال/قابل‌استفاده باشند** - چون تا وقتی `scope_enforced=false` است، عملاً دقیقاً مثل یک کلید global رفتار می‌کنند (طبق طراحی عمدی همین بخش)؛ یعنی ساختن یک کلید scoped-به‌ظاهر در Phase B نباید به کسی این تصور غلط را بدهد که همان لحظه محدود شده است.
2. **در Phase C، اعتبارسنجی principal باید یک dependency مرکزی و اجباری برای تمام endpointهای `/api/bot` باشد، حتی endpointهای global** (مثل `/nodes`، `/customer-menu-config`) - نه یک بررسی اختیاری که فقط endpointهای «مهم» آن را صدا می‌زنند. این دقیقاً همان درسی است که خودِ این آسیب‌پذیری از آن به وجود آمد (`add_balance`/`link_telegram` فراموش شدند چون بررسی پخش بود، نه مرکزی).

## Phase A - وضعیت پیاده‌سازی

**پیاده‌سازی شد** (schema + backfill، هیچ enforcement/اتصالی):

- `backend/app/models.py`: ۸ ستون additive روی `ApiKey` (`owner_admin_id`, `key_type` default `legacy_global`, `capabilities`, `scope_enforced` default `False`, `key_hash` unique/indexed, `key_prefix`, `key_last4`, `created_by_admin_id`).
- `backend/app/main.py`: `_backfill_api_key_hashes()` - idempotent، فیلتر روی `key_hash IS NULL`، هر ردیف whitespace‌دار (که نباید هیچ‌وقت رخ بدهد) را رد می‌کند نه کرش - در `on_startup` بلافاصله بعد از `_auto_migrate_missing_columns()` صدا زده می‌شود.
- `backend/tests/test_api_key_migration.py` (۳۴ assertion) - دقیقاً همان تکنیک `test_package_groups.py` (جایگزینی موقت `app_main.engine`/`SessionLocal` با یک engine ساختگی) برای سه سناریو:
  1. **دیتابیس قدیمی** - جدول `api_keys` دقیقاً به شکل قبل از این migration، با ۲ ردیف واقعی؛ بعد از migration همه‌ی ستون‌های جدید اضافه شدند و هر ردیف `key_type=legacy_global`, `owner_admin_id=NULL`, `scope_enforced=False`, و `key_hash`/`key_prefix`/`key_last4` درست از روی همان `key` قدیمی محاسبه شد.
  2. **دیتابیس تازه** - یک کلید تازه‌ساخته با ORM، بدون تنظیم دستی هیچ ستون جدیدی، دقیقاً همان مقادیر پیش‌فرض را می‌گیرد.
  3. **اجرای دوباره‌ی startup** - migration+backfill دوبار پشت‌سرهم روی هم آن دیتابیس (چه تازه چه قدیمی) اجرا شد؛ هیچ ردیفی تغییر نکرد، هیچ خطایی رخ نداد.
  - یک بخش آخر هم با `inspect.getsource` تایید می‌کند `deps.get_bot_api_key`، `routers/bot.py`، و `telegram_bot/panel_bridge.py` هیچ‌کدام `bot_auth` را import نمی‌کنند - یعنی مرز Phase A را خودِ تست نگه‌بانی می‌کند، نه فقط توضیح در سند.

### باگ P2 پیدا‌شده و رفع‌شده در بازبینی سوم Product: crash روی migration ناقص

**مشکل:** `_backfill_api_key_hashes()`ی اولیه با `db.query(models.ApiKey)...` کل ردیف ORM را می‌خواند - یعنی SELECT روی هر ۸ ستون جدید، نه فقط ۴ ستونی (`id, key, key_hash, key_prefix, key_last4`) که این تابع واقعاً لازم دارد. `_auto_migrate_missing_columns()` عمداً خطای افزودن هر ستون را لاگ و skip می‌کند تا startup را قطع نکند - ولی اگر همان یکی از ۸ ستون (مثلاً `created_by_admin_id`، هرچند دلیلش مهم نیست) واقعاً اضافه نشده باشد، همین یک SELECT کامل با `OperationalError: no such column` کل startup را کرش می‌کرد؛ دقیقاً برخلاف فلسفه‌ی خودِ `_auto_migrate_missing_columns` («یک ستون گم‌شده ارزش کرش‌کردن ندارد»).

**رفع:** تابع حالا اول با `inspect(engine).get_columns(...)` بررسی می‌کند ۵ ستون موردنیازش واقعاً روی جدول هست یا نه؛ اگر نبود فقط warning می‌دهد و بی‌سروصدا برمی‌گردد (startup ادامه پیدا می‌کند، دفعه‌ی بعد دوباره امتحان می‌شود). خواندن/نوشتن هم دیگر از `models.ApiKey` ORM نیست - از `models.ApiKey.__table__` (Core) با `select()`/`update()` صریح روی همان ۵ ستون، یعنی این تابع دیگر هیچ‌وقت به وجود ستون‌های دیگر (`owner_admin_id`, `key_type`, `capabilities`, `scope_enforced`, `created_by_admin_id`) وابسته نیست.

**دو رگرسیون اضافه شد:**
- یک ستون **غیرضروری** (`created_by_admin_id`) گم است → backfill همچنان موفق می‌شود و کلید را hash می‌کند.
- یک ستون **ضروری** (`key_hash`/`key_prefix`/`key_last4`) گم است → backfill بدون کرش فقط warning می‌دهد و رد می‌شود؛ ردیف دست‌نخورده می‌ماند.

**نتیجه:** ۸۳ تست بک‌اند سبز (شامل ۳۴ assertion در `test_api_key_migration.py`)، compileall سبز، `npm run build` سبز، `git diff --check` سبز. یک `SAWarning` درباره‌ی چرخه‌ی FK بین `admin_users`/`payment_cards` در طول تست دیده می‌شود - با `git stash` تایید شد این هشدار از قبلِ این تغییر هم وجود داشته (نامرتبط با ستون‌های جدید).

**ریسک deploy:** پایین. تنها تغییر واقعی روی دیتابیس زنده، افزودن ۸ ستون null‌پذیر/پیش‌فرض‌دار به یک جدول کوچک (`api_keys`) به‌علاوه یک پرس‌وجوی سبک و اکنون کاملاً مقاوم‌به‌migration-ناقص روی همان جدول در startup است - هیچ جدول دیگری، هیچ فایل اجراشونده‌ی دیگری، و هیچ رفتار زنده‌ای لمس نشد. Push نشده - منتظر گزارش/تایید بعدی طبق دستور.
