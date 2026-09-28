# تحلیل: عدم وجود owner/scope روی X-API-Key (`/api/bot/*`)

**مرحله:** ۳ - تحلیل ✅ / reproduction test ✅ / طراحی BotPrincipal ✅ / Phase A (schema+backfill) ✅ / Phase B (ساخت و lifecycle کلیدهای typed) ✅ / **Phase C نسخه‌ی اول تا هفتم ❌ (رد شدند در بازبینی مستقل کد) / نسخه‌ی هشتم (canonical، مستقل و کامل) ✅ طراحی شد - هنوز صفر کد/enforcement/commit/push**.
**وضعیت:** طراحی Phase C نسخه‌ی هشتم (تنها مرجع اجرا - انتهای سند، بدون ارجاع به نسخه‌های قبل) آماده‌ی بازبینی مستقل بعدی Product است؛ تا تایید، هیچ اجرای C0 یا مرحله‌ی بعدی شروع نمی‌شود.
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

**وضعیت پیاده‌سازی Phase B (۲۰۲۶-۰۹-۲۸):**

- `POST /api/api-keys` فقط `tenant_integration` می‌سازد؛ owner/capability را ذخیره می‌کند، ولی کلید را با `enabled=false` و `scope_enforced=false` می‌سازد. endpoint toggle نیز فعال‌کردن آن را با 409 رد می‌کند (اگر ردیفی بیرون از برنامه فعال شده باشد، همان toggle همچنان می‌تواند آن را خاموش کند).
- `POST /api/api-keys/global` مسیر مجزاست؛ فقط بعد از `require_confirm_password` یک `global_integration` فعال و بدون owner می‌سازد. UI پیش از ساخت هشدار صریح دسترسی سراسری نشان می‌دهد.
- deploy ربات روی سرور دوم کلید را با `remote_shared_bot`، بدون owner، `scope_enforced=false` و همان رفتار فعال قبلی می‌سازد؛ secret/hash/prefix/last4 و سازنده همان لحظه ثبت می‌شوند.
- UI نوع، owner و capabilityهای tenant را می‌گیرد؛ دو capability کم‌خطر `customer_read`/`customer_write` پیش‌فرض‌اند و بقیه فقط با انتخاب صریح اضافه می‌شوند. کلید tenant در فهرست «در انتظار Phase C» است و دکمه‌ی فعال‌سازی ندارد.
- plaintext و پاسخ API عمداً دست‌نخورده‌اند (Phase D). هیچ تغییری در auth زنده‌ی `/api/bot`، ربات in-process یا bypass شناخته‌شده‌ی `panel_bridge._scope()` ایجاد نشده است.
- `backend/tests/test_api_key_phase_b.py` با تست HTTP واقعی و تست deploy mock‌شده، ساخت tenant/global، تأیید رمز، ممنوعیت فعال‌سازی tenant، سازگاری lifecycle کلید legacy، طبقه‌بندی remote bot، hash metadata و مرز «بدون BotPrincipal wiring» را پوشش می‌دهد.

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

## Phase C - طراحی rollout (تحلیل و طراحی فقط - بدون کد، بدون enforcement، بدون commit/push)

**تاریخ:** ۲۰۲۶-۰۹-۲۸. **وضعیت:** طراحی برای بازبینی مستقل بعدی؛ صفر خط کد تغییر کرده. طبق دستور صریح: "بعد از آماده‌شدن طراحی، قبل از هر enforcement واقعی یک بازبینی مستقل دیگر انجام می‌دهیم."

### ۱. Dependency مرکزی و اجباری - یک نقطه‌ی عبور برای HTTP و in-process

**مشکلی که حل می‌کند:** Guardrail شماره‌ی ۲ ثبت‌شده در بازبینی سوم Product - اگر بررسی principal اختیاری/پخش‌شده باشد، دوباره یک endpoint (مثل `add_balance`/`link_telegram` در گذشته) فراموش می‌شود.

**طرح:** امروز هر endpoint در `routers/bot.py` دقیقاً یک پارامتر یکسان دارد: `api_key: models.ApiKey = Depends(deps.get_bot_api_key)`. این خودش یک نقطه‌ی مرکزی موجود است - نقطه‌ی مناسب برای تبدیل به principal، نه ساختن یک مسیر جدید:

```text
1. deps.get_bot_api_key بدون تغییر باقی می‌ماند (همان lookup/enabled/last_used_at).
2. یک تابع جدید deps.get_bot_principal(api_key=Depends(get_bot_api_key)) -> BotPrincipal
   اضافه می‌شود: فقط bot_auth.BotPrincipal.from_api_key(api_key) را برمی‌گرداند.
3. هر امضای endpoint در routers/bot.py از
     api_key: models.ApiKey = Depends(deps.get_bot_api_key)
   به
     principal: bot_auth.BotPrincipal = Depends(deps.get_bot_principal)
   تغییر می‌کند - یک sed مکانیکی روی کل فایل، نه ۳۰ تغییر دستی جدا.
4. deps.get_bot_api_key به یک نام "خصوصی" تغییر می‌کند (مثلاً پیشوند _ یا انتقال به
   ماژول bot_auth خودش) تا هیچ endpoint جدیدی نتواند مستقیم به ApiKey برسد و
   ناخواسته از principal رد شود - بستن راه فرار، نه فقط توصیه در کد ریویو.
```

این یعنی **همان تغییر واحد** به‌صورت مکانیکی هر endpoint موجود و هر endpoint آینده‌ای که به همین الگو ساخته شود را می‌پوشاند - چیزی که در بازبینی سوم به‌عنوان درس اصلی این آسیب‌پذیری ثبت شد.

**نکته‌ی حیاتی برای عدم قطعی:** `get_bot_principal` هیچ 403/404ای خودش صادر نمی‌کند - فقط principal را می‌سازد (که برای هر کلید legacy امروز دقیقاً `LEGACY_GLOBAL, scope_enforced=False` است، یعنی `is_scoped=False`). enforcement واقعی (`require_bot_capability`/`resolve_claimed_owner`/...) همچنان **داخل بدنه‌ی هر endpoint** صدا زده می‌شود، نه در خودِ dependency - چون تصمیم capability/owner/node به پارامترهای همان endpoint (مثل `claimed_owner_id`، `node_id`) نیاز دارد که در سطح dependency در دسترس نیست. یعنی dependency مرکزی «اجباری بودنِ داشتنِ principal» را تضمین می‌کند، نه خودش تصمیم authorize/deny - آن تصمیم در بخش ۲ زیر جدول‌بندی شده و هنوز per-endpoint فراخوانی می‌شود (اما دیگر نمی‌شود «یادش رفت» چون principal همیشه حاضر است).

**کالر in-process (`panel_bridge.py`):** طبق تحلیل قبلی این سند (بخش «کالر in-process»)، راه‌حل یک authorization service مشترک است، نه یک نسخه‌ی موازی:
- خودِ توابع `routers/bot.py` (بدنه‌ی هر endpoint) باید از گرفتن `principal` از FastAPI `Depends` مستقل شوند - یعنی منطق واقعی هر endpoint (اعتبارسنجی، فراخوانی `resolve_claimed_owner`/`require_bot_capability`/...، دسترسی به DB) در یک تابع داخلی (مثلاً با پیشوند `_do_`) قرار می‌گیرد که پارامتر اولش `principal: BotPrincipal` است؛ endpoint خودِ FastAPI فقط یک wrapper نازک می‌شود که `Depends(get_bot_principal)` را می‌گیرد و به همان تابع داخلی پاس می‌دهد.
- `panel_bridge.py` به‌جای صدا زدن مستقیم بدنه‌ی endpoint با یک `owner_admin_id` دستی (بایپس امروزی که در `test_bot_inprocess_trust_characterization.py` مستند شد)، یک `BotPrincipal.internal(config.bot_owner_admin_id)` می‌سازد (یک‌بار در ابتدای هر تابعِ bridge، یا cache شده روی `RuntimeConfig` همان thread) و همان تابع داخلی `_do_*` را با این principal صدا می‌زند - دقیقاً همان مسیر تصمیم‌گیری HTTP، نه یک کپی.
- نتیجه: `_scope()` امروزی (که «پیش‌فرض، نه سقف» است) از مسیر تصمیم‌گیری کنار گذاشته می‌شود؛ هر `owner_admin_id` صریحی که handler یک ربات اختصاصی به bridge بدهد باید از همان `resolve_claimed_owner` رد شود که HTTP هم رد می‌شود - یعنی همان bypass مستندشده در تست characterization دیگر ممکن نیست.

**کالر remote bot:** چیز جدیدی لازم ندارد - `remote_bridge.py` صرفاً HTTP واقعی با `X-API-Key` به همان `/api/bot/*` می‌زند، پس به‌صورت خودکار از همان مسیر HTTP بالا رد می‌شود؛ چون کلید آن `REMOTE_SHARED_BOT`/`scope_enforced=False` می‌ماند (بخش ۶ پایین)، `is_scoped=False` و رفتار امروز عینا حفظ می‌شود.

### ۲. جدول کامل endpoint → capability → روش بررسی owner/user/node

ستون «روش بررسی» دقیقاً کدام یک از ۴ تابع مرکزی `bot_auth.py` صدا زده می‌شود را نام می‌برد. «تغییر امضا لازم؟» یعنی آیا پارامتر جدیدی به خودِ endpoint اضافه می‌شود یا نه (طبق یافته‌های بخش «بررسی ویژه‌ی موارد خواسته‌شده» بالا).

| Endpoint | Capability | روش بررسی owner/user/node | تغییر امضا لازم؟ |
|---|---|---|---|
| `GET /nodes` | *(ندارد - زیرساخت مشترک)* | فیلتر لیست با `hierarchy.accessible_node_ids` معادلِ principal (helper جدید، بخش ۳) | نه |
| `GET /packages` | `CUSTOMER_READ` | `resolve_claimed_owner(principal, owner_admin_id)` سپس فیلتر امروزی با نتیجه‌اش | نه |
| `GET /payment-info` | `CUSTOMER_READ` | `resolve_claimed_owner` | نه |
| `GET /payment-cards/{card_id}` | `PAYMENT_READ` | جدید: بارگذاری کارت، سپس بررسی `card.owner_admin_id` با `resolve_claimed_owner`/معادل (کارت امروز اصلاً owner claim ندارد - باید از روی خودِ رکورد کارت بررسی شود، نه پارامتر ورودی) | نه (owner از رکورد گرفته می‌شود) |
| `POST /payment-cards/{card_id}/record-payment` | `PAYMENT_WRITE` | مثل بالا | نه |
| `GET /sales-stats` | `CUSTOMER_READ` | `resolve_claimed_owner` | نه |
| `GET /customer-menu-config` | *(ندارد - global by design)* | هیچ - فقط principal معتبر لازم است (`_ensure_valid` از طریق خودِ `require_bot_capability`ی با یک capability بی‌اثر، یا صرفاً عبور از dependency مرکزی) | نه |
| `GET /tutorials` | `CUSTOMER_READ` | `resolve_claimed_owner` | نه |
| `GET /packages/{id}/files/{id}/download` | `FILES_READ` | جدید: بررسی تعلق پکیج به `resolve_claimed_owner`ی معتبر (owner از خودِ پکیج خوانده می‌شود) | نه |
| `GET /tutorials/{id}/media/{id}/download` | `FILES_READ` | مثل بالا (owner از تنظیمات global آموزش - این‌ها امروز per-tenant نیستند؛ فعلاً فقط `CUSTOMER_READ`/`FILES_READ` کافی است، بدون owner check اضافه) | نه |
| `GET /tutorials/{id}/software/{id}/download` | `FILES_READ` | مثل بالا | نه |
| `GET /admin-by-telegram/{tg_id}` | `ADMIN_LOOKUP` | ندارد (خروجی خودش کل هویت است؛ صرفاً capability-gated) | نه |
| `GET /admin-username/{admin_id}` | `ADMIN_LOOKUP` | ندارد (افشای کم؛ صرفاً capability-gated) | نه |
| `GET /telegram-user-ids` | `BROADCAST` | `resolve_claimed_owner` | نه |
| `POST /users` (create_user) | `CUSTOMER_WRITE` | `resolve_claimed_owner(principal, payload.owner_admin_id)` - نتیجه به‌جای `payload.owner_admin_id` خام به `user_ops.create_user_record` می‌رود | نه (owner از payload موجود گرفته می‌شود، فقط validate) |
| `POST /users/{u}/purchase-package` | `CUSTOMER_WRITE` | `require_bot_user_access` روی کاربر بارگذاری‌شده | نه |
| `POST /referral/apply` | `CUSTOMER_WRITE` | `require_bot_user_access` روی کاربر یافت‌شده با `username` | نه |
| `POST /discount/validate`, `/discount/redeem` | `CUSTOMER_WRITE` | `resolve_claimed_owner` | نه |
| `GET /users` (list_users) | `CUSTOMER_READ` | `resolve_claimed_owner` | نه |
| `GET /users/by-telegram/{id}` | `CUSTOMER_READ` | `resolve_claimed_owner` | نه |
| `GET /users/by-telegram/{id}/all` | `CUSTOMER_READ` | `resolve_claimed_owner` | نه |
| `GET /users/{username}` | `CUSTOMER_READ` | `require_bot_user_access` (از طریق `_get_user_or_404` که همین حالا `owner_admin_id` می‌گیرد - جای فراخوانی عوض می‌شود، نه منطق) | نه |
| `POST /users/{u}/link-telegram` | `IDENTITY_WRITE` | `require_bot_user_access` روی کاربر - **به amضا یک principal اضافه می‌شود (از dependency)، نه owner_admin_id دستی؛ چون owner از خودِ کاربر بارگذاری‌شده گرفته می‌شود** | نه (owner از رکورد کاربر، نه پارامتر جدید) |
| `POST /users/{u}/connections` | `CUSTOMER_WRITE` | `require_bot_user_access` + اگر `node_id` در بدنه باشد، `require_bot_node_access` هم | نه |
| `GET /users/{u}/purchases` | `CUSTOMER_READ` | `require_bot_user_access` | نه |
| `POST /users/{u}/purchases/{id}/rename` | `CUSTOMER_WRITE` | `require_bot_user_access` | نه |
| `DELETE /users/{u}/purchases/{id}` | `CUSTOMER_WRITE` | `require_bot_user_access` | نه |
| `DELETE /users/{u}/connections/{id}` | `CUSTOMER_WRITE` | `require_bot_user_access` | نه |
| `GET /users/{u}/subscription-link` | `CUSTOMER_READ` | `require_bot_user_access` | نه |
| `GET /miniapp-button-text`, `/panel-public-url` | *(ندارد - global by design)* | هیچ | نه |
| `POST /users/{u}/purchases/{id}/renew` | `CUSTOMER_WRITE` | `require_bot_user_access` | نه |
| `POST /users/{u}/renew` | `CUSTOMER_WRITE` | `require_bot_user_access` | نه |
| `POST /users/{u}/reset-usage` | `CUSTOMER_WRITE` | `require_bot_user_access` | نه |
| `POST /users/{u}/set-enabled` | `CUSTOMER_WRITE` | `require_bot_user_access` | نه |
| `POST /users/{u}/add-balance` | `WALLET_WRITE` | `require_bot_user_access` روی کاربر - **همین جا هم owner از رکورد کاربر گرفته می‌شود، نه پارامتر جدید در امضا** | نه |
| `DELETE /users/{u}` | `CUSTOMER_WRITE` | `require_bot_user_access` | نه |

**نتیجه‌ی کلیدی این جدول:** ستون «تغییر امضا لازم؟» برای همه‌ی ردیف‌ها **نه** است. حتی `add_balance`/`link_telegram` (که در تحلیل اولیه «هیچ پارامتر owner_admin_id در امضا ندارند» به‌عنوان مشکل ثبت شده بودند) نیازی به پارامتر جدید ندارند، چون:
- در سطح FastAPI فقط نوع `Depends` عوض می‌شود (`api_key` → `principal`) - این تغیر امضا نیست، تغییر نوع همان یک پارامتر همیشگی است.
- owner برای این دو مورد از **خودِ رکورد کاربر بارگذاری‌شده** گرفته می‌شود (`user.owner_admin_id`)، نه از یک claim جدید در payload - `require_bot_user_access` دقیقاً برای همین طراحی شده بود.

### ۳. طراحی امن `/nodes` و جریان `pick_node`

**اصل طراحی:** scoping فقط **یک بار**، در `GET /nodes`، اعمال می‌شود؛ `pick_node` (در `telegram_bot/handlers/customer.py`) هیچ تغییری نمی‌خواهد چون فقط مصرف‌کننده‌ی همان لیست است - هر چه `/nodes` برگرداند، `pick_node` از همان انتخاب می‌کند.

```text
تابع جدید در bot_auth.py: accessible_node_ids_for(db, principal) -> Optional[set[int]]
  - principal نامعتبر: [] (فهرست خالی، نه استثنا - چون این helper برای فیلتر
    یک query است، نه یک 403 مستقیم؛ endpoint خودش با require_bot_capability
    مسیر نامعتبر را قبلا رد کرده است).
  - principal.is_scoped == False (هر کلید legacy/remote/global امروز، یا هر
    تنانت‌کلیدی که scope_enforced=false مانده): None (بدون محدودیت) - دقیقا
    رفتار امروز، صفر تغییر برای shared bot/remote bot/کلیدهای فعال.
  - principal.is_scoped == True: دقیقا hierarchy.accessible_node_ids(db, admin)
    همان ادمینِ principal.owner_admin_id - همان چیزی که routers/nodes.py:99
    برای پنل وب همان ادمین برمی‌گرداند.

GET /api/bot/nodes:
  ids = accessible_node_ids_for(db, principal)
  query = ... .filter(Node.enabled == True)
  if ids is not None: query = query.filter(Node.id.in_(ids))
```

**جریان `pick_node` (shared bot، بدون owner):** چون shared bot همیشه `is_scoped=False` می‌ماند (owner ندارد)، `accessible_node_ids_for` برایش `None` برمی‌گرداند - یعنی **دقیقاً همان لیست کامل نودهای امروز**، بدون هیچ تغییر رفتار. تغییر رفتار واقعی فقط برای یک کلید/ربات **اختصاصی** (که مالک مشخص دارد و `scope_enforced=true` شده) رخ می‌دهد: مشتری آن تنانت فقط نودهای همان تنانت را برای انتخاب می‌بیند - همان چیزی که پنل وب همان ادمین هم به او نشان می‌دهد.

**محدودیت شناخته‌شده، عمداً خارج از این طراحی (باید در بازبینی بعدی صریحاً تایید/رد شود):** این طرح رابطه‌ی مستقیم «کدام پکیج فقط با کدام نودها کار می‌کند» را حل نمی‌کند - فقط «این principal اصلاً حق دیدن این نود را دارد یا نه» را. اگر یک پکیج به نودی وابسته باشد که تنانت آن پکیج به آن دسترسی ندارد، این یک باگ داده (data-integrity) در تعریف پکیج است، نه چیزی که authorization layer باید حل کند - پیشنهاد می‌شود این را به یک بررسی جدا (نه بخشی از Phase C) موکول کنیم، همان‌طور که در سند خودِ Phase A هم صریح مستند شده بود.

### ۴. Rollout: observation → فعال‌سازی تدریجی → telemetry → rollback → تست‌های معکوس‌شونده

**اصل بنیادین کل rollout:** `scope_enforced` ستونی است که از Phase A/B وجود دارد و امروز برای **همه** ردیف‌ها `False` است. تمام enforcement بالا شرط‌بندی‌شده روی `principal.is_scoped` است که خودش شرط `scope_enforced=True` دارد - یعنی **صرف merge/deploy شدن کد Phase C، هیچ رفتار زنده‌ای عوض نمی‌شود**، تا وقتی کسی صریحاً یک ردیف را `scope_enforced=True` کند.

مراحل rollout (هرکدام یک commit/PR جدا، طبق پروتکل استاندارد این پروژه - تحلیل/تست قبل، تایید Product، بعد push):

1. **C0 - Wiring بدون فعال‌سازی (observation-only).** `deps.get_bot_principal` + بخش ۱/۲ بالا merge می‌شود. چون هیچ ردیف `scope_enforced=true` ندارد، `is_scoped` برای همه‌ی کلیدهای موجود False می‌ماند - رفتار زنده صفر تغییر. تنها اثر قابل‌مشاهده: هر تصمیم از این نقطه به بعد از `_log_decision` رد می‌شود، یعنی الان می‌توانیم برای اولین‌بار در لاگ ببینیم در ترافیک واقعی چقدر «claimed owner_admin_id caller-supplied با هیچ principal.owner_admin_idای» مقایسه نمی‌شود (چون principal.owner_admin_id برای همه None است) - این خودش تاییدی است، نه یک متریک قابل مقایسه؛ متریک واقعی از C1 به بعد معنا پیدا می‌کند.
2. **C1 - یک کلید تست داخلی.** یک `tenant_integration` کلید تازه (Phase B) که به یک ادمین تست واقعی (نه production) تعلق دارد، دستی `scope_enforced=true` می‌شود. مشاهده‌ی لاگ `_log_decision` برای این یک کلید به مدت مشخص (پیشنهاد: حداقل چند روز واقعی استفاده) - هیچ کلید دیگری لمس نمی‌شود.
3. **C2 - فعال‌سازی تدریجی کلیدهای Phase B واقعی.** ترتیب پیشنهادی برای تبدیل `scope_enforced=false → true` (بخش ۵ زیر) - هر بار یک یا چند کلید، با فاصله، نه یک UPDATE سراسری.
4. **C3 - وصل‌شدن in-process (`panel_bridge.py`).** طبق بخش ۱، جداگانه از C1/C2 چون رفتار **ربات‌های اختصاصی زنده** (نه کلیدهای HTTP جدید) را لمس می‌کند - این باید آخرین مرحله باشد، بعد از این‌که مسیر HTTP در production واقعی (حداقل با کلیدهای تست) رفتار درست را نشان داد.
5. **C4 (اختیاری، تصمیم جدا) - ارتقای انتخابی یک کلید legacy.** فقط با درخواست صریح صاحب همان کلید، هرگز خودکار.

**Telemetry:** هر تصمیم از ۴ تابع مرکزی از قبل لاگ می‌شود (`_log_decision`، از Phase A/طراحی BotPrincipal). برای rollout، پیشنهاد: یک شمارنده‌ی جمع‌شده (نه فقط لاگ خام) روی `allowed=False` به تفکیک `key_id` در بازه‌ی زمانی، تا یک کلید که به‌اشتباه scoped شده (و مشروع دارد رد می‌شود) زود دیده شود - این یک متریک جدید است، جزو Phase C طراحی می‌شود ولی پیاده‌سازی دقیق dashboard/alert آن به بازبینی enforcement واگذار می‌شود، نه بخشی از این سند تحلیلی.

**Rollback:** چون enforcement کاملاً شرط‌بندی‌شده روی `scope_enforced` ستون دیتابیس است (نه یک flag کد/دیپلوی)، rollback یک مرحله یعنی صرفاً `UPDATE api_keys SET scope_enforced=false WHERE id=...` روی همان کلید(های) مسئله‌دار - **بدون نیاز به revert کد یا دیپلوی دوباره**. rollback مرحله‌ی C3 (in-process) پیچیده‌تر است چون به یک دیپلوی کد نیاز دارد (بازگرداندن `panel_bridge.py` به فراخوانی مستقیم بدون principal) - به همین دلیل C3 آخرین مرحله و جداگانه commit می‌شود، تا rollbackاش هم مستقل از C0-C2 باشد.

**تست‌های معکوس‌شونده (reversible/regression):** برای هر مرحله، پیش از فعال‌سازی، یک تست باید هم «قبل» و هم «بعد» را اثبات کند - یعنی:
- تست‌های امروزی (`test_bot_auth_principal.py`, `test_api_key_phase_b.py`, `test_bot_inprocess_trust_characterization.py`) که رفتار «بدون wiring» را مستند می‌کنند باید **بعد از C0** هم سبز بمانند تا زمانی که خودشان صریحاً به نسخه‌ی «با wiring» بازنویسی شوند - یعنی wiring نباید همزمان با pass شدن این فایل‌ها ادغام شود؛ این فایل‌ها باید در همان commit که wiring می‌آید، به نسخه‌ای تغییر کنند که «wiring هست، ولی چون scope_enforced=false همه‌جا، رفتار یکسان است» را اثبات کنند - جای assertion `"BotPrincipal" in inspect.getsource(...) == False` باید با یک assertion جدید عوض شود: «هست، ولی خروجی رفتاری‌اش برای کلید legacy/remote یکسان با قبل است».
- یک تست جدید characterization معکوس برای C3: دقیقاً سناریوی `test_bot_inprocess_trust_characterization.py` (ربات admin_a با owner_admin_id صریح admin_b) باید بعد از C3 به 403/404 تبدیل شود - یعنی همان تست، assertion آن از «موفق می‌شود» (رفتار امروز، عمدا مستند) به «رد می‌شود» تغییر می‌کند؛ تا وقتی C3 پیاده نشده، این تست نباید نوشته شود چون امروز واقعاً باید pass کند (رفتار فعلی است، نه باگ تستی).

### ۵. ترتیب تبدیل کلیدهای Phase B از `scope_enforced=false` به `true`

طبق C1/C2 بالا، و طبق ریسک هر key_type:

1. کلید(های) تست داخلی (C1) - صفر تاثیر روی مشتری واقعی.
2. کلیدهای `tenant_integration` که **هنوز هیچ استفاده‌ی زنده‌ای ندارند** (تازه در Phase B ساخته شده‌اند ولی چون تا امروز اصلا `enabled=false` بودند، هیچ integration واقعی رویشان ساخته نشده) - این‌ها enable+scope_enforced همزمان می‌شوند، چون فعال‌سازی اولشان است، نه تغییر رفتار یک چیز در حال کار.
3. کلیدهای `tenant_integration` که ادمین صاحبش صریحاً درخواست فعال‌سازی داده (self-service، بعد از این‌که C2 برای گروه ۲ بدون مشکل تایید شد).
4. **هرگز خودکار/دسته‌جمعی برای `legacy_global`/`remote_shared_bot`** - این دو نوع طبق طراحی بخش قبل اصلا `REQUIRES_OWNER`/قابل‌scope نیستند (`FORBIDS_OWNER`)؛ تبدیل آن‌ها به scoped یعنی اول باید به `tenant_integration` **rotate** شوند (کلید جدید صادر شود، قدیمی غیرفعال شود) - این خودش تصمیم جدا و out-of-scope همین rollout است (بخش Phase D سند).

### ۶. تضمین صریح: `legacy_global` و ربات‌های فعال دست‌نخورده می‌مانند

- هر کلید بدون `key_type` ستون (قبل از Phase A) یا با `key_type='legacy_global'`: `from_api_key` آن را دقیقاً `LEGACY_GLOBAL, owner_admin_id=None, scope_enforced=False` می‌سازد → `is_scoped=False` → `resolve_claimed_owner` claim خام را همان‌طور برمی‌گرداند، `require_bot_user_access`/`require_bot_node_access` بدون بررسی اجازه می‌دهند، `require_bot_capability` چون `DEFAULT_CAPABILITIES_BY_KEY_TYPE[LEGACY_GLOBAL] == ALL_CAPABILITIES` هرگز رد نمی‌کند. **نتیجه: صفر تغییر رفتار HTTP برای هر کلید موجود، تا زمانی که کسی دستی `scope_enforced` آن را true کند.**
- `remote_shared_bot` (کلید deploy ربات سرور دوم): همان استدلال - `scope_enforced=False` پیش‌فرض Phase B، `owner_admin_id=None` همیشه (چون `FORBIDS_OWNER`). صفر تغییر برای ربات remote زنده.
- ربات داخلی (in-process, shared و اختصاصی): تا مرحله‌ی C3 اصلا به `bot_auth` وصل نیست - یعنی حتی خودِ سوال «آیا رفتارش عوض می‌شود» تا C3 بی‌معنی است. از C3 به بعد، shared bot (`owner_admin_id=None`) طبق `is_scoped` همچنان بدون‌مقیاس می‌ماند؛ فقط ربات‌های **اختصاصی** (که از قبل هم مفهوم مالک دارند) وارد مسیر بررسی می‌شوند - و طبق طراحی، این دقیقاً همان چیزی است که پنل وب همان ادمین از قبل نشانش می‌داد، نه یک محدودیت تازه‌ی غیرمنتظره.
- **در هیچ نقطه از این rollout کد deploy/push شرط توقف shared bot یا remote bot نیست** - هر مرحله (C0-C4) فقط `ApiKey`-هایی را لمس می‌کند که خودشان صریحا انتخاب شده‌اند؛ خودِ deploy کد (C0, C3) روی رفتار زنده هیچ کلیدی اثر ندارد مگر آن کلید از قبل `scope_enforced=true` باشد.

### ۷. فهرست دقیق تغییرات پیشنهادی (بدون اعمال - فقط فهرست)

| # | فایل | تغییر | مرحله |
|---|---|---|---|
| ۱ | `backend/app/deps.py` | افزودن `get_bot_principal(api_key=Depends(get_bot_api_key)) -> BotPrincipal`؛ تغییر نام/محدودسازی دسترسی به `get_bot_api_key` خام | C0 |
| ۲ | `backend/app/routers/bot.py` | تغییر نوع پارامتر `Depends` در تمام endpointها از `ApiKey` به `BotPrincipal`؛ استخراج بدنه‌ی هر endpoint به تابع داخلی `_do_*(principal, ...)` قابل‌فراخوانی هم از HTTP هم از bridge؛ افزودن فراخوانی `resolve_claimed_owner`/`require_bot_capability`/`require_bot_user_access`/`require_bot_node_access` طبق جدول بخش ۲ در هرکدام | C0 |
| ۳ | `backend/app/services/bot_auth.py` | افزودن `accessible_node_ids_for(db, principal)` (بخش ۳) | C0 |
| ۴ | `backend/app/routers/bot.py` (`GET /nodes`) | فیلتر با خروجی `accessible_node_ids_for` | C0 (رفتار عوض نمی‌شود تا کلیدی scoped شود) |
| ۵ | `backend/app/telegram_bot/panel_bridge.py` | ساخت `BotPrincipal.internal(config.bot_owner_admin_id)` به‌جای `_scope()`ی امروزی؛ فراخوانی همان توابع داخلی `_do_*` بجای صدا زدن مستقیم بدنه‌ی endpoint قدیمی | C3 (جدا، آخر) |
| ۶ | `backend/tests/test_bot_auth_principal.py` | بدون تغییر منطقی - همچنان معتبر چون فقط واحد `bot_auth.py` را تست می‌کند | - |
| ۷ | `backend/tests/test_bot_api_key_cross_tenant.py` | تبدیل به تست «بعد از C2، با یک کلید scope_enforced=true، cross-tenant واقعا رد می‌شود» - یک نسخه‌ی جدید، نه ویرایش نسخه‌ی امروز (که رفتار «قبل» را مستند می‌کند و باید بماند به‌عنوان regression تاریخی) | C2 |
| ۸ | `backend/tests/test_bot_inprocess_trust_characterization.py` | افزودن نسخه‌ی معکوس (بخش ۴ بالا) - فایل جدید یا سناریوی جدید در همان فایل، بعد از C3 | C3 |
| ۹ | `backend/tests/test_api_key_phase_b.py` | خط‌های `check("... remains free of BotPrincipal wiring", ...)` باید به نسخه‌ی «هست، رفتار یکسان» تبدیل شوند (بخش ۴ بالا) | C0 |
| ۱۰ | مستند این فایل | افزودن گزارش نتایج تست بعد از هر مرحله (C0...C4)، طبق پروتکل استاندارد | هر مرحله |

**دامنه‌ی این commit:** هیچ‌کدام از ۱۰ مورد بالا اعمال نشده - این صرفاً فهرست است. رمز مادر (`config.py`/`routers/auth.py`) در هیچ مورد از این فهرست ذکر یا لمس نشده.

## باز برای بازبینی Product (Phase C)

- تایید کلی معماری dependency مرکزی (بخش ۱) و اینکه استخراج `_do_*` قابل‌قبول است یا رویکرد دیگری (مثل یک لایه‌ی adapter جدا) ترجیح دارد.
- تایید جدول endpoint→capability بخش ۲ - به‌خصوص ردیف‌های «تغییر امضا لازم؟ = نه» که برایشان owner از رکورد (نه پارامتر) گرفته می‌شود (`link_telegram`, `add_balance`, کارت‌های پرداخت).
- تایید طراحی `/nodes`/`pick_node` (بخش ۳) و صراحتاً تایید/رد اینکه رابطه‌ی پکیج↔نود از دامنه‌ی Phase C خارج بماند.
- تایید ترتیب C0-C4 و ترتیب تبدیل `scope_enforced` (بخش‌های ۴/۵).
- تایید این‌که فهرست ۱۰-موردی بخش ۷ کامل است یا موردی کم دارد، پیش از شروع پیاده‌سازی واقعی C0.

## Phase C - نسخه‌ی دوم (اصلاح ۶ نقص P1 از بازبینی مستقل کد، ۲۰۲۶-۰۹-۲۸)

**نسخه‌ی اول رد شد.** بازبینی مستقل با خواندن مستقیم کد (نه صرفاً این سند) ۶ نقص P1 پیدا کرد. هر ۶ مورد با خواندن فایل‌های واقعی (`routers/bot.py`, `services/hierarchy.py`, `telegram_bot/panel_bridge.py`, `telegram_bot/handlers/admin_pending.py`, `models.py`, `schemas.py`) دوباره تایید شد - نه صرفاً پذیرفته شد. جزئیات هر نقص + مدرک کد + اصلاح:

### نقص ۱ - فرض «dependency فعلی per-endpoint است» غلط بود

**مدرک:** `routers/bot.py:31` - `router = APIRouter(prefix="/api/bot", tags=["bot"], dependencies=[Depends(get_bot_api_key)])`. این یک dependency **سطح router** است - نتیجه‌اش هیچ‌جا inject نمی‌شود. تایید شد با grep کامل فایل: **هیچ‌یک** از ~۳۵ endpoint پارامتری از نوع `api_key: models.ApiKey = Depends(...)` ندارد. علاوه بر این، `panel_bridge.py` همین توابع (`bot_router.list_users`, `bot_router.create_user`, ...) را **مستقیم به‌عنوان تابع پایتون** با `owner_admin_id=_scope(owner_admin_id)` صدا می‌زند - بدون گذر از FastAPI اصلاً.

**نتیجه:** تبدیل مکانیکی «فقط نوع پارامتر Depends را عوض کن» که در نسخه‌ی اول ادعا شده بود (بخش ۱/۲ نسخه‌ی اول) **ممکن نیست** - چون چنین پارامتری از اول وجود نداشت. و چون `panel_bridge.py` همان توابع را مستقیم صدا می‌زند (نه از طریق FastAPI)، افزودن یک پارامتر اجباری جدید (`principal`) به امضای این توابع **همان لحظه** فراخوانی‌های `panel_bridge.py` را می‌شکند مگر در همان commit اصلاح شود - یعنی جدا کردن «C0 = فقط HTTP» از «C3 = فقط in-process» (که نسخه‌ی اول فرض کرده بود) از نظر معماری امکان‌پذیر نیست.

**طراحی اصلاح‌شده:**
```text
- router-level dependencies=[Depends(get_bot_api_key)] دست‌نخورده می‌ماند - فقط
  رد کردن ۴۰۱ برای کلید غیرمعتبر/غیرفعال (رفتار امروز، بدون تغییر).
- به امضای هر یک از ~۳۵ endpoint در routers/bot.py یک پارامتر واقعا جدید
  اضافه می‌شود: principal: bot_auth.BotPrincipal = Depends(deps.get_bot_principal)
  (این یک تغییر امضای واقعی است - بر خلاف ادعای نسخه‌ی اول، نه یک no-op).
- در همان commit، panel_bridge.py برای هر فراخوانی مستقیم یک
  BotPrincipal.internal(config.bot_owner_admin_id, scope_enforced=False)
  می‌سازد و به همان تابع پاس می‌دهد - نه بعدتر (نقص ۱ دقیقاً همین را رد می‌کرد).
- scope_enforced=False صریح در internal() (override پیش‌فرض True خودِ متد،
  طبق داکیومنت آن در bot_auth.py) لازم است چون owner_admin_id ربات‌های
  اختصاصی زنده همین امروز None نیست - بدون این override، همین commit
  enforcement را برای هر ربات اختصاصی در حال کار روشن می‌کرد.
```

### نقص ۲ - rollback با `scope_enforced=false` تنها، کلید را cross-tenant می‌کند

**مدرک:** در `bot_auth.py`، `require_bot_capability` مستقل از `scope_enforced` روی مجموعه‌ی `capabilities` تصمیم می‌گیرد؛ اما `resolve_claimed_owner`/`require_bot_user_access`/`require_bot_node_access` هر سه روی `principal.is_scoped` شرط می‌خورند که با `scope_enforced=False` بلافاصله `False` می‌شود. یک کلید `tenant_integration` که در Phase B فعال شده، از قبل `capabilities={customer_read, customer_write}` را **مستقل از scope_enforced** دارد. پس اگر یک ادمین برای rollback فقط `scope_enforced` را `false` کند و کلید را `enabled=true` نگه دارد، آن کلید دقیقاً همان capabilityها را نگه می‌دارد ولی حالا **بدون** بررسی owner/user/node - یعنی rollback به شکل پیشنهادی نسخه‌ی اول، وضعیت را از «scoped» به «cross-tenant با دسترسی نوشتن» بدتر می‌کند، نه به حالت امن قبلی برمی‌گرداند.

**اصلاح:** تنها rollback مجاز، خاموش‌کردن خودِ کلید است، نه فقط پرچم scope:
```text
UPDATE api_keys SET enabled = false WHERE id = ...
```
(نه `scope_enforced = false` به‌تنهایی روی یک کلید `enabled=true`.) چون یک کلید `enabled=false` حتی به `deps.get_bot_api_key` هم نمی‌رسد (رد می‌شود قبل از ساخت هر principal)، این تنها rollback است که هیچ حالت واسط ناامن ندارد. بخش rollback نسخه‌ی اول با همین قاعده بازنویسی می‌شود.

### نقص ۳ - `resolve_claimed_owner` با مدل hierarchy ناسازگار است

**مدرک:** `hierarchy.owned_admin_ids` (`services/hierarchy.py:182`) به یک Admin اجازه می‌دهد **اتحاد خودش + کل زیردرخت Sellerهایش** را ببیند - `require_bot_user_access` همین حالا درست از این استفاده می‌کند (`hierarchy.can_see_user`). ولی `resolve_claimed_owner` حتی پارامتر `db` هم ندارد و فقط تساوی دقیق (`claimed_owner_id != principal.owner_admin_id`) را بررسی می‌کند - از نظر ساختاری نمی‌تواند hierarchy را مشورت کند. مسیر واقعی که این فرق را نشان می‌دهد: `telegram_bot/handlers/admin_pending.py`'s `_approval_actor` (خط ~۴۰۸-۴۵۰) به یک Admin کامل اجازه می‌دهد رسید مربوط به یک **Seller زیرمجموعه‌اش** را تایید کند (`storage.may_handle` با `owner_ids` کامل زیردرخت، نه تساوی دقیق) - و `perform_approval` (همان فایل، خط ۱۷۲) که نتیجه‌اش را اجرا می‌کند، باید کاربر/خرید جدید را زیر owner واقعی همان درخواست (که می‌تواند owner Seller باشد، نه Admin تاییدکننده) ثبت کند. `resolve_claimed_owner`ی که فقط تساوی دقیق قبول می‌کند، دقیقاً همین سناریوی مشروع را رد می‌کند.

**اصلاح - امضا و منطق `resolve_claimed_owner` در `bot_auth.py` باید عوض شود** (یک تغییر کد واقعی روی artifact نوشته‌شده‌ی Phase A/B - هنوز صفر اثر زنده چون چیزی این تابع را صدا نمی‌زند):
```python
def resolve_claimed_owner(db: Session, principal: BotPrincipal, claimed_owner_id: Optional[int], *, endpoint=None) -> Optional[int]:
    _ensure_valid(principal, ...)
    if not principal.is_scoped:
        return claimed_owner_id            # بدون تغییر
    admin = db.get(models.AdminUser, principal.owner_admin_id)
    owned = hierarchy.owned_admin_ids(db, admin) if admin else set()
    if claimed_owner_id is not None and claimed_owner_id not in owned:
        raise HTTPException(403, _DENIED_MESSAGE)
    return claimed_owner_id if claimed_owner_id is not None else principal.owner_admin_id
```
`test_bot_auth_principal.py`'s سناریوهای مربوط به این تابع باید هم‌زمان با این تغییر بازنویسی شوند (بخش تغییرات پیشنهادی پایین).

### نقص ۴ - فیلتر نود گردش Seller را می‌شکند

**مدرک:** `hierarchy.accessible_node_ids` (`services/hierarchy.py:274`) عمداً برای هر Seller `set()` (خالی) برمی‌گرداند - «Sellerها هیچ‌وقت مستقیم نود مدیریت نمی‌کنند». اما مسیر خرید پکیج ساده (`telegram_bot/handlers/customer_purchase.py`'s pick_node) وقتی پکیج هیچ `PackageConnection` از پیش‌تعیین‌شده‌ای ندارد، به یک لیست نود غیرخالی برای انتخاب دستی مشتری نیاز دارد - و این مسیر برای مشتریان زیرمجموعه‌ی Seller هم اجرا می‌شود (Seller دقیقاً همان پکیج‌های ساده‌ی Admin والدش را بازفروش می‌کند). طراحی نسخه‌ی اول (`accessible_node_ids_for` = مستقیم `hierarchy.accessible_node_ids(db, admin)`) این لیست را برای هر principal اسکوپ‌شده‌ی Seller، بعد از C3، خالی می‌کند - یک شکست واقعی، نه یک edge case.

**اصلاح:** باید از `hierarchy.parent_admin_scope_id` (`services/hierarchy.py:166` - از قبل دقیقاً برای همین منظور نوشته شده: «کدام Admin's granted nodes/owned packages این حساب باید استفاده کند») عبور کرد، نه از admin خودِ principal مستقیم:
```python
def accessible_node_ids_for(db: Session, principal: BotPrincipal) -> Optional[set[int]]:
    if not principal.is_scoped:
        return None
    admin = db.get(models.AdminUser, principal.owner_admin_id)
    if admin is None:
        return set()
    scope_id = hierarchy.parent_admin_scope_id(admin)   # خودش برای Admin، والدش برای Seller
    scope_admin = admin if scope_id == admin.id else db.get(models.AdminUser, scope_id)
    return hierarchy.accessible_node_ids(db, scope_admin) if scope_admin else set()
```
برای یک Seller این حالا نودهای **Admin والدش** را برمی‌گرداند (همان نودهایی که پکیج‌های قابل بازفروشش واقعاً استفاده می‌کنند)، نه یک مجموعه‌ی همیشه‌خالی. **این یک انحراف عمدی از `routers/nodes.py:98-99`** (که چنین resolve والد را ندارد) است - چون صفحه‌ی مدیریت نود پنل وب اصلاً هیچ استفاده‌ی Seller-facing ندارد، ولی `pick_node` ربات یک مسیر عملیاتی مشتری‌محور است که راه دیگری برای تکمیل خرید پکیج ساده ندارد. این انحراف باید صریح به تایید Product برسد، نه silently معادل فرض شود.

هم‌راستا با یادداشت Product: مسیر ساخت connection (`add_connection`/`create_user`'s پارامتر `connections`، وقتی `node_id` دستی از پکیج ساده می‌آید) هم باید از همین `accessible_node_ids_for` اصلاح‌شده (نه نسخه‌ی خام hierarchy) برای `require_bot_node_access` استفاده کند - نه فقط `/nodes` خودش. این خودش پاسخ به یادداشت سوم Product پایین‌تر است.

### نقص ۵ - چند ردیف جدول endpoint با مدل واقعی مغایر بودند

| Endpoint | ادعای نسخه‌ی اول | اصلاح‌شده (با مدرک) |
|---|---|---|
| `GET /payment-info` | `CUSTOMER_READ` | **`PAYMENT_READ`** - `routers/bot.py:441` شماره کارت/صاحب کارت/دستورالعمل پرداخت برمی‌گرداند (`resolve_active_card`)؛ داده‌ی پرداخت است، نه پروفایل مشتری |
| `GET /tutorials/{id}/media/{id}/download`, `/software/{id}/download` | «tutorial تنانتی نیست، بدون owner check» | **غلط بود** - `models.Tutorial.owner_admin_id` واقعا وجود دارد (`models.py:2257`, «هر Admin لیست کاملا جدا دارد»). باید مثل بقیه از `resolve_claimed_owner`ی اصلاح‌شده (نقص ۳) رد شود، نه بدون بررسی مالکیت |
| `GET /admin-by-telegram/{tg_id}` | «افشای کم، فقط ADMIN_LOOKUP» | `schemas.BotAdminInfo` (`schemas.py:1693`) یک فیلد `owner_ids` (کل زیردرخت hierarchy) + `sell_block_reason` هم برمی‌گرداند - افشای واقعی، نه کم. باید علاوه بر `ADMIN_LOOKUP`، فقط برای `tg_id`ای که به خودِ principal یا زیردرخت خودش resolve می‌شود پاسخ دهد (همان بررسی hierarchy نقص ۳)، نه برای هر `tg_id` دلخواه |
| endpointهای بدون capability (`customer-menu-config`, `miniapp-button-text`, `panel-public-url`, `/nodes` پیش از فیلتر) | «هیچ» | تصریح شد: این‌ها هنوز باید از `_ensure_valid` (رد یک principal نامعتبر) عبور کنند - «بدون capability» یعنی بدون گیت دومِ specific، نه بدون گیت اول |

### سه یادداشت اضافه‌ی Product - همه پذیرفته شد

1. **C3 باید per-owner rollback مستقل داشته باشد:** یک ستون additive جدید پیشنهاد می‌شود - `AdminUser.dedicated_bot_scope_enforced` (bool, default `False`). فعال‌سازی/rollback enforcement in-process هر ربات اختصاصی یعنی صرفاً `UPDATE admin_users SET dedicated_bot_scope_enforced=... WHERE id=...` روی همان یک ردیف - نه یک سوییچ سراسری کد/دیپلوی. `panel_bridge.py` این ستون را می‌خواند تا مقدار `scope_enforced` واقعی برای `BotPrincipal.internal(...)` را (به‌جای همیشه `False` بعد از C0) تعیین کند.
2. **نام‌گذاری C0:** به «C0 - Wiring/Preflight» تغییر می‌کند (نه «observation-only») - چون طبق اصلاح نقص ۱، C0 حالا واقعاً هم مسیر HTTP هم `panel_bridge.py` را وایر می‌کند (فقط enforced نمی‌کند)، پس از همان لحظه هر دو مسیر از `_log_decision` رد می‌شوند و داده‌ی observation واقعی تولید می‌کنند - نگرانی «C0 قبلی چیزی تولید نمی‌کرد» با اصلاح نقص ۱ به‌طور طبیعی برطرف شد.
3. **mismatch پکیج↔نود صرفاً data-integrity نبود:** تایید شد - این دقیقاً همان چیزی است که نقص ۴ بالا حل می‌کند (`accessible_node_ids_for` اصلاح‌شده باید هم `/nodes` هم مسیر ساخت connection را بپوشاند)؛ از فهرست «باز برای بعد» نسخه‌ی اول حذف شد چون جزو Phase C است، نه بعد از آن.

### تغییرات لازم در فهرست ۱۰-موردی نسخه‌ی اول (بخش ۷ بالا)

- ردیف ۱/۲ (dependency + panel_bridge): دیگر دو مرحله‌ی جدا (C0 سپس C3) نیستند - یک commit واحد wiring، طبق نقص ۱.
- ردیف ۳ (`accessible_node_ids_for`): باید از ابتدا با resolve از طریق `parent_admin_scope_id` نوشته شود (نقص ۴)، نه نسخه‌ی ساده‌ی نسخه‌ی اول.
- **مورد جدید ۱۱:** `backend/app/services/bot_auth.py` - بازنویسی `resolve_claimed_owner` با پارامتر `db` و بررسی `hierarchy.owned_admin_ids` (نقص ۳)، به‌علاوه‌ی بررسی هم‌ارز برای `admin-by-telegram` (نقص ۵).
- **مورد جدید ۱۲:** `backend/app/models.py` - افزودن `AdminUser.dedicated_bot_scope_enforced` (additive، یادداشت Product #۱).
- **مورد جدید ۱۳:** جدول rollback بخش ۴ نسخه‌ی اول با قاعده‌ی «فقط `enabled=false`» بازنویسی شود (نقص ۲).

### وضعیت

اجرای C0 (حتی بدون enforcement) **متوقف می‌ماند** تا همین نسخه‌ی دوم هم بازبینی/تایید Product شود. هیچ کد production، commit، یا push در این دور هم انجام نشد - فقط بازخوانی کد واقعی (`routers/bot.py`, `services/hierarchy.py`, `telegram_bot/panel_bridge.py`, `telegram_bot/handlers/admin_pending.py`, `models.py`, `schemas.py`) و اصلاح سند.

---

# Phase C - نسخه‌ی سوم (canonical - تنها مرجع اجرا)

**این بخش، از اینجا تا انتهای سند، تنها مرجع اجرای Phase C است.** نسخه‌ی اول و دوم بالا فقط برای تاریخچه/سابقه‌ی بازبینی نگه داشته شده‌اند - طبق دستور صریح («پیاده‌ساز نباید مجبور شود نسخه‌ی اول و deltaهای نسخه‌ی دوم را ذهنی ادغام کند»)، هیچ نکته‌ای که در این بخش تکرار نشده باشد معتبر نیست؛ اگر نسخه‌ی اول/دوم با اینجا تناقض دارند، **این بخش برنده است**.

۴ نقص امنیتی مسدودکننده + ۲ نقص rollout از بازبینی سوم Product، هر کدام با خواندن مستقیم کد دوباره تایید شد:
- `BotPrincipal.from_api_key` امروز هیچ invariantای بین `key_type`/`scope_enforced` بررسی نمی‌کند (`bot_auth.py`) - یک ردیف `tenant_integration, enabled=true, scope_enforced=false` (به هر دلیل: باگ، ویرایش دستی DB) یک principal معتبر و **unscoped** می‌سازد که همان `customer_read`/`customer_write` را cross-tenant اجرا می‌کند.
- `resolve_claimed_owner` (نسخه‌ی دوم) برای *ادعای owner یک نوشتن جدید* طراحی شده، نه برای *بررسی مالکیت یک resource موجود* - برای `Tutorial`/`Package`/`PaymentCard` که `owner_admin_id=NULL` معنی «متعلق به سوپرادمین» دارد (نه «هیچ‌کس»)، این دو عملیات با هم قاطی شده‌اند.
- `accessible_node_ids_for` (نسخه‌ی دوم) وقتی یک Seller **مستقیماً زیر سوپرادمین** باشد (پیکربندی معتبر و صریحاً مجاز - `hierarchy.validate_placement`) از طریق `parent_admin_scope_id` به ردیف سوپرادمین می‌رسد و `hierarchy.accessible_node_ids` برای سوپرادمین `None` (بی‌محدودیت) برمی‌گرداند - یعنی این Seller به همه‌ی نودهای کل پنل دسترسی پیدا می‌کند.
- `routers/packages.py:_sync_connections` (کد امروزی، نه Phase C) فقط وجود نود و تطابق پروتکل را بررسی می‌کند - هیچ‌جا بررسی نمی‌کند نودی که در `PackageConnection` ذخیره می‌شود متعلق/گرانت‌شده به همان Adminی است که پکیج را می‌سازد. تایید شد با خواندن کامل تابع (`routers/packages.py:155-193`) و هر دو call site آن (خط ۳۰۵ و ۳۵۰).
- افزودن دستی `principal` به هر endpoint هیچ ضامنی ندارد که endpoint بعدی آن را فراموش نکند - نیاز به یک registry+test که مجموعه‌ی دقیق route های `/api/bot` را با یک policy مرکزی مقایسه کند.
- لاگ C0 (نسخه‌ی دوم) وقتی principal همیشه unscoped ساخته می‌شود، فقط «unscoped=allow» ثبت می‌کند - هیچ پیش‌بینی از رفتار enforced آینده نیست.

## ۱. Invariant های fail-closed (اصلاح‌شده روی `bot_auth.py`)

```python
# در BotPrincipal.from_api_key، بعد از بررسی REQUIRES_OWNER/FORBIDS_OWNER امروزی:
if key_type == KeyType.TENANT_INTEGRATION and not scope_enforced:
    # یک کلید tenant_integration که enabled=true است ولی scope_enforced=false
    # مانده - چه به دلیل باگ، چه ویرایش دستی DB، چه یک حالت میانی ناقص از
    # activation - هرگز نباید به‌عنوان "unscoped" (مثل legacy) رفتار کند؛
    # چون قبلاً capabilities محدودش (customer_read/customer_write) را دارد،
    # unscoped‌شدن یعنی همان capabilityها را cross-tenant اجرا کند - بدتر از
    # رد کامل. فقط "معتبر ولی enforced" یا "نامعتبر" مجاز است، نه چیزی بینشان.
    return cls._invalid(key.id, key.label)
```

**Activation اتمیک (برای C2، جایگزین توگل دومرحله‌ای که فقط در Phase B برای *forced-disabled* لازم بود):** یک اندپوینت/عملیات جدید فعال‌سازی یک کلید tenant، هر دو ستون را در یک `UPDATE` واحد ست می‌کند:
```sql
UPDATE api_keys SET enabled = true, scope_enforced = true WHERE id = ... AND key_type = 'tenant_integration';
```
هرگز دو `UPDATE` جدا با یک لحظه‌ی میانی `enabled=true, scope_enforced=false` روی دیتابیس. این invariant، به‌همراه فیکس بالا در `from_api_key`، یعنی حتی اگر بایی این قاعده را دور بزند (race، migration ناقص، خطای عملیاتی)، principal ساخته‌شده به‌جای unscoped، رد کامل می‌شود - fail-closed در دو لایه‌ی مستقل (نوشتن + خوانش).

## ۲. جدا‌کردن «claim ورودی» از «مالکیت resource موجود»

دو عملیات کاملاً متفاوتند و باید دو تابع جدا بمانند:

**الف) Claim برای یک نوشتن/فیلتر جدید** (مثل `create_user`'s `owner_admin_id`, `list_users`'s فیلتر, `list_packages`) - همان `resolve_claimed_owner` نسخه‌ی دوم (با اصلاح hierarchy-aware نقص ۳ دور قبل)، بدون تغییر بیشتر:
```python
def resolve_claimed_owner(db: Session, principal: BotPrincipal, claimed_owner_id: Optional[int], *, endpoint=None) -> Optional[int]:
    _ensure_valid(principal, "resolve_claimed_owner", endpoint=endpoint)
    if not principal.is_scoped:
        return claimed_owner_id
    admin = db.get(models.AdminUser, principal.owner_admin_id)
    owned = hierarchy.owned_admin_ids(db, admin) if admin else set()
    if claimed_owner_id is not None and claimed_owner_id not in owned:
        _log_decision(principal, "resolve_claimed_owner", allowed=False, reason="claim خارج از owned_admin_ids", endpoint=endpoint)
        raise HTTPException(403, _DENIED_MESSAGE)
    return claimed_owner_id if claimed_owner_id is not None else principal.owner_admin_id
```

**ب) بررسی مالکیت یک resource از قبل بارگذاری‌شده** که `owner_admin_id`اش می‌تواند `NULL` باشد و `NULL` معنی «سوپرادمین» دارد، نه «بدون مالک/در دسترس همه» - تابع تازه، **پارامتری جدا برای «کدام owner_id ها مجازند» می‌گیرد که endpoint خودش با تابع hierarchy مخصوص همان نوع resource محاسبه می‌کند** (نه یک منطق عمومی داخل `bot_auth.py`، چون قاعده‌ی هر resource فرق دارد):
```python
def require_bot_resource_owner_access(
    principal: BotPrincipal, resource_owner_admin_id: Optional[int],
    allowed_owner_ids: set[Optional[int]], *, endpoint: Optional[str] = None,
) -> None:
    """allowed_owner_ids را خودِ endpoint از تابع hierarchy مخصوص همان نوع
    resource می‌گیرد - نه اینجا حدس زده می‌شود:
      - Tutorial  -> hierarchy.accessible_tutorial_owner_ids(admin)
      - Package   -> hierarchy.accessible_package_owner_ids(admin)
      - PaymentCard -> {None, principal.owner_admin_id} ∪ hierarchy.owned_admin_ids(db, admin)
        (سیاست تازه - هیچ accessible_payment_card_owner_ids موجود نیست؛
        باید صریح توسط Product تایید شود، نه صرفاً منعکس یک قاعده‌ی موجود)
    principal unscoped: همیشه مجاز (بدون تغییر رفتار امروز)."""
    _ensure_valid(principal, "require_bot_resource_owner_access", endpoint=endpoint)
    if not principal.is_scoped:
        _log_decision(principal, "require_bot_resource_owner_access", allowed=True, reason="unscoped", endpoint=endpoint)
        return
    allowed = resource_owner_admin_id in allowed_owner_ids
    _log_decision(principal, "require_bot_resource_owner_access", allowed=allowed, reason="resource policy", endpoint=endpoint)
    if not allowed:
        raise HTTPException(404, "موردی یافت نشد")
```

**کاربرد در جدول endpoint (بخش ۶):** دانلود tutorial، `get_payment_card`/`record_payment_card_use` باید از (ب) رد شوند، نه از (الف). `create_user`/`list_users`/`list_packages` (که owner برای یک عملیات جدید/فیلتر تعیین می‌کنند، نه بررسی مالکیت یک شیء موجود) از (الف) رد می‌شوند - همان چیزی که نسخه‌ی دوم داشت.

## ۳. `accessible_node_ids_for` - fail-closed برای Seller زیر سوپرادمین

```python
def accessible_node_ids_for(db: Session, principal: BotPrincipal) -> Optional[set[int]]:
    if not principal.is_scoped:
        return None
    admin = db.get(models.AdminUser, principal.owner_admin_id)
    if admin is None:
        return set()
    if hierarchy.is_seller(admin):
        parent = admin.parent_admin
        if parent is None:
            return set()
        if parent.is_superadmin:
            # پیکربندی معتبر (validate_placement صریحاً مجازش می‌داند) - ولی
            # accessible_node_ids(db, superadmin) == None (بی‌محدودیت، چون
            # نظارت زیرساخت نود برای سوپرادمین عمداً ایزوله نشده). دادن آن
            # مستقیم به یک Seller یعنی دسترسی به کل نودهای پنل - fail-closed
            # به همان ترجمه‌ای که accessible_package_owner_ids/
            # accessible_tutorial_owner_ids برای همین پیکربندی استفاده
            # می‌کنند: فقط نودهای صریحاً global (owner_admin_id IS NULL).
            return {
                row.id for row in
                db.query(models.Node.id).filter(models.Node.owner_admin_id.is_(None)).all()
            }
        if hierarchy.role(parent) != hierarchy.ROLE_ADMIN:
            # هر شکل غیرمنتظره‌ی دیگر (داده‌ی خراب، نقشی که این تابع
            # نمی‌شناسد) - fail-closed، هرگز بی‌محدودیت.
            return set()
        scope_admin = parent
    else:
        scope_admin = admin  # شامل حالت "خودِ principal یک سوپرادمین با بات اختصاصی خودش است" - عمداً None/بی‌محدودیت، چون این دقیقاً همان چیزی است که آن سوپرادمین در پنل هم می‌بیند
    return hierarchy.accessible_node_ids(db, scope_admin)
```

## ۴. `_sync_connections` و provisioning - defense-in-depth، هر دو داخل Phase C

**Write-time (`routers/packages.py:_sync_connections`, خطوط ۱۵۵-۱۹۳):** بعد از بررسی وجود نود/تطابق پروتکل امروزی، یک بررسی جدید اضافه می‌شود - این تغییر مربوط به `bot_auth`/BotPrincipal نیست (این مسیر از `get_current_admin`ی معمولی پنل وب می‌آید، نه از `/api/bot`)، بلکه یک باگ authorization مستقل در همان router است که چون دقیقاً همان سوال «این tenant به این node دسترسی دارد یا نه» را می‌پرسد، طبق یادداشت Product داخل دامنه‌ی همین Phase C می‌ماند:
```python
allowed_node_ids = hierarchy.accessible_node_ids(db, admin)  # admin = caller از get_current_admin
if allowed_node_ids is not None and node.id not in allowed_node_ids:
    raise HTTPException(403, f"سرور «{node.name}» در اختیار شما نیست")
```
(`admin` سوپرادمین همچنان `None`/بی‌محدودیت می‌گیرد - رفتار امروزش برای سوپرادمین بدون تغییر.)

**Provisioning-time (defense-in-depth، وقتی customer واقعاً پکیج را می‌خرد/تمدید می‌کند):** حتی با فیکس بالا، یک `PackageConnection` که قبل از این فیکس ذخیره شده (یا با یک مسیر دیگر دستکاری DB) می‌تواند هنوز نود نامربوط داشته باشد. `user_ops`/مسیر ایجاد connection واقعی هنگام provisioning باید همان بررسی را دوباره - این‌بار در برابر owner واقعی پکیج (نه caller لحظه‌ی خرید که می‌تواند مشتری/بات باشد) - تکرار کند، تا یک ردیف قدیمی خراب هرگز سرویس واقعی روی نود نامربوط راه‌اندازی نکند. جای دقیق کد (کدام تابع در `user_ops.py`) نیاز به یک بررسی جدا از فایل آن سرویس دارد - **این ریز-طراحی برای دور بعد باز می‌ماند، ولی جزو محدوده‌ی Phase C است، نه بعد از آن.**

## ۵. Route-policy registry - عدم‌فراموشی تضمینی، نه دستی

به‌جای اعتماد به «هر endpoint یادش باشد principal/require_bot_capability را صدا بزند»، یک ثبت مرکزی صریح:

```python
# services/bot_auth.py یا یک ماژول جدا کنار routers/bot.py
ROUTE_POLICIES: dict[tuple[str, str], "RoutePolicy"] = {
    ("GET", "/nodes"): RoutePolicy(capability=None, resource_check="node_list"),
    ("GET", "/packages"): RoutePolicy(capability=CUSTOMER_READ, resource_check="claim"),
    ("GET", "/payment-info"): RoutePolicy(capability=PAYMENT_READ, resource_check="claim"),
    ("GET", "/payment-cards/{card_id}"): RoutePolicy(capability=PAYMENT_READ, resource_check="payment_card_owner"),
    # ... هر ~۳۵ route با دقیقاً یک ردیف، هیچ مقدار پیش‌فرض ضمنی
    ("GET", "/customer-menu-config"): RoutePolicy(capability=None, resource_check="none"),  # هنوز از _ensure_valid رد می‌شود
}
```

**تست پوشش صددرصدی (بخشی از همین Phase C، نه اختیاری):**
```python
# tests/test_bot_route_policy_coverage.py
declared_routes = {(r.methods, r.path) for r in bot_router.routes}
assert declared_routes == set(ROUTE_POLICIES.keys())  # هر route جدید بدون policy، این تست را می‌شکند
```
و یک تست دوم که تایید می‌کند خودِ endpoint واقعاً از `ROUTE_POLICIES` بخواند (نه یک کپی دستی) - مثلاً با monkey-patch کردن policy یک route و دیدن که نتیجه‌ی واقعی تغییر می‌کند، نه فقط اینکه دیکشنری درست است.

## ۶. Shadow evaluation واقعی برای C0 (نه صرفاً تغییر نام)

C0 («Wiring/Preflight»، بدون تغییر) هر principal را با `scope_enforced` واقعی‌اش می‌سازد (نه همیشه False) - طبق طراحی خودِ `bot_auth.py`، برای کلیدهای/بات‌های موجود امروز این همیشه `False` است، پس رفتار پاسخ صفر تغییر می‌کند. برای اینکه لاگ همان لحظه معنا‌دار باشد، هر یک از ۴ تابع مرکزی یک نسخه‌ی shadow اضافه می‌گیرد که **فقط لاگ می‌کند، هرگز روی پاسخ اثر نمی‌گذارد**:

```python
def _shadow_would_allow(principal: BotPrincipal, real_check: Callable[[], None]) -> bool:
    """principal واقعی را با یک principal فرضیِ scope_enforced=True کپی می‌کند
    و همان تابع بررسی را روی آن اجرا می‌کند - فقط برای لاگ، exception همان‌جا
    catch می‌شود، هرگز به بالا raise نمی‌شود."""
    shadow = dataclasses.replace(principal, scope_enforced=True)
    try:
        real_check(shadow)
        return True
    except HTTPException:
        return False

# در هر یک از ۴ تابع مرکزی، بعد از تصمیم واقعی:
if not principal.scope_enforced and principal.owner_admin_id is not None:
    would_allow = _shadow_would_allow(principal, lambda p: <همان تابع با p>)
    logger.info("bot_auth shadow: action=%s would_allow_if_enforced=%s ...", action, would_allow)
```

این دقیقاً همان چیزی است که Product به‌عنوان جایگزین «فقط تغییر نام C0» خواسته - از لحظه‌ی C0، برای هر principal دارای owner (حتی وقتی enforcement خاموش است)، لاگ می‌گوید اگر همین الان enforced بود چه اتفاقی می‌افتاد؛ این دیتای واقعی rollout است، نه فقط یک ادعای اسمی.

## ۷. جدول کامل و نهایی endpoint → capability → روش بررسی (canonical)

| Endpoint | Capability | روش بررسی |
|---|---|---|
| `GET /nodes` | *(ندارد)* | فیلتر با `accessible_node_ids_for` (بخش ۳) |
| `GET /packages` | `CUSTOMER_READ` | `resolve_claimed_owner` (بخش ۲-الف) |
| `GET /payment-info` | `PAYMENT_READ` | `resolve_claimed_owner` |
| `GET /payment-cards/{card_id}` | `PAYMENT_READ` | `require_bot_resource_owner_access` با `{None, principal.owner_admin_id} ∪ owned_admin_ids` (بخش ۲-ب) |
| `POST /payment-cards/{card_id}/record-payment` | `PAYMENT_WRITE` | مثل بالا |
| `GET /sales-stats` | `CUSTOMER_READ` | `resolve_claimed_owner` |
| `GET /customer-menu-config` | *(ندارد)* | فقط `_ensure_valid` |
| `GET /tutorials` | `CUSTOMER_READ` | `resolve_claimed_owner` (فهرست، نه یک شیء بارگذاری‌شده - فیلتر با `accessible_tutorial_owner_ids`) |
| `GET /packages/{id}/files/{id}/download` | `FILES_READ` | `require_bot_resource_owner_access` با `accessible_package_owner_ids(admin)` |
| `GET /tutorials/{id}/media\|software/{id}/download` | `FILES_READ` | `require_bot_resource_owner_access` با `accessible_tutorial_owner_ids(admin)` |
| `GET /admin-by-telegram/{tg_id}` | `ADMIN_LOOKUP` | نتیجه فقط اگر `tg_id` به خودِ principal یا عضوی از `owned_admin_ids` resolve شود - وگرنه 404 (نه فقط capability) |
| `GET /admin-username/{admin_id}` | `ADMIN_LOOKUP` | `admin_id in owned_admin_ids ∪ {principal.owner_admin_id}` |
| `GET /telegram-user-ids` | `BROADCAST` | `resolve_claimed_owner` |
| `POST /users` | `CUSTOMER_WRITE` | `resolve_claimed_owner` روی `payload.owner_admin_id` |
| `POST /users/{u}/purchase-package` | `CUSTOMER_WRITE` | `require_bot_user_access` |
| `POST /referral/apply` | `CUSTOMER_WRITE` | `require_bot_user_access` |
| `POST /discount/validate,/redeem` | `CUSTOMER_WRITE` | `resolve_claimed_owner` |
| `GET /users` | `CUSTOMER_READ` | `resolve_claimed_owner` |
| `GET /users/by-telegram/{id}[/all]` | `CUSTOMER_READ` | `resolve_claimed_owner` |
| `GET /users/{username}` | `CUSTOMER_READ` | `require_bot_user_access` |
| `POST /users/{u}/link-telegram` | `IDENTITY_WRITE` | `require_bot_user_access` |
| `POST /users/{u}/connections` | `CUSTOMER_WRITE` | `require_bot_user_access` + اگر `node_id` صریح باشد، فیلتر با `accessible_node_ids_for` |
| `GET /users/{u}/purchases` | `CUSTOMER_READ` | `require_bot_user_access` |
| `POST /users/{u}/purchases/{id}/rename` | `CUSTOMER_WRITE` | `require_bot_user_access` |
| `DELETE /users/{u}/purchases/{id}` | `CUSTOMER_WRITE` | `require_bot_user_access` |
| `DELETE /users/{u}/connections/{id}` | `CUSTOMER_WRITE` | `require_bot_user_access` |
| `GET /users/{u}/subscription-link` | `CUSTOMER_READ` | `require_bot_user_access` |
| `GET /miniapp-button-text, /panel-public-url` | *(ندارد)* | فقط `_ensure_valid` |
| `POST /users/{u}/purchases/{id}/renew, /renew, /reset-usage, /set-enabled` | `CUSTOMER_WRITE` | `require_bot_user_access` |
| `POST /users/{u}/add-balance` | `WALLET_WRITE` | `require_bot_user_access` |
| `DELETE /users/{u}` | `CUSTOMER_WRITE` | `require_bot_user_access` |

هر ۳۵ ردیف بالا باید عیناً در `ROUTE_POLICIES` (بخش ۵) ظاهر شود - تست پوشش همین را تضمین می‌کند.

## ۸. Rollout نهایی (C0-C4، اصلاح‌شده)

- **C0 - Wiring/Preflight + Shadow.** یک commit واحد: `deps.get_bot_principal` + پارامتر `principal` به هر ۳۵ endpoint + `panel_bridge.py` هم‌زمان (طبق نقص ۱ نسخه‌ی دوم) + `ROUTE_POLICIES`/تست پوشش (بخش ۵) + shadow evaluation (بخش ۶). `panel_bridge.py` با `BotPrincipal.internal(config.bot_owner_admin_id, scope_enforced=admin.dedicated_bot_scope_enforced)` می‌سازد (ستون تازه، پیش‌فرض `False`، پس صفر تغییر رفتار زنده). `_sync_connections`ی فیکس‌شده (بخش ۴) همین‌جا هم می‌آید - مستقل از principal، رفتار سوپرادمین بدون تغییر.
- **C1 - یک کلید تست.** بدون تغییر از نسخه‌ی دوم.
- **C2 - فعال‌سازی تدریجی کلیدهای HTTP** با UPDATE اتمیک (بخش ۱)، ترتیب بدون تغییر از نسخه‌ی اول (بخش ۵ آن).
- **C3 - فعال‌سازی تدریجی per-owner in-process** با `UPDATE admin_users SET dedicated_bot_scope_enforced=true WHERE id=...` - هر ادمین/فروشنده مستقل، rollback همان یک `UPDATE` معکوس؛ دیگر یک دیپلوی سراسری نیست (چون خودِ کد از C0 وایر شده).
- **C4 - اختیاری/legacy.** بدون تغییر.
- **Rollback در هر مرحله:** برای کلید HTTP، فقط `enabled=false` (بخش ۱، نه تنها `scope_enforced=false`). برای in-process، فقط `dedicated_bot_scope_enforced=false` روی همان AdminUser (چون خودِ ستون از ابتدا مستقل طراحی شده، تنها حالت است - نیازی به قاعده‌ی اتمیک جدا ندارد چون هیچ «capability مستقل از این پرچم» برای in-process وجود ندارد؛ `BotPrincipal.internal()` همیشه `ALL_CAPABILITIES` می‌دهد، پس تنها متغیر همین یک پرچم `is_scoped` است).

## ۹. فهرست نهایی و کامل تغییرات (canonical - جایگزین فهرست‌های قبلی)

| # | فایل | تغییر |
|---|---|---|
| ۱ | `backend/app/services/bot_auth.py` | invariant تازه در `from_api_key` (بخش ۱)؛ `resolve_claimed_owner` با پارامتر `db` + hierarchy (بخش ۲-الف)؛ تابع تازه `require_bot_resource_owner_access` (بخش ۲-ب)؛ `accessible_node_ids_for` با فیکس Seller-زیر-سوپرادمین (بخش ۳)؛ `_shadow_would_allow` + فراخوانی‌اش در هر ۴ تابع مرکزی (بخش ۶)؛ `ROUTE_POLICIES`/`RoutePolicy` (بخش ۵) |
| ۲ | `backend/app/deps.py` | `get_bot_principal` |
| ۳ | `backend/app/routers/bot.py` | پارامتر `principal` به هر ۳۵ endpoint؛ فراخوانی توابع مرکزی طبق جدول بخش ۷ |
| ۴ | `backend/app/telegram_bot/panel_bridge.py` | ساخت `BotPrincipal.internal(config.bot_owner_admin_id, scope_enforced=<از AdminUser>)` به‌جای `_scope()`؛ پاس به همان توابع |
| ۵ | `backend/app/models.py` | افزودن `AdminUser.dedicated_bot_scope_enforced` (bool, default False, additive) |
| ۶ | `backend/app/routers/packages.py` | بررسی `hierarchy.accessible_node_ids` داخل `_sync_connections` (بخش ۴) |
| ۷ | `backend/app/services/user_ops.py` (یا فایل دقیق provisioning) | بررسی معکوس هنگام ساخت connection واقعی (بخش ۴ - defense-in-depth؛ محل دقیق نیازمند بررسی جدا) |
| ۸ | `backend/tests/test_bot_route_policy_coverage.py` (تازه) | پوشش صددرصدی `ROUTE_POLICIES` در برابر route های واقعی (بخش ۵) |
| ۹ | `backend/tests/test_bot_auth_principal.py` | بازنویسی سناریوهای `resolve_claimed_owner`/افزودن `require_bot_resource_owner_access`/`accessible_node_ids_for`/invariant تازه/shadow |
| ۱۰ | `backend/tests/test_bot_inprocess_trust_characterization.py` | نسخه‌ی معکوس بعد از C3 (بدون تغییر از نسخه‌ی اول) |
| ۱۱ | مستند این فایل | گزارش نتایج تست بعد از هر مرحله |

**دامنه‌ی همین commit فعلی (نوشتن این سند):** هیچ‌کدام از ۱۱ مورد بالا اعمال نشده - صفر کد production، صفر commit، صفر push. رمز مادر در هیچ نقطه از این تحلیل لمس نشده.

### وضعیت

اجرای C0 همچنان متوقف است تا همین نسخه‌ی سوم (canonical) بازبینی/تایید شود.

**اصلاحیه ۲۰۲۶-۰۹-۲۸ (بازبینی چهارم Product): نسخه‌ی سوم منسوخ شد.** ۵ نقص مسدودکننده + ۲ ناهماهنگی اجرایی پیدا شد؛ همه با خواندن مستقیم کد دوباره تایید شد. **از این‌جا به بعد، بخش «Phase C - نسخه‌ی چهارم» پایین‌تر تنها مرجع اجراست - این بخش (نسخه‌ی سوم) هم صرفاً تاریخچه شد.**

---

# Phase C - نسخه‌ی چهارم (canonical - تنها مرجع اجرا، جایگزین نسخه‌ی سوم)

بازبینی چهارم Product هر ۷ مورد را با خواندن مستقیم `panel_bridge.py`, `services/user_ops.py`, `routers/bot.py` دوباره تایید کرد:

1. **حذف `_scope()` رفتار زنده را می‌شکند** - در C0، `resolve_claimed_owner` برای principal unscoped مقدار ورودی را بدون تغییر برمی‌گرداند؛ اگر `panel_bridge.py` دیگر `_scope(owner_admin_id)` را صدا نزند، هر handlerای که (طبق طراحی امروزی خودِ `_scope`) `owner_admin_id` نمی‌فرستد، به‌جای پیش‌فرض owner ربات اختصاصی، `None` (یعنی «همه‌چیز» - رفتار ربات مشترک) می‌گیرد. `_scope()` دو کار جداست: **پیش‌فرض‌گذاری claim** (کاری که همیشه باید بماند) و **enforcement** (کاری که `resolve_claimed_owner`/`principal` انجام می‌دهد) - نسخه‌ی سوم این دو را قاطی کرده بود.
2. **چند مسیر in-process اصلاً از `bot_router` رد نمی‌شوند** - تایید شد با خواندن `panel_bridge.py`: `get_package_files` (خط ۹۲)، `get_tutorial_media` (خط ۲۱۲)، `get_tutorial_software_file` (خط ۲۳۸)، `get_admin_telegram_id` (خط ۳۳۲)، `get_admin_username` (خط ۳۶۱) همه مستقیم `SessionLocal()`+`db.query`/`db.get` هستند - هیچ‌کدام `bot_router.*` را صدا نمی‌زنند. افزودن `principal` به endpointهای HTTP `routers/bot.py` این ۵ تابع را اصلاً لمس نمی‌کند.
3. **Registry فقط کامل‌بودن دیکشنری را ثابت می‌کرد، نه اجرا شدنش** - یک دیکشنری جدا از خودِ endpoint، هیچ تضمینی نمی‌دهد که endpoint واقعاً از آن استفاده کرده.
4. **`None` در `allowed_owner_ids`ی کارت پرداخت بیش‌ازحد باز بود** - تایید شد با خواندن `get_payment_info` (نسخه‌ی سوم، بخش ۲): آن تابع عمداً کارت global را برای یک ادمین/فروشنده‌ی دارای بات اختصاصی fallback نمی‌کند («WHERE THE MONEY GOES does not fall back... Blank instead»). گذاشتن `None` در `allowed_owner_ids` هر tenant دقیقاً همان چیزی را که آن تصمیم عمداً جلویش را گرفته، از مسیر بات باز می‌کرد.
5. **Shadow evaluation می‌توانست خودِ درخواست واقعی را بترکاند** - طراحی قبلی فقط `HTTPException` را catch می‌کرد؛ هر خطای دیگر (DB، باگ برنامه‌نویسی) از دل شادو بیرون می‌ریخت.
6. **بررسی provisioning واقعاً implementation-ready نبود** - تایید شد با خواندن `services/user_ops.py`: `provision_package_connections` (خط ۱۱۹۳) و `apply_package_as_purchase` (خط ۱۳۲۶) و مسیر bulk-create (خط ۷۳۴) **هر سه** از یک تابع واحد - `provision_connection` (خط ۱۵۳۰) - رد می‌شوند؛ این خودش دقیقاً همان یک choke-point لازم است، نه سه جای جدا.
7. **شمارش/تست اشتباه** - `routers/bot.py` واقعاً **۳۸** route decorator دارد (تایید با `grep -c`)، نه ۳۵؛ و `set` (خروجی `route.methods`) hashable نیست - نمی‌شود در تاپل کلید دیکشنری/مجموعه گذاشت.

## ۱. حفظ `_scope()` در C0 - جداسازی defaulting از enforcement

`panel_bridge.py` **دقیقاً همان `_scope(owner_admin_id)`ی امروزی را نگه می‌دارد** - هیچ حذفی در C0. تنها چیزی که اضافه می‌شود: هر متد، **علاوه بر** claim دیفالت‌شده، یک `principal` هم می‌سازد و هر دو را به تابع مشترک `_do_*` پاس می‌دهد:

```python
# panel_bridge.py - بدون تغییر:
async def list_users(self, page=1, page_size=8, search=None, owner_admin_id=None):
    principal = BotPrincipal.internal(config.bot_owner_admin_id, scope_enforced=await _dedicated_scope_enforced())
    result = await _call(bot_router._do_list_users, page=page, page_size=page_size, search=search,
                          owner_admin_id=_scope(owner_admin_id), principal=principal)   # _scope() دست‌نخورده
    ...
```

`resolve_claimed_owner(db, principal, claimed_owner_id)` وقتی `principal.is_scoped=False` (وضعیت همه‌ی چیزهای امروز در C0) باشد، دقیقاً همان `claimed_owner_id` (که `_scope()` قبلاً پیش‌فرض owner ربات اختصاصی را در آن گذاشته) را برمی‌گرداند - یعنی **صفر تغییر رفتار**، برخلاف نسخه‌ی سوم که با حذف `_scope()` این پیش‌فرض را از بین می‌برد. **`_scope()` فقط بعد از C3 (enforcement in-process) و بعد از نوشتن تست معکوس مخصوص همین سناریو حذف می‌شود** - نه در C0.

## ۲. مسیرهای in-process که از `bot_router` رد نمی‌شوند - انتقال به سرویس مشترک

پنج تابع (بخش «تایید شد» بالا) باید منطق واقعی‌شان را به یک ماژول سرویس principal-aware مشترک منتقل کنند که **هم HTTP هم in-process** از همان یک نسخه صدا بزنند - نه یک نسخه‌ی HTTP و یک نسخه‌ی in-process جدا:

```python
# services/bot_files.py (تازه) - principal-aware, هم از routers/bot.py هم از panel_bridge.py صدا زده می‌شود
def get_package_files(db: Session, principal: BotPrincipal, package_id: int) -> list[dict]:
    package = db.get(models.Package, package_id)
    if package is None:
        raise HTTPException(404, "پکیج پیدا نشد")
    require_bot_resource_owner_access(principal, package.owner_admin_id,
                                       hierarchy.accessible_package_owner_ids(_admin_for(db, principal)))
    ... (همان منطق خواندن فایل از دیسک که امروز در panel_bridge.py هست)

# panel_bridge.py:
async def get_package_files(self, package_id: int) -> list[dict]:
    principal = BotPrincipal.internal(config.bot_owner_admin_id, scope_enforced=await _dedicated_scope_enforced())
    return await asyncio.to_thread(lambda: _with_session(bot_files.get_package_files, principal, package_id))

# routers/bot.py:
@router.get("/packages/{package_id}/files/{file_id}/download")
@bot_route_policy(capability=FILES_READ)
def download_package_file(package_id, file_id, principal=Depends(get_bot_principal), db=Depends(get_db)):
    files = bot_files.get_package_files(db, principal, package_id)
    ...
```
همین الگو برای `get_tutorial_media`/`get_tutorial_software_file` (با `accessible_tutorial_owner_ids`) و `get_admin_telegram_id`/`get_admin_username` (با همان بررسی hierarchy-membership که برای `admin-by-telegram`/`admin-username` در نسخه‌ی سوم، بخش ۷ جدول، طراحی شده بود) تکرار می‌شود - هر ۵ تابع، نه فقط دانلودهای فایل.

## ۳. Enforcement واقعاً مرکزی - decorator، نه فقط دیکشنری

کلید تصحیح: به‌جای کلید‌کردن بر اساس `(method, path)` رشته‌ای (که هم `set` غیرقابل‌hash دارد هم مسئله‌ی prefix)، **بر اساس خودِ object تابع endpoint** کلید می‌زنیم - و یک decorator، capability را قبل از اجرای بدنه‌ی تابع، خودکار enforce می‌کند:

```python
ROUTE_POLICIES: dict[Callable, Optional[str]] = {}   # fn -> capability یا None (یعنی VALID_ONLY)

def bot_route_policy(capability: Optional[str]):
    def decorator(fn):
        ROUTE_POLICIES[fn] = capability
        @functools.wraps(fn)
        def wrapper(*args, principal: BotPrincipal, **kwargs):
            if capability is None:
                _ensure_valid(principal, fn.__name__)
            else:
                require_bot_capability(principal, capability, endpoint=fn.__name__)
            return fn(*args, principal=principal, **kwargs)
        wrapper._bot_policy_applied = True
        return wrapper
    return decorator

@router.get("/users/{username}/add-balance".replace("{username}", "{username}"))  # (مثال - مسیر واقعی بدون تغییر)
@bot_route_policy(capability=WALLET_WRITE)
def add_balance(username: str, payload: ..., principal: BotPrincipal = Depends(get_bot_principal), db: Session = Depends(get_db)):
    ...
```

**تست پوشش واقعی (نه فقط دیکشنری):**
```python
# tests/test_bot_route_policy_coverage.py
from app.routers.bot import router
from fastapi.routing import APIRoute

routes = [r for r in router.routes if isinstance(r, APIRoute)]
assert len(routes) == 38   # عدد واقعی، تایید‌شده با grep - نه ۳۵
for r in routes:
    assert getattr(r.endpoint, "_bot_policy_applied", False), f"{r.path} فاقد @bot_route_policy است"
```
این مکانیزم دو چیز را با هم تضمین می‌کند: (۱) هیچ endpointای بدون decorator رد نمی‌شود (تست بالا مستقیم روی خودِ تابع endpoint چک می‌کند، نه یک دیکشنری جدا که ممکن است out-of-sync شود)، (۲) capability همیشه **قبل از** اجرای بدنه‌ی تابع بررسی می‌شود - نه اینکه به یاد بدنه‌ی تابع باشد. بررسی‌های resource-specific (`require_bot_user_access`, `require_bot_resource_owner_access`, `accessible_node_ids_for`) همچنان داخل بدنه‌ی هر تابع می‌مانند - چون به شیء بارگذاری‌شده نیاز دارند که در سطح decorator موجود نیست؛ این محدودیت اجتناب‌ناپذیر است (هر فریم‌وردی همین‌طور است)، نه یک نقص جدید.

## ۴. سیاست کارت پرداخت - `None` فقط برای principal بدون‌مالک

اصلاح جدول بخش ۷ نسخه‌ی سوم، ردیف `payment-cards`/`record-payment`:
```python
def _payment_card_allowed_owner_ids(db, principal: BotPrincipal) -> set[Optional[int]]:
    if not principal.is_scoped:
        return {None} | {row.id for row in db.query(models.AdminUser.id).all()}  # امروز بدون تغییر - هر owner
    admin = db.get(models.AdminUser, principal.owner_admin_id)
    return hierarchy.owned_admin_ids(db, admin) if admin else set()   # بدون None - مطابق get_payment_info
```
یک principal اسکوپ‌شده هرگز کارت pool سراسری (`owner_admin_id IS NULL`) را نمی‌بیند/استفاده نمی‌کند - دقیقاً هم‌راستا با تصمیم عمدی `get_payment_info` که برای چنین tenantای کارت سراسری را fallback نمی‌کند تا پول به حساب اشتباه نرود. principal بدون‌مالک (shared/global/legacy) دقیقاً رفتار امروز را حفظ می‌کند.

## ۵. Shadow evaluation - ایزوله‌ی کامل از مسیر واقعی + پوشش کامل توابع

```python
def _shadow_would_allow(principal: BotPrincipal, real_check) -> Optional[bool]:
    """هرگز چیزی به مسیر واقعی raise نمی‌کند - هر Exceptionای (نه فقط
    HTTPException) اینجا catch و لاگ می‌شود؛ نتیجه‌ی نامعلوم None است، نه
    یک تصمیم قطعی. یک باگ در خودِ شادو هرگز درخواست واقعی سالم را نباید
    fail کند."""
    if principal.owner_admin_id is None or principal.scope_enforced:
        return None   # چیزی برای شبیه‌سازی نیست (بدون owner) یا از قبل enforced (شادو بی‌معنی)
    try:
        shadow = dataclasses.replace(principal, scope_enforced=True)
        real_check(shadow)
        return True
    except HTTPException:
        return False
    except Exception:
        logger.exception("bot_auth shadow: خطای داخلی - نتیجه نامعلوم ثبت شد")
        return None
```
این تابع در انتهای **هر پنج** تابع مرکزی صدا زده می‌شود (نه ۴ - `require_bot_resource_owner_access` هم اضافه شد)، به‌علاوه‌ی بررسی هم‌ارز hierarchy در `admin-by-telegram`/`admin-username` (بخش ۲ بالا). نتیجه فقط لاگ می‌شود؛ خودِ فراخوانی `_shadow_would_allow` هم در یک `try/except Exception: pass` گسترده‌تر در سطح caller قرار می‌گیرد (دو لایه ایزولاسیون، نه یک لایه).

## ۶. Provisioning - محل دقیق، implementation-ready

**یک choke-point واحد، نه دو:** `services/user_ops.py`'s `provision_connection` (خط ۱۵۳۰) توسط `provision_package_connections` (۱۱۹۳)، `apply_package_as_purchase` (۱۳۲۶)، و مسیر bulk-create (نزدیک خط ۷۳۴) **هر سه** صدا زده می‌شود - و این تابع از قبل `user` (که `user.owner_admin_id` رویش ست شده) و `node` را در اختیار دارد، پس نیازی به پارامتر جدید یا context اضافه نیست:

```python
# در ابتدای provision_connection، قبل از هر dispatch به provision_wireguard/... (یعنی قبل از هر اثر خارجی واقعی):
if user.owner_admin_id is not None:
    owner = db.get(models.AdminUser, user.owner_admin_id)
    allowed = hierarchy.selling_scope_node_ids(db, owner) if owner else set()
    if allowed is not None and node.id not in allowed:
        raise HTTPException(403, f"سرور «{node.name}» در اختیار این مجموعه نیست")
# user.owner_admin_id is None (کاربر orphan/ساخته‌شده توسط سوپرادمین): بدون تغییر، بی‌محدودیت
```
`hierarchy.selling_scope_node_ids(db, admin)` تابع تازه‌ای در **`services/hierarchy.py`** (نه `bot_auth.py` - چون هم کد بات هم این مسیر خالص وب/provisioning به آن نیاز دارند) که دقیقاً منطق فیکس‌شده‌ی `accessible_node_ids_for` (نسخه‌ی سوم، بخش ۳: resolve از طریق `parent_admin_scope_id` برای Seller، fail-closed به نودهای global‌only اگر والد سوپرادمین باشد) را در خودش دارد؛ `accessible_node_ids_for` در `bot_auth.py` بعد از این فقط یک wrapper نازک می‌شود:
```python
def accessible_node_ids_for(db, principal):
    if not principal.is_scoped:
        return None
    admin = db.get(models.AdminUser, principal.owner_admin_id)
    return hierarchy.selling_scope_node_ids(db, admin) if admin else set()
```
**همان تابع** برای `routers/packages.py:_sync_connections` (write-time، بدون نیاز به منطق Seller-translation چون سازنده‌ی پکیج هرگز Seller نیست) و برای خودِ `provision_connection` (defense-in-depth، runtime) استفاده می‌شود - یک منبع حقیقت، نه دو کپی. هیچ نگرانی rollback/تراکنش جدید مطرح نیست چون این بررسی **قبل از هر فراخوانی SSH/RouterOS/3X-UI واقعی** رخ می‌دهد (اولین خط `provision_connection`، قبل از dispatch)، نه بعد از آن.

## ۷. اصلاح عملیات Activation (بخش ۱ نسخه‌ی سوم) - فایل‌ها و شرایط دقیق

`routers/api_keys.py` یک endpoint تازه می‌گیرد:
```python
@router.post("/{key_id}/activate", dependencies=[Depends(require_confirm_password)])
def activate_tenant_key(key_id: int, db: Session = Depends(get_db)):
    result = db.execute(
        update(models.ApiKey.__table__)
        .where(models.ApiKey.id == key_id, models.ApiKey.key_type == KeyType.TENANT_INTEGRATION,
               models.ApiKey.enabled == False)   # noqa: E712 - پیش‌شرط، نه فقط توصیف
        .values(enabled=True, scope_enforced=True)
    )
    db.commit()
    if result.rowcount == 0:
        raise HTTPException(409, "کلید در وضعیت قابل‌فعال‌سازی نیست")
    return {"ok": True}
```
یک UPDATE، یک شرط `rowcount`، همان `require_confirm_password` که `create_global_key`/`delete_key` هم استفاده می‌کنند - هیچ لحظه‌ی میانی `enabled=true, scope_enforced=false` روی DB. تست lifecycle (`test_api_key_phase_b.py` یا فایل تازه) و تغییر UI (دکمه‌ی «فعال‌سازی» در `Settings.jsx` برای کلید tenant، به‌جای پیام «در انتظار Phase C») به فهرست فایل‌های بخش ۹ اضافه می‌شوند.

**شفاف‌سازی صادقانه‌ی C0 برای HTTP:** چون کلید tenant تا فعال‌سازی `enabled=false` می‌ماند (اصلاً به `get_bot_api_key` نمی‌رسد) و بعد از فعال‌سازی طبق invariant بخش ۱ نسخه‌ی سوم `scope_enforced=true` اجباری است، **هیچ کلید HTTPای در C0 وجود ندارد که principal معتبر+بدون‌مالک+دارای owner باشد** - یعنی shadow telemetry بخش ۵ در عمل، تا قبل از C1 (اولین کلید تست واقعی)، **فقط برای بات‌های in-process اختصاصی داده تولید می‌کند، نه برای هیچ کلید HTTP**. این یک محدودیت واقعی است، نه یک باگ طراحی - در گزارش هر مرحله باید صریح گفته شود، نه به‌عنوان «observation کامل از هر دو مسیر» جا زده شود.

## ۸. فهرست نهایی فایل‌ها (canonical - جایگزین فهرست نسخه‌ی سوم)

| # | فایل | تغییر |
|---|---|---|
| ۱ | `backend/app/services/hierarchy.py` | تابع تازه `selling_scope_node_ids(db, admin)` (بخش ۶) |
| ۲ | `backend/app/services/bot_auth.py` | invariant `TENANT_INTEGRATION`+`scope_enforced=false`→invalid؛ `resolve_claimed_owner` با `db`+hierarchy؛ `require_bot_resource_owner_access`؛ `accessible_node_ids_for` (wrapper نازک روی `hierarchy.selling_scope_node_ids`)؛ `_shadow_would_allow` + فراخوانی در هر ۵ تابع؛ `ROUTE_POLICIES`+`bot_route_policy` decorator (بخش ۳)؛ `_payment_card_allowed_owner_ids` (بخش ۴) |
| ۳ | `backend/app/services/bot_files.py` (تازه) | `get_package_files`/`get_tutorial_media`/`get_tutorial_software_file`/`get_admin_telegram_id`/`get_admin_username` - principal-aware، مشترک بین HTTP و in-process (بخش ۲) |
| ۴ | `backend/app/services/user_ops.py` | بررسی دسترسی نود در ابتدای `provision_connection` (بخش ۶) |
| ۵ | `backend/app/routers/packages.py` | بررسی `hierarchy.selling_scope_node_ids` در `_sync_connections` |
| ۶ | `backend/app/deps.py` | `get_bot_principal` |
| ۷ | `backend/app/routers/bot.py` | `@bot_route_policy` روی هر ۳۸ endpoint (نه ۳۵)؛ پارامتر `principal`؛ ۵ endpoint دانلود/lookup به `bot_files.py` وصل می‌شوند |
| ۸ | `backend/app/routers/api_keys.py` | endpoint تازه‌ی `activate_tenant_key` (بخش ۷) |
| ۹ | `backend/app/telegram_bot/panel_bridge.py` | `_scope()` **حفظ می‌شود**؛ هر متد یک `BotPrincipal.internal(...)` هم می‌سازد و به `_do_*`/`bot_files.*` پاس می‌دهد؛ ۵ متد بخش ۲ به `bot_files.py` وصل می‌شوند |
| ۱۰ | `backend/app/models.py` | `AdminUser.dedicated_bot_scope_enforced` (additive) |
| ۱۱ | `frontend/src/pages/Settings.jsx` | دکمه‌ی «فعال‌سازی» برای کلید tenant (بخش ۷) |
| ۱۲ | `backend/tests/test_bot_route_policy_coverage.py` (تازه) | assert روی خودِ function object ها، نه رشته (بخش ۳) |
| ۱۳ | `backend/tests/test_api_key_phase_b.py` یا فایل تازه | lifecycle کامل `activate_tenant_key` |
| ۱۴ | `backend/tests/test_bot_auth_principal.py` | بازنویسی کامل مطابق بخش‌های ۱-۶ |
| ۱۵ | `backend/tests/test_bot_inprocess_trust_characterization.py` | نسخه‌ی معکوس بعد از C3 + تست تازه‌ی «`_scope()` در C0 هنوز پیش‌فرض را می‌گذارد» |
| ۱۶ | مستند این فایل | گزارش بعد از هر مرحله |

هیچ‌کدام از ۱۶ مورد اعمال نشده - صفر کد production، صفر commit/push در این دور هم. رمز مادر لمس نشد.

### وضعیت

اجرای C0 همچنان متوقف است تا همین نسخه‌ی چهارم (canonical) بازبینی/تایید شود.

**اصلاحیه ۲۰۲۶-۰۹-۲۸ (بازبینی پنجم Product): نسخه‌ی چهارم منسوخ شد.** ۴ نقص مسدودکننده + ۲ نقص اجرایی + ۱ ادعای بیش‌ازحد کلی پیدا شد. **از این‌جا به بعد، بخش «Phase C - نسخه‌ی پنجم» پایین‌تر تنها مرجع اجراست.**

---

# Phase C - نسخه‌ی پنجم (canonical - تنها مرجع اجرا، جایگزین نسخه‌ی چهارم)

هر ۶ مورد با خواندن مستقیم `routers/users.py` تایید شد:

1. **مسیر global قبل از authorization crash می‌کرد** - در مثال `bot_files.py` نسخه‌ی چهارم، `hierarchy.accessible_package_owner_ids(_admin_for(db, principal))` **قبل از** فراخوانی `require_bot_resource_owner_access` و به‌صورت eager ارزیابی می‌شد؛ برای principal بدون‌مالک (`owner_admin_id=None` - shared/legacy/global، **رایج‌ترین حالت**)، `_admin_for` چیزی برنمی‌گرداند و `hierarchy.accessible_package_owner_ids(None)` روی `role(None)` کرش می‌کند.
2. **Decorator فقط capability را غیرقابل‌فراموشی می‌کرد، نه resource check** - `@bot_route_policy(capability=X)` بدون هیچ اجباری برای `require_bot_user_access`/... در بدنه، هنوز اجازه می‌داد دقیقاً همان کلاس باگ اصلی (`add_balance`/`link_telegram` فراموش‌شده) برگردد.
3. **Shadow کارت پرداخت مقدار غلط ثبت می‌کرد** - چون `allowed_owner_ids` یک‌بار، خارج از تابع بررسی و بر اساس principal اصلی (unscoped در C0) محاسبه می‌شد، shadow (که principal را موقتاً scoped می‌کند) همان مجموعه‌ی «همه چیز» را دوباره استفاده می‌کرد و برای هر کارت (حتی کارت تنانت دیگر) `would_allow=true` ثبت می‌کرد.
4. **Scope زمان provisioning با scope زمان ساخت پکیج یکی نیست** - `provision_connection`ی نسخه‌ی چهارم فقط `user.owner_admin_id` (تنانت مشتری) را می‌بیند. برای یک پکیج global (owner_admin_id=NULL، ساخته‌ی سوپرادمین که در `_sync_connections` امروزی هر نودی مجاز است) وقتی مشتری یک Seller-زیر-سوپرادمین آن را می‌خرد، این چک فقط نودهای global را مجاز می‌داند - یعنی خریدِ **همان پکیج ثابت** برای این مشتری رد می‌شود در حالی که برای مشتری دیگری (owner=سوپرادمین مستقیم) قبول می‌شود. مرجع authorization باید کسی باشد که node را برای همین connection مجاز کرده (سازنده‌ی پکیج/actor لحظه‌ی خرید)، نه صاحب مشتری.
5. **کلید‌گذاری registry اشتباه بود** - FastAPI روی `APIRoute.endpoint`، تابع `wrapper` (خروجی decorator) را ثبت می‌کند، نه `fn` اصلی؛ `ROUTE_POLICIES[fn]=...` هرگز با `route.endpoint` یکی نمی‌شود.
6. **فهرست فایل‌ها ناقص بود** - تایید شد `Settings.jsx` امروز `toggleApiKey` را از `client.js:264` می‌گیرد؛ `activateApiKey` باید کنارش اضافه شود، و هر متن تازه‌ی UI باید کلید fa/en در `translations.js` بگیرد. همچنین ادعای «`provision_connection` تنها choke-point همه‌ی provisioning» غلط بود - تایید شد `routers/users.py:889,900,959` مستقیم `provision_wireguard`/`provision_openvpn`/`provision_xray` را صدا می‌زند، با یک بررسی *جدا و از قبل صحیح* (`_get_user_and_node`, خط ۸۶۰-۸۷۶: دسترسی **caller** نه owner هدف) - این مسیر لمس نمی‌شود، فقط ادعای «تنها نقطه» در سند تصحیح می‌شود.

## ۱. `require_bot_resource_owner_access` - محاسبه‌ی تنبل (lazy)، نه eager

```python
def require_bot_resource_owner_access(
    principal: BotPrincipal, resource_owner_admin_id: Optional[int],
    allowed_owner_ids_fn: Callable[[BotPrincipal], set[Optional[int]]], *, endpoint: Optional[str] = None,
) -> None:
    """allowed_owner_ids_fn فقط وقتی is_scoped=True باشد صدا زده می‌شود -
    برای principal بدون‌مالک (شایع‌ترین حالت: shared/legacy/global/C0)
    هرگز اجرا نمی‌شود، پس هیچ‌وقت با owner_admin_id=None کرش نمی‌کند.
    caller باید یک تابع (نه یک مقدار حاضر) بدهد - همین یک قاعده، هم
    کرش نقص ۱ و هم نتیجه‌ی غلط shadow نقص ۳ را با یک فیکس می‌بندد،
    چون shadow دقیقاً همین تابع را با principal.scope_enforced=True
    دوباره صدا می‌زند و این‌بار چون is_scoped=True شده، تابع واقعاً
    اجرا و به مجموعه‌ی درست می‌رسد - نه یک مقدار stale از principal اصلی."""
    _ensure_valid(principal, "require_bot_resource_owner_access", endpoint=endpoint)
    if not principal.is_scoped:
        _log_decision(principal, "require_bot_resource_owner_access", allowed=True, reason="unscoped", endpoint=endpoint)
        return
    allowed = resource_owner_admin_id in allowed_owner_ids_fn(principal)
    _log_decision(principal, "require_bot_resource_owner_access", allowed=allowed, reason="resource policy", endpoint=endpoint)
    if not allowed:
        raise HTTPException(404, "موردی یافت نشد")
```

هر تابع `allowed_owner_ids_fn` (برای Package/Tutorial/PaymentCard) هم باید خودش روی `principal.owner_admin_id` شاخه بزند، نه `principal.is_scoped`:
```python
def _payment_card_allowed_owner_ids(db):
    def _fn(principal: BotPrincipal) -> set[Optional[int]]:
        # اینجا principal همیشه owner دارد (چون require_bot_resource_owner_access
        # فقط وقتی is_scoped=True این تابع را صدا می‌زند، و is_scoped یعنی
        # owner_admin_id هم قطعاً set است) - پس db.get همیشه امن است، هم برای
        # فراخوانی واقعی هم برای فراخوانی shadow با principal کپی‌شده.
        admin = db.get(models.AdminUser, principal.owner_admin_id)
        return hierarchy.owned_admin_ids(db, admin) if admin else set()
    return _fn
```
کاربرد: `require_bot_resource_owner_access(principal, card.owner_admin_id, _payment_card_allowed_owner_ids(db))`. `bot_files.get_package_files` (بخش ۲ نسخه‌ی چهارم) هم به همین شکل اصلاح می‌شود: `lambda p: hierarchy.accessible_package_owner_ids(db.get(models.AdminUser, p.owner_admin_id))`.

## ۲. Resource check غیرقابل‌فراموشی - accessor متمرکز، نه decorator تنها

Decorator بخش ۳ نسخه‌ی چهارم (`@bot_route_policy`) فقط capability را می‌پوشاند - برای resource check، **راه واقعی جلوگیری از فراموشی این است که راه دیگری برای گرفتن آن شیء وجود نداشته باشد**، نه صرفاً «endpoint باید یادش بماند تابع را صدا بزند»:

```text
- هر ۵ نوع resource (User, Package, Tutorial, PaymentCard, Node-list) دقیقاً
  یک accessor principal-aware می‌گیرد که خودش داخلش require_bot_*_access را
  صدا می‌زند و شیء را برمی‌گرداند یا 404/403 می‌دهد - نه اینکه endpoint اول
  db.get کند و بعد (شاید) بررسی را صدا بزند:
    _get_user_or_403(db, principal, username)          -> models.User
    _get_package_or_403(db, principal, package_id)      -> models.Package
    _get_tutorial_or_403(db, principal, tutorial_id)     -> models.Tutorial
    _get_payment_card_or_403(db, principal, card_id)     -> models.PaymentCard
    _scoped_node_ids(db, principal)                      -> Optional[set[int]]  (فیلتر لیست)
- تست استاتیک: grep/AST روی routers/bot.py تایید می‌کند هیچ‌جای دیگر (بیرون
  از همین ۵ تابع) db.query(models.User)/db.get(models.User, ...) و معادلش
  برای Package/Tutorial/PaymentCard وجود ندارد - یعنی راه دیگری برای رسیدن
  به آن شیء از اول نیست، نه فقط «قرار است این تابع صدا زده شود».
```

**RoutePolicy حالا دو فیلد اجباری دارد، نه یک:**
```python
@dataclass(frozen=True)
class RoutePolicy:
    capability: Optional[str]              # None = فقط _ensure_valid
    resource_strategy: str                 # "none" صریح، نه غیاب فیلد - هیچ مقدار پیش‌فرض ضمنی
```
تست پوشش (بخش ۳) هر دو فیلد را - نه فقط وجود marker را - در برابر جدول کامل بخش ۷ نسخه‌ی چهارم مقایسه می‌کند.

**صداقت درباره‌ی قدرت این تضمین:** capability را decorator در **runtime** برای هر درخواست enforce می‌کند (تضمین قوی). resource check را accessor متمرکز + تست استاتیک grep می‌پوشاند (تضمین «راه دیگری برای گرفتن شیء نیست» + «کد امروز این قاعده را نقض نمی‌کند») - این ضعیف‌تر از یک runtime gate است (یک توسعه‌دهنده‌ی مصمم می‌تواند تئوریاً دور بزند)، ولی به‌مراتب قوی‌تر از «قرارداد نوشته‌شده در کامنت» است. این تفاوت باید در گزارش صریح بماند، نه یک‌سان با capability جا زده شود.

## ۳. کلید‌گذاری صحیح decorator (روی `wrapper`، نه `fn`)

```python
def bot_route_policy(capability: Optional[str], resource_strategy: str):
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, principal: BotPrincipal, **kwargs):
            if capability is None:
                _ensure_valid(principal, fn.__name__)
            else:
                require_bot_capability(principal, capability, endpoint=fn.__name__)
            return fn(*args, principal=principal, **kwargs)
        wrapper._bot_capability = capability          # روی wrapper - همان چیزی که FastAPI به‌عنوان route.endpoint ثبت می‌کند
        wrapper._bot_resource_strategy = resource_strategy
        return wrapper
    return decorator
```
```python
# tests/test_bot_route_policy_coverage.py
routes = [r for r in router.routes if isinstance(r, APIRoute)]
assert len(routes) == 38
for r in routes:
    assert hasattr(r.endpoint, "_bot_capability"), f"{r.path} فاقد @bot_route_policy است"
    assert r.endpoint._bot_resource_strategy != "", f"{r.path} فاقد resource_strategy صریح است"
    # به‌علاوه: مقایسه‌ی دقیق (method, path) -> (capability, resource_strategy) با جدول canonical بخش ۷ نسخه‌ی چهارم
```

## ۴. Provisioning - authorization_scope به‌جای owner مشتری

**اصل تازه:** مرجع مجازبودن یک node برای یک connection، **کسی است که آن node را برای همین connection انتخاب/تایید کرده** - نه صاحب مشتری. این دقیقاً همان اصلی است که `routers/users.py:_get_user_and_node` (خط ۸۶۰-۸۷۶) از قبل به‌درستی پیاده می‌کند: دسترسی **caller** (`admin` از `get_current_admin`) را چک می‌کند، نه owner کاربر هدف. `provision_connection` باید همین اصل را بگیرد، نه اصل متفاوتی:

```python
def provision_connection(
    db: Session, user: models.User, node: models.Node, protocol, *,
    authorization_scope_admin_id: Optional[int] = ...,   # پارامتر تازه
    ...
) -> models.Connection:
    if authorization_scope_admin_id is not None:
        admin = db.get(models.AdminUser, authorization_scope_admin_id)
        allowed = hierarchy.selling_scope_node_ids(db, admin) if admin else set()
        if allowed is not None and node.id not in allowed:
            raise HTTPException(403, f"سرور «{node.name}» در اختیار این مجموعه نیست")
```

**مقدار `authorization_scope_admin_id` per call site (نه یک پیش‌فرض واحد):**
- `provision_package_connections`/`apply_package_as_purchase` (اتصال bundled از `PackageConnection`): **`package.owner_admin_id`** - همان مرجعی که `_sync_connections` زمان ساخت پکیج قبلاً تاییدش کرده؛ یعنی یک پکیج global (owner=NULL) همیشه با همان قاعده‌ی نامحدودِ زمان ساخت provision می‌شود، مستقل از owner مشتری خریدار - نقص ۴ دقیقاً همین‌جا حل می‌شود، چون دیگر بسته به خریدار فرق نمی‌کند.
- مسیر bulk-create/دستی (نزدیک خط ۷۳۴ `user_ops.py`، و مسیر بات `add_connection` با `node_id` صریح): **owner همان actor لحظه‌ی خرید/ساخت** - `principal.owner_admin_id` برای بات، یا `admin.id` برای پنل وب (اگر این مسیر از `routers/users.py` صدا زده شود؛ فعلاً نمی‌شود - `routers/users.py` خودش مسیر جدا `_get_user_and_node` دارد و دست‌نخورده می‌ماند).
- `authorization_scope_admin_id=None` (پکیج/actor سوپرادمین): بدون تغییر، بی‌محدودیت - رفتار امروز سوپرادمین.

**اصلاح ادعای دامنه‌ی `provision_connection`:** این تابع choke-point هر سه مسیر **مبتنی‌بر پکیج/بات** است (`provision_package_connections`, `apply_package_as_purchase`, بخش bulk-create), **نه تمام provisioning پروژه**. `routers/users.py`'s سه endpoint دستی (`connections/wireguard`, `/openvpn`, و مشابه، خط ۸۷۸ به بعد) مستقیم توابع protocol-specific را صدا می‌زنند و از قبل بررسی خودشان (`_get_user_and_node`) را دارند که **صحیح است و دست‌نخورده می‌ماند** - این مسیر جزو محدوده‌ی این Phase نیست، فقط ادعای «همه‌جا از یک نقطه» در سند حذف شد.

## ۵. تکمیل فهرست فایل‌ها

علاوه بر ۱۶ ردیف نسخه‌ی چهارم، این موارد اضافه/اصلاح می‌شوند:

| # | فایل | تغییر |
|---|---|---|
| ۱۷ | `frontend/src/api/client.js` | `activateApiKey(id)` تازه، کنار `toggleApiKey` (خط ۲۶۴) - `POST /api/api-keys/{id}/activate` با هدر `X-Confirm-Password` |
| ۱۸ | `frontend/src/i18n/translations.js` | کلیدهای fa/en تازه برای دکمه‌ی «فعال‌سازی» و پیام‌های تایید رمز مرتبط با آن (دقیقاً کدام کلید - جزو ریزطراحی پیاده‌سازی UI، نه این سند) |
| ۱۹ | `backend/app/services/user_ops.py` | `provision_connection` با پارامتر `authorization_scope_admin_id` (بخش ۴)؛ ۳ call site (خط ~۱۲۱۴, ~۱۴۰۲, ~۷۳۴) هرکدام مقدار خودشان را می‌فرستند |
| ۲۰ | `backend/app/services/bot_auth.py` | ۵ تابع accessor متمرکز (بخش ۲)؛ امضای `require_bot_resource_owner_access` با `allowed_owner_ids_fn` (بخش ۱)؛ `RoutePolicy` با فیلد اجباری `resource_strategy` (بخش ۲/۳)؛ `bot_route_policy` با attribute روی `wrapper` (بخش ۳) |
| ۲۱ | `backend/tests/test_bot_route_policy_coverage.py` | grep/AST استاتیک برای «۵ accessor تنها راه» (بخش ۲) + مقایسه‌ی دقیق capability+resource_strategy (بخش ۳) |

ردیف ۳ نسخه‌ی چهارم (`services/bot_files.py`) و ردیف ۴ (`user_ops.py`ی provisioning) با همین اصلاحات (لazy fn، authorization_scope) به‌روزرسانی می‌شوند - فایل جدید اضافه نمی‌شود، محتوایشان اصلاح می‌شود.

هیچ‌کدام اعمال نشده - صفر کد production، صفر commit/push. رمز مادر لمس نشد.

### وضعیت

اجرای C0 همچنان متوقف است تا همین نسخه‌ی پنجم (canonical) بازبینی/تایید شود.

**اصلاحیه ۲۰۲۶-۰۹-۲۸ (بازبینی ششم Product): نسخه‌ی پنجم منسوخ شد.** ۳ نقص (۲ امنیتی/سازگاری + شادو ناقص) + ۱ نقص اجرایی (accessor family) + ۱ نکته‌ی مهم روش‌شناختی («هنوز canonical مستقل نیست - ارجاع به نسخه‌های قبل»). **پاسخ به نکته‌ی آخر: از این‌جا به بعد، بخش «Phase C - نسخه‌ی ششم» یک سند کاملاً خودکفا و بدون هیچ ارجاعی به نسخه‌ی اول تا پنجم است - همه‌چیز (جدول ۳۸ endpoint، pseudocode کامل هر تابع، rollout، فهرست فایل) عیناً همین‌جا نوشته شده.**

---

# Phase C - نسخه‌ی ششم (canonical، کامل و مستقل - تنها مرجع اجرا)

## ۱. مدل پایه (بدون تغییر نسبت به `bot_auth.py`ی موجود، فقط یک invariant اضافه)

`KeyType`, ۹ capability (`CUSTOMER_READ/WRITE`, `IDENTITY_WRITE`, `WALLET_WRITE`, `PAYMENT_READ/WRITE`, `FILES_READ`, `BROADCAST`, `ADMIN_LOOKUP`), `BotPrincipal` dataclass (`key_id, key_type, owner_admin_id, capabilities, label, is_internal, scope_enforced, valid`) و `.is_scoped = valid AND owner_admin_id is not None AND scope_enforced` - همه عیناً همان‌طور که در `bot_auth.py` نوشته و با ۶۲ assertion تست شده‌اند، **به‌جز یک اصلاح در `from_api_key`**:
```python
if key_type == KeyType.TENANT_INTEGRATION and not scope_enforced:
    return cls._invalid(key.id, key.label)   # یک کلید tenant با enabled=true, scope_enforced=false
                                              # هرگز unscoped نمی‌شود؛ فقط enforced یا نامعتبر
```

## ۲. `NodeAuthorizationScope` - نوع بسته، بدون `None` شناور

```python
@dataclass(frozen=True)
class NodeAuthorizationScope:
    """تنها دو شکل مجاز - نه یک Optional[int] خام. ساخته‌شدن unrestricted
    فقط از طریق دو تابع resolver پایین (هرگز با نوشتن None توسط یک caller
    دلبخواه) - یعنی یک باگ/فراموشی در آینده به‌جای «بی‌محدودیت تصادفی»،
    یک TypeError صریح می‌دهد (چون provision_connection پارامترش را
    NodeAuthorizationScope تایپ کرده، نه Optional[int])."""
    admin_id: Optional[int]
    unrestricted: bool

    @classmethod
    def unrestricted_scope(cls) -> "NodeAuthorizationScope":
        return cls(admin_id=None, unrestricted=True)

    @classmethod
    def scoped(cls, admin_id: int) -> "NodeAuthorizationScope":
        if admin_id is None:
            raise ValueError("scoped() یک admin_id واقعی می‌خواهد")
        return cls(admin_id=admin_id, unrestricted=False)

def resolve_package_authorization_scope(package: models.Package) -> NodeAuthorizationScope:
    """مرجع bundled connections - مستقل از principal/rollout، همیشه فعال
    (چون مبنایش authorization زمان ساخت پکیج است، نه bot scope)."""
    if package.owner_admin_id is None:
        return NodeAuthorizationScope.unrestricted_scope()
    return NodeAuthorizationScope.scoped(package.owner_admin_id)

def resolve_bot_authorization_scope(principal: BotPrincipal) -> NodeAuthorizationScope:
    """مرجع مسیر دستی/pick_node بات - is_scoped را خودش داخلش چک می‌کند،
    پس C0 (scope_enforced=false همه‌جا) همیشه unrestricted می‌گیرد بدون
    اینکه caller مجبور باشد جدا این را یادش بماند."""
    if not principal.is_scoped:
        return NodeAuthorizationScope.unrestricted_scope()
    return NodeAuthorizationScope.scoped(principal.owner_admin_id)

def resolve_superadmin_actor_scope(admin: models.AdminUser) -> NodeAuthorizationScope:
    """فقط از جایی صدا زده می‌شود که admin از قبل با require_superadmin
    تایید شده - نه یک مسیر عمومی."""
    return NodeAuthorizationScope.unrestricted_scope()
```

## ۳. Shadow evaluation - یک مکانیزم واحد برای هر تصمیم

```python
def _shadow_would_allow(principal: BotPrincipal, decide: Callable[[BotPrincipal], bool]) -> Optional[bool]:
    """decide یک تابع خالص است که True/False برمی‌گرداند (raise نمی‌کند) -
    هر ۵ تابع مرکزی پایین دقیقاً با همین یک شکل، منطق تصمیم خودشان را به
    اینجا می‌دهند؛ هیچ نسخه‌ی جدا/بسپوک برای هرکدام نیست. principal
    بدون owner یا از قبل enforced: چیزی برای شبیه‌سازی نیست."""
    if principal.owner_admin_id is None or principal.scope_enforced:
        return None
    try:
        shadow = dataclasses.replace(principal, scope_enforced=True)
        return bool(decide(shadow))
    except Exception:
        logger.exception("bot_auth shadow: خطای داخلی - نتیجه نامعلوم ثبت شد، مسیر واقعی اثر نگرفت")
        return None
```

## ۴. پنج تابع مرکزی - نسخه‌ی نهایی کامل

```python
def resolve_claimed_owner(db: Session, principal: BotPrincipal, claimed_owner_id: Optional[int], *, endpoint=None) -> Optional[int]:
    _ensure_valid(principal, "resolve_claimed_owner", endpoint=endpoint)
    def _decide(p: BotPrincipal) -> bool:
        admin = db.get(models.AdminUser, p.owner_admin_id)
        owned = hierarchy.owned_admin_ids(db, admin) if admin else set()
        return claimed_owner_id is None or claimed_owner_id in owned
    if not principal.is_scoped:
        would_allow = _shadow_would_allow(principal, _decide)
        _log_decision(principal, "resolve_claimed_owner", allowed=True, reason="unscoped",
                      claimed_owner_id=claimed_owner_id, endpoint=endpoint, extra={"would_allow_if_enforced": would_allow})
        return claimed_owner_id
    if not _decide(principal):
        _log_decision(principal, "resolve_claimed_owner", allowed=False, reason="claim خارج از owned_admin_ids",
                      claimed_owner_id=claimed_owner_id, endpoint=endpoint)
        raise HTTPException(403, _DENIED_MESSAGE)
    _log_decision(principal, "resolve_claimed_owner", allowed=True, reason="own tenant/subtree",
                  claimed_owner_id=claimed_owner_id, endpoint=endpoint)
    return claimed_owner_id if claimed_owner_id is not None else principal.owner_admin_id


def require_bot_capability(principal: BotPrincipal, capability: str, *, endpoint=None) -> None:
    _ensure_valid(principal, "require_bot_capability", endpoint=endpoint)
    allowed = capability in principal.capabilities
    _log_decision(principal, "require_bot_capability", allowed=allowed, reason=capability, endpoint=endpoint)
    if not allowed:
        raise HTTPException(403, _DENIED_MESSAGE)
    # نکته: capability مستقل از scope_enforced است (نقص rollback همان
    # دور قبل) - پس اینجا shadow معنا ندارد، چیزی «اگر enforced بود» فرق
    # نمی‌کرد چون این تابع از اول به scope کاری ندارد.


def require_bot_user_access(db: Session, principal: BotPrincipal, user: models.User, *, endpoint=None) -> None:
    _ensure_valid(principal, "require_bot_user_access", endpoint=endpoint)
    def _decide(p: BotPrincipal) -> bool:
        admin = db.get(models.AdminUser, p.owner_admin_id)
        return admin is not None and hierarchy.can_see_user(admin, hierarchy.owned_admin_ids(db, admin), user.owner_admin_id)
    if not principal.is_scoped:
        would_allow = _shadow_would_allow(principal, _decide)
        _log_decision(principal, "require_bot_user_access", allowed=True, reason="unscoped",
                      endpoint=endpoint, extra={"would_allow_if_enforced": would_allow})
        return
    allowed = _decide(principal)
    _log_decision(principal, "require_bot_user_access", allowed=allowed, reason="hierarchy check", endpoint=endpoint)
    if not allowed:
        raise HTTPException(404, "کاربر پیدا نشد")


def require_bot_resource_owner_access(
    principal: BotPrincipal, resource_owner_admin_id: Optional[int],
    allowed_owner_ids_fn: Callable[[BotPrincipal], set[Optional[int]]], *, endpoint=None,
) -> None:
    """allowed_owner_ids_fn فقط وقتی واقعاً لازم باشد (مسیر واقعی اسکوپ‌شده،
    یا مسیر shadow) صدا زده می‌شود - هرگز eager، پس با owner_admin_id=None
    کرش نمی‌کند."""
    _ensure_valid(principal, "require_bot_resource_owner_access", endpoint=endpoint)
    def _decide(p: BotPrincipal) -> bool:
        return resource_owner_admin_id in allowed_owner_ids_fn(p)
    if not principal.is_scoped:
        would_allow = _shadow_would_allow(principal, _decide)
        _log_decision(principal, "require_bot_resource_owner_access", allowed=True, reason="unscoped",
                      endpoint=endpoint, extra={"would_allow_if_enforced": would_allow})
        return
    allowed = _decide(principal)
    _log_decision(principal, "require_bot_resource_owner_access", allowed=allowed, reason="resource policy", endpoint=endpoint)
    if not allowed:
        raise HTTPException(404, "موردی یافت نشد")


def require_bot_node_access(db: Session, principal: BotPrincipal, node: models.Node, *, endpoint=None) -> None:
    _ensure_valid(principal, "require_bot_node_access", endpoint=endpoint)
    def _decide(p: BotPrincipal) -> bool:
        admin = db.get(models.AdminUser, p.owner_admin_id)
        allowed_ids = hierarchy.selling_scope_node_ids(db, admin) if admin else set()
        return allowed_ids is None or node.id in allowed_ids
    if not principal.is_scoped:
        would_allow = _shadow_would_allow(principal, _decide)
        _log_decision(principal, "require_bot_node_access", allowed=True, reason="unscoped",
                      endpoint=endpoint, extra={"would_allow_if_enforced": would_allow})
        return
    allowed = _decide(principal)
    _log_decision(principal, "require_bot_node_access", allowed=allowed, reason="hierarchy check", endpoint=endpoint)
    if not allowed:
        raise HTTPException(404, "نود پیدا نشد")
```

هر ۵ تابع دقیقاً یک شکل دارند: `_ensure_valid` اول، یک `_decide(p)` محلی خالص، سپس شاخه‌ی `not is_scoped` (real=allow, shadow=log) در برابر شاخه‌ی `is_scoped` (real=enforce). `require_bot_capability` تنها استثناست چون مستقل از scope است (توضیح داخل کدش).

**بررسی hierarchy برای `admin-by-telegram`/`admin-username`** از همین `require_bot_resource_owner_access` استفاده می‌کند (`allowed_owner_ids_fn = lambda p: hierarchy.owned_admin_ids(db, db.get(models.AdminUser, p.owner_admin_id)) | {p.owner_admin_id}`) - تابع جدا نیست، shadow را مجانی می‌گیرد.

## ۵. خانواده‌های accessor بسته (کامل - این‌ها کل مجموعه‌اند)

```text
User:        _get_user_or_403(db, principal, username) -> User
             _get_user_by_telegram_or_403(db, principal, telegram_id) -> Optional[User]
             _list_users_query(db, principal, search) -> Query[User]
Package:     _get_package_or_403(db, principal, package_id) -> Package
             _list_packages_query(db, principal) -> Query[Package]
Tutorial:    _get_tutorial_or_403(db, principal, tutorial_id) -> Tutorial
             _list_tutorials_query(db, principal) -> Query[Tutorial]
PaymentCard: _get_payment_card_or_403(db, principal, card_id) -> PaymentCard
             (بدون list - بات هیچ endpoint فهرست کارت ندارد)
Node:        _get_node_or_403(db, principal, node_id) -> Node
             _scoped_node_ids(db, principal) -> Optional[set[int]]
```
**۱۲ تابع، نه بیشتر.** تست AST (بخش ۷) تایید می‌کند در `routers/bot.py` هیچ `db.query(models.User|Package|Tutorial|PaymentCard|Node)` یا `db.get(models.User|Package|Tutorial|PaymentCard|Node, ...)`ای بیرون از همین ۱۲ تابع وجود ندارد - یک whitelist بسته و شمارش‌پذیر، نه استثناهای رشد‌کننده.

## ۶. Route-policy decorator (کلید‌گذاری صحیح روی `wrapper`)

```python
@dataclass(frozen=True)
class RoutePolicy:
    capability: Optional[str]   # None = فقط _ensure_valid
    resource_strategy: str      # همیشه صریح - هرگز غایب

def bot_route_policy(capability: Optional[str], resource_strategy: str):
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, principal: BotPrincipal, **kwargs):
            if capability is None:
                _ensure_valid(principal, fn.__name__)
            else:
                require_bot_capability(principal, capability, endpoint=fn.__name__)
            return fn(*args, principal=principal, **kwargs)
        wrapper._bot_capability = capability
        wrapper._bot_resource_strategy = resource_strategy
        return wrapper
    return decorator
```
```python
# tests/test_bot_route_policy_coverage.py
routes = [r for r in router.routes if isinstance(r, APIRoute)]
assert len(routes) == 38
CANONICAL = { ... }   # (method, path) -> (capability, resource_strategy) - عیناً از جدول بخش ۷ پایین
for r in routes:
    method = next(iter(r.methods - {"HEAD"}))
    assert hasattr(r.endpoint, "_bot_capability"), f"{method} {r.path} فاقد @bot_route_policy"
    got = (r.endpoint._bot_capability, r.endpoint._bot_resource_strategy)
    assert got == CANONICAL[(method, r.path)], f"{method} {r.path}: {got} != {CANONICAL[(method, r.path)]}"

# tests/test_bot_resource_accessor_ast.py - بخش ۵
import ast, inspect
ALLOWED_FUNCS = {"_get_user_or_403", "_get_user_by_telegram_or_403", "_list_users_query",
                 "_get_package_or_403", "_list_packages_query", "_get_tutorial_or_403",
                 "_list_tutorials_query", "_get_payment_card_or_403", "_get_node_or_403", "_scoped_node_ids"}
GATED_MODELS = {"User", "Package", "Tutorial", "PaymentCard", "Node"}
# پیمایش AST ماژول routers/bot.py: هر Call به db.query(models.X)/db.get(models.X, ...)
# که X در GATED_MODELS است، باید داخل بدنه‌ی یکی از ALLOWED_FUNCS باشد - وگرنه fail.
```

## ۷. جدول کامل و نهایی ۳۸ endpoint (canonical - این جدول را `CANONICAL` بالا عیناً کد می‌کند)

| # | Method | Path (نسبت به `/api/bot`) | Capability | resource_strategy |
|---|---|---|---|---|
| ۱ | GET | `/nodes` | *(None)* | `node_list` |
| ۲ | GET | `/packages` | `CUSTOMER_READ` | `package_list` |
| ۳ | GET | `/payment-info` | `PAYMENT_READ` | `claim` |
| ۴ | GET | `/payment-cards/{card_id}` | `PAYMENT_READ` | `payment_card_owner` |
| ۵ | POST | `/payment-cards/{card_id}/record-payment` | `PAYMENT_WRITE` | `payment_card_owner` |
| ۶ | GET | `/sales-stats` | `CUSTOMER_READ` | `claim` |
| ۷ | GET | `/customer-menu-config` | *(None)* | `none` |
| ۸ | GET | `/tutorials` | `CUSTOMER_READ` | `tutorial_list` |
| ۹ | GET | `/packages/{package_id}/files/{file_id}/download` | `FILES_READ` | `package_owner` |
| ۱۰ | GET | `/tutorials/{tutorial_id}/media/{media_id}/download` | `FILES_READ` | `tutorial_owner` |
| ۱۱ | GET | `/tutorials/{tutorial_id}/software/{software_id}/download` | `FILES_READ` | `tutorial_owner` |
| ۱۲ | GET | `/admin-by-telegram/{tg_id}` | `ADMIN_LOOKUP` | `admin_hierarchy` |
| ۱۳ | GET | `/admin-username/{admin_id}` | `ADMIN_LOOKUP` | `admin_hierarchy` |
| ۱۴ | GET | `/telegram-user-ids` | `BROADCAST` | `claim` |
| ۱۵ | POST | `/users` | `CUSTOMER_WRITE` | `claim` |
| ۱۶ | POST | `/users/{username}/purchase-package` | `CUSTOMER_WRITE` | `user` |
| ۱۷ | POST | `/referral/apply` | `CUSTOMER_WRITE` | `user` |
| ۱۸ | POST | `/discount/validate` | `CUSTOMER_WRITE` | `claim` |
| ۱۹ | POST | `/discount/redeem` | `CUSTOMER_WRITE` | `claim` |
| ۲۰ | GET | `/users` | `CUSTOMER_READ` | `user_list` |
| ۲۱ | GET | `/users/by-telegram/{telegram_id}` | `CUSTOMER_READ` | `claim` |
| ۲۲ | GET | `/users/by-telegram/{telegram_id}/all` | `CUSTOMER_READ` | `claim` |
| ۲۳ | GET | `/users/{username}` | `CUSTOMER_READ` | `user` |
| ۲۴ | POST | `/users/{username}/link-telegram` | `IDENTITY_WRITE` | `user` |
| ۲۵ | POST | `/users/{username}/connections` | `CUSTOMER_WRITE` | `user+node` |
| ۲۶ | GET | `/users/{username}/purchases` | `CUSTOMER_READ` | `user` |
| ۲۷ | POST | `/users/{username}/purchases/{purchase_id}/rename` | `CUSTOMER_WRITE` | `user` |
| ۲۸ | DELETE | `/users/{username}/purchases/{purchase_id}` | `CUSTOMER_WRITE` | `user` |
| ۲۹ | DELETE | `/users/{username}/connections/{connection_id}` | `CUSTOMER_WRITE` | `user` |
| ۳۰ | GET | `/users/{username}/subscription-link` | `CUSTOMER_READ` | `user` |
| ۳۱ | GET | `/miniapp-button-text` | *(None)* | `none` |
| ۳۲ | GET | `/panel-public-url` | *(None)* | `none` |
| ۳۳ | POST | `/users/{username}/purchases/{purchase_id}/renew` | `CUSTOMER_WRITE` | `user` |
| ۳۴ | POST | `/users/{username}/renew` | `CUSTOMER_WRITE` | `user` |
| ۳۵ | POST | `/users/{username}/reset-usage` | `CUSTOMER_WRITE` | `user` |
| ۳۶ | POST | `/users/{username}/set-enabled` | `CUSTOMER_WRITE` | `user` |
| ۳۷ | POST | `/users/{username}/add-balance` | `WALLET_WRITE` | `user` |
| ۳۸ | DELETE | `/users/{username}` | `CUSTOMER_WRITE` | `user` |

`resource_strategy` → کدام accessor/تابع:
- `claim` → `resolve_claimed_owner` روی پارامتر `owner_admin_id` موجود.
- `user` → `_get_user_or_403` (که خودش `require_bot_user_access` را داخلش صدا می‌زند).
- `user_list`/`package_list`/`tutorial_list` → `_list_*_query` (فیلتر، نه 403 تکی).
- `node_list` → `_scoped_node_ids`.
- `user+node` → `_get_user_or_403` + (اگر `node_id` در بدنه باشد) `_get_node_or_403`.
- `package_owner`/`tutorial_owner`/`payment_card_owner` → `_get_package_or_403`/`_get_tutorial_or_403`/`_get_payment_card_or_403` (هرکدام داخلش `require_bot_resource_owner_access` را با `allowed_owner_ids_fn` مخصوص خودش صدا می‌زند).
- `admin_hierarchy` → همان `require_bot_resource_owner_access` با `allowed_owner_ids_fn = owned_admin_ids ∪ {self}` (بخش ۴).
- `none` → هیچ‌کدام؛ فقط `_ensure_valid` (خودِ decorator).

## ۸. Provisioning - `authorization_scope` اجباری، بدون پیش‌فرض

```python
def provision_connection(
    db: Session, user: models.User, node: models.Node, protocol, *,
    authorization_scope: NodeAuthorizationScope,   # keyword-only, بدون هیچ default - عمداً
    flow: str = "", max_concurrent_sessions=1, purchase_batch=None, package_name=None, speed_limit_mbps=None,
) -> models.Connection:
    if not authorization_scope.unrestricted:
        admin = db.get(models.AdminUser, authorization_scope.admin_id)
        allowed = hierarchy.selling_scope_node_ids(db, admin) if admin else set()
        if allowed is not None and node.id not in allowed:
            raise HTTPException(403, f"سرور «{node.name}» در اختیار این مجموعه نیست")
    ... (dispatch امروزی به provision_wireguard/... بدون تغییر)
```
**Call siteها:**
- `provision_package_connections`/`apply_package_as_purchase` (bundled از `PackageConnection`): `authorization_scope=resolve_package_authorization_scope(package)` - **همیشه فعال، مستقل از principal/rollout** (چون مبنایش زمان ساخت پکیج است، نه bot scope؛ Product صریحاً این استقلال را خواسته).
- bulk-create/دستی بات (`node_id` صریح از پیک‌کردن مشتری): `authorization_scope=resolve_bot_authorization_scope(principal)` - این تابع خودش `is_scoped` را چک می‌کند، پس در C0 (همه‌جا `scope_enforced=false`) همیشه `unrestricted_scope()` می‌دهد و **رفتار زنده صفر تغییر می‌کند**؛ شادو تصمیم فرضی را جدا (با `_shadow_would_allow` روی همان منطق `hierarchy.selling_scope_node_ids`) لاگ می‌کند، بدون اثر روی connection واقعی.
- superadmin مستقیم (اگر جایی لازم شود): `resolve_superadmin_actor_scope(admin)`.

`hierarchy.selling_scope_node_ids(db, admin)` (تابع تازه در `services/hierarchy.py`) - resolve از طریق `parent_admin_scope_id` برای Seller، fail-closed به فقط نودهای `owner_admin_id IS NULL` اگر والد سوپرادمین باشد، fail-closed به `set()` برای هر شکل غیرمنتظره‌ی دیگر؛ `accessible_node_ids_for`/`_scoped_node_ids` در `bot_auth.py` هم wrapperهای نازک روی همین یک تابعند.

`routers/packages.py:_sync_connections` هم از همین `hierarchy.selling_scope_node_ids(db, admin)` (با `admin`ی که پکیج را می‌سازد - هرگز Seller، طبق مدل Package) استفاده می‌کند - یک منبع حقیقت.

**`routers/users.py`'s سه endpoint دستی connections (`/connections/wireguard` و مشابه، `_get_user_and_node`)** دست‌نخورده می‌مانند - از قبل با `hierarchy.accessible_node_ids(db, admin)` روی caller خودشان درست عمل می‌کنند؛ جزو این Phase نیستند.

## ۹. `panel_bridge.py` - wiring کامل

- **`_scope(owner_admin_id)` عیناً حفظ می‌شود** - همچنان تنها منبع پیش‌فرض‌گذاری claim. هیچ‌جا حذف نمی‌شود تا بعد از C3 + تست معکوس.
- هر متد **علاوه بر** claim دیفالت‌شده، یک principal هم می‌سازد: `BotPrincipal.internal(config.bot_owner_admin_id, scope_enforced=<از AdminUser.dedicated_bot_scope_enforced>)`.
- `get_package_files`/`get_tutorial_media`/`get_tutorial_software_file`/`get_admin_telegram_id`/`get_admin_username` - این ۵ تابع دیگر مستقیم `SessionLocal()+db.query` نمی‌زنند؛ همان ۵ accessor بخش ۵ (`_get_package_or_403` و ...) را که `routers/bot.py` هم صدا می‌زند، با principal‌شان صدا می‌زنند - یک تابع مشترک، نه دو نسخه.
- بقیه‌ی متدها (`list_users`, `create_user`, ...) دقیقاً مثل امروز `bot_router._do_*(..., owner_admin_id=_scope(owner_admin_id), principal=principal)` را صدا می‌زنند.

## ۱۰. Activation اتمیک + UI

```python
# routers/api_keys.py - تازه
@router.post("/{key_id}/activate", dependencies=[Depends(require_confirm_password)])
def activate_tenant_key(key_id: int, db: Session = Depends(get_db)):
    result = db.execute(
        update(models.ApiKey.__table__)
        .where(models.ApiKey.id == key_id, models.ApiKey.key_type == KeyType.TENANT_INTEGRATION,
               models.ApiKey.enabled == False)  # noqa: E712
        .values(enabled=True, scope_enforced=True)
    )
    db.commit()
    if result.rowcount == 0:
        raise HTTPException(409, "کلید در وضعیت قابل‌فعال‌سازی نیست")
    return {"ok": True}
```
`frontend/src/api/client.js`: `activateApiKey(id)` کنار `toggleApiKey` (خط ۲۶۴) - `POST /api/api-keys/{id}/activate` با هدر `X-Confirm-Password`. `frontend/src/pages/Settings.jsx`: دکمه‌ی «فعال‌سازی» به‌جای برچسب «در انتظار Phase C» روی کلید tenant. `frontend/src/i18n/translations.js`: کلید fa/en تازه برای متن دکمه + پیام تایید رمز.

## ۱۱. Rollout C0-C4

- **C0 - Wiring/Preflight + Shadow (یک commit).** `deps.get_bot_principal`؛ `principal` به هر ۳۸ endpoint (بخش ۷)؛ `panel_bridge.py` هم‌زمان (بخش ۹، `_scope()` حفظ)؛ ۵ accessor بایپس‌شده به سرویس مشترک (بخش ۵/۹)؛ `_sync_connections`+`provision_connection` (بخش ۸ - بخش package-authorization **فوراً فعال**، بخش bot-authorization از طریق `resolve_bot_authorization_scope` که خودش C0 را unrestricted نگه می‌دارد)؛ decorator+registry+تست پوشش (بخش ۶)؛ تست AST (بخش ۵). نتیجه: صفر تغییر رفتار برای هر کلید/بات موجود امروز؛ شادو از همین لحظه برای بات‌های اختصاصی دارای owner لاگ می‌شود (برای کلید HTTP، طبق بخش ۱، تا فعال‌سازی اولین کلید تست در C1 داده‌ای نیست - این یک محدودیت واقعی است، نه نقص).
- **C1 -** یک کلید تست (`activate_tenant_key`، بخش ۱۰) روی یک ادمین تست واقعی.
- **C2 -** فعال‌سازی تدریجی کلیدهای HTTP واقعی، با همان `activate_tenant_key`.
- **C3 -** فعال‌سازی تدریجی per-owner in-process با `UPDATE admin_users SET dedicated_bot_scope_enforced=true WHERE id=...`؛ فقط بعد از C3 برای اولین owner، `_scope()` حذف/تست معکوس نوشته می‌شود.
- **C4 -** ارتقای انتخابی کلید legacy، فقط با درخواست صریح صاحبش.
- **Rollback:** HTTP فقط `enabled=false`؛ in-process فقط `dedicated_bot_scope_enforced=false` روی همان AdminUser.

## ۱۲. فهرست کامل فایل‌ها (نهایی)

| # | فایل | تغییر |
|---|---|---|
| ۱ | `backend/app/services/hierarchy.py` | `selling_scope_node_ids(db, admin)` |
| ۲ | `backend/app/services/bot_auth.py` | invariant بخش ۱؛ `NodeAuthorizationScope`+۳ resolver (بخش ۲)؛ `_shadow_would_allow` (بخش ۳)؛ هر ۵ تابع مرکزی نسخه‌ی نهایی (بخش ۴)؛ `RoutePolicy`/`bot_route_policy` (بخش ۶) |
| ۳ | `backend/app/services/bot_resources.py` (تازه) | ۱۲ accessor بخش ۵ - مشترک بین HTTP و in-process |
| ۴ | `backend/app/services/user_ops.py` | `provision_connection` با `authorization_scope` اجباری؛ ۳ call site (بخش ۸) |
| ۵ | `backend/app/routers/packages.py` | `_sync_connections` با `hierarchy.selling_scope_node_ids` |
| ۶ | `backend/app/deps.py` | `get_bot_principal` |
| ۷ | `backend/app/routers/bot.py` | `@bot_route_policy` + `principal` روی هر ۳۸ endpoint (بخش ۷)؛ استفاده از `bot_resources.py` |
| ۸ | `backend/app/routers/api_keys.py` | `activate_tenant_key` (بخش ۱۰) |
| ۹ | `backend/app/telegram_bot/panel_bridge.py` | بخش ۹ - `_scope()` حفظ، principal اضافه، ۵ تابع به `bot_resources.py` وصل |
| ۱۰ | `backend/app/models.py` | `AdminUser.dedicated_bot_scope_enforced` (additive) |
| ۱۱ | `frontend/src/api/client.js` | `activateApiKey` |
| ۱۲ | `frontend/src/pages/Settings.jsx` | دکمه‌ی فعال‌سازی |
| ۱۳ | `frontend/src/i18n/translations.js` | کلیدهای fa/en تازه |
| ۱۴ | `backend/tests/test_bot_route_policy_coverage.py` (تازه) | بخش ۶ |
| ۱۵ | `backend/tests/test_bot_resource_accessor_ast.py` (تازه) | بخش ۵ |
| ۱۶ | `backend/tests/test_api_key_phase_b.py` یا فایل تازه | lifecycle کامل `activate_tenant_key` |
| ۱۷ | `backend/tests/test_bot_auth_principal.py` | بازنویسی کامل مطابق بخش‌های ۱-۴ |
| ۱۸ | `backend/tests/test_bot_inprocess_trust_characterization.py` | نسخه‌ی معکوس بعد از C3 + تست «`_scope()` در C0 هنوز پیش‌فرض را می‌گذارد» |
| ۱۹ | مستند این فایل | گزارش بعد از هر مرحله |

هیچ‌کدام اعمال نشده - صفر کد production، صفر commit/push. رمز مادر لمس نشد.

### وضعیت

اجرای C0 همچنان متوقف است تا همین نسخه‌ی ششم (canonical، مستقل و کامل) بازبینی/تایید شود.

**اصلاحیه ۲۰۲۶-۰۹-۲۸ (بازبینی هفتم Product): نسخه‌ی ششم منسوخ شد.** ۴ نقص P1 + ۱ ریسک rollout مهم (preflight داده‌ی تاریخی) + ۲ اصلاح اجرایی. **از این‌جا به بعد، بخش «Phase C - نسخه‌ی هفتم» تنها مرجع اجراست - کاملاً خودکفا، بدون ارجاع به نسخه‌های قبل.**

---

# Phase C - نسخه‌ی هفتم (canonical، کامل و مستقل - تنها مرجع اجرا)

## ۱. مدل پایه

بدون تغییر نسبت به `bot_auth.py`ی موجود، با یک invariant: `from_api_key` وقتی `key_type == TENANT_INTEGRATION and not scope_enforced` باشد، principal را نامعتبر (`_invalid`) می‌سازد - هرگز unscoped.

## ۲. `NodeAuthorizationScope` - نوع بسته با اعتبارسنجی واقعی در هر دو سر

```python
@dataclass(frozen=True)
class NodeAuthorizationScope:
    admin_id: Optional[int]
    unrestricted: bool

    def __post_init__(self):
        # فقط دقیقاً دو ترکیب مجاز - dataclass معمولی این را رایگان
        # تضمین نمی‌کند، پس اینجا صریح رد می‌شود.
        if self.unrestricted and self.admin_id is not None:
            raise ValueError("unrestricted=True نمی‌تواند admin_id داشته باشد")
        if not self.unrestricted and self.admin_id is None:
            raise ValueError("unrestricted=False باید admin_id واقعی داشته باشد")

    @classmethod
    def unrestricted_scope(cls) -> "NodeAuthorizationScope":
        return cls(admin_id=None, unrestricted=True)

    @classmethod
    def scoped(cls, admin_id: int) -> "NodeAuthorizationScope":
        return cls(admin_id=admin_id, unrestricted=False)


def resolve_package_authorization_scope(package: models.Package) -> NodeAuthorizationScope:
    if package.owner_admin_id is None:
        return NodeAuthorizationScope.unrestricted_scope()
    return NodeAuthorizationScope.scoped(package.owner_admin_id)


def resolve_bot_authorization_scope(principal: BotPrincipal) -> NodeAuthorizationScope:
    """اصلاح این دور: قبلاً principal نامعتبر (valid=False) هم چون
    is_scoped خودش به‌خودی False می‌شود، بی‌سروصدا unrestricted می‌گرفت -
    یک باگ در principal ساخت‌وساز، ambient-authority بی‌محدودیت می‌داد.
    حالا _ensure_valid همان اول صدا زده می‌شود - این resolver دیگر به
    اینکه caller قبلاً چیزی را چک کرده باشد اعتماد نمی‌کند، خودش هم چک
    می‌کند."""
    _ensure_valid(principal, "resolve_bot_authorization_scope")
    if not principal.is_scoped:
        return NodeAuthorizationScope.unrestricted_scope()
    return NodeAuthorizationScope.scoped(principal.owner_admin_id)


def resolve_superadmin_actor_scope(admin: models.AdminUser) -> NodeAuthorizationScope:
    """اصلاح این دور: قبلاً هر AdminUser بدون بررسی is_superadmin را
    unrestricted می‌کرد. حالا خودش fail-closed اعتبارسنجی می‌کند - اعتماد
    به اینکه caller از قبل require_superadmin را صدا زده کافی نیست."""
    if admin is None or not admin.is_superadmin:
        raise ValueError("resolve_superadmin_actor_scope فقط برای یک AdminUser واقعاً سوپرادمین معتبر است")
    return NodeAuthorizationScope.unrestricted_scope()
```

## ۳. Shadow evaluation

```python
def _shadow_would_allow(principal: BotPrincipal, decide: Callable[[BotPrincipal], bool]) -> Optional[bool]:
    if principal.owner_admin_id is None or principal.scope_enforced:
        return None
    try:
        shadow = dataclasses.replace(principal, scope_enforced=True)
        return bool(decide(shadow))
    except Exception:
        logger.exception("bot_auth shadow: خطای داخلی - نتیجه نامعلوم ثبت شد، مسیر واقعی اثر نگرفت")
        return None
```

## ۴. پنج تابع مرکزی (بدون تغییر نسبت به نسخه‌ی قبل - قبلاً درست بودند)

```python
def resolve_claimed_owner(db, principal, claimed_owner_id, *, endpoint=None):
    _ensure_valid(principal, "resolve_claimed_owner", endpoint=endpoint)
    def _decide(p):
        admin = db.get(models.AdminUser, p.owner_admin_id)
        owned = hierarchy.owned_admin_ids(db, admin) if admin else set()
        return claimed_owner_id is None or claimed_owner_id in owned
    if not principal.is_scoped:
        would_allow = _shadow_would_allow(principal, _decide)
        _log_decision(principal, "resolve_claimed_owner", allowed=True, reason="unscoped",
                      claimed_owner_id=claimed_owner_id, endpoint=endpoint, extra={"would_allow_if_enforced": would_allow})
        return claimed_owner_id
    if not _decide(principal):
        _log_decision(principal, "resolve_claimed_owner", allowed=False, reason="claim خارج از owned_admin_ids",
                      claimed_owner_id=claimed_owner_id, endpoint=endpoint)
        raise HTTPException(403, _DENIED_MESSAGE)
    _log_decision(principal, "resolve_claimed_owner", allowed=True, reason="own tenant/subtree", endpoint=endpoint)
    return claimed_owner_id if claimed_owner_id is not None else principal.owner_admin_id


def require_bot_capability(principal, capability, *, endpoint=None):
    _ensure_valid(principal, "require_bot_capability", endpoint=endpoint)
    allowed = capability in principal.capabilities
    _log_decision(principal, "require_bot_capability", allowed=allowed, reason=capability, endpoint=endpoint)
    if not allowed:
        raise HTTPException(403, _DENIED_MESSAGE)
    # مستقل از scope_enforced - شادو اینجا معنا ندارد.


def require_bot_user_access(db, principal, user, *, endpoint=None):
    _ensure_valid(principal, "require_bot_user_access", endpoint=endpoint)
    def _decide(p):
        admin = db.get(models.AdminUser, p.owner_admin_id)
        return admin is not None and hierarchy.can_see_user(admin, hierarchy.owned_admin_ids(db, admin), user.owner_admin_id)
    if not principal.is_scoped:
        would_allow = _shadow_would_allow(principal, _decide)
        _log_decision(principal, "require_bot_user_access", allowed=True, reason="unscoped",
                      endpoint=endpoint, extra={"would_allow_if_enforced": would_allow})
        return
    allowed = _decide(principal)
    _log_decision(principal, "require_bot_user_access", allowed=allowed, reason="hierarchy check", endpoint=endpoint)
    if not allowed:
        raise HTTPException(404, "کاربر پیدا نشد")


def require_bot_resource_owner_access(principal, resource_owner_admin_id, allowed_owner_ids_fn, *, endpoint=None):
    _ensure_valid(principal, "require_bot_resource_owner_access", endpoint=endpoint)
    def _decide(p):
        return resource_owner_admin_id in allowed_owner_ids_fn(p)
    if not principal.is_scoped:
        would_allow = _shadow_would_allow(principal, _decide)
        _log_decision(principal, "require_bot_resource_owner_access", allowed=True, reason="unscoped",
                      endpoint=endpoint, extra={"would_allow_if_enforced": would_allow})
        return
    allowed = _decide(principal)
    _log_decision(principal, "require_bot_resource_owner_access", allowed=allowed, reason="resource policy", endpoint=endpoint)
    if not allowed:
        raise HTTPException(404, "موردی یافت نشد")


def require_bot_node_access(db, principal, node, *, endpoint=None):
    _ensure_valid(principal, "require_bot_node_access", endpoint=endpoint)
    def _decide(p):
        admin = db.get(models.AdminUser, p.owner_admin_id)
        allowed_ids = hierarchy.selling_scope_node_ids(db, admin) if admin else set()
        return allowed_ids is None or node.id in allowed_ids
    if not principal.is_scoped:
        would_allow = _shadow_would_allow(principal, _decide)
        _log_decision(principal, "require_bot_node_access", allowed=True, reason="unscoped",
                      endpoint=endpoint, extra={"would_allow_if_enforced": would_allow})
        return
    allowed = _decide(principal)
    _log_decision(principal, "require_bot_node_access", allowed=allowed, reason="hierarchy check", endpoint=endpoint)
    if not allowed:
        raise HTTPException(404, "نود پیدا نشد")
```

## ۵. خانواده‌های accessor - **۱۲ تابع واقعی** (اصلاح: نسخه‌ی قبل ۱۰ تابع را «۱۲» شمرده بود، `AdminUser` هم اضافه شد)

```text
User:        _get_user_or_403(db, principal, username) -> User
             _get_user_by_telegram_or_403(db, principal, telegram_id) -> Optional[User]
             _list_users_query(db, principal, search) -> Query[User]
Package:     _get_package_or_403(db, principal, package_id) -> Package
             _list_packages_query(db, principal) -> Query[Package]
Tutorial:    _get_tutorial_or_403(db, principal, tutorial_id) -> Tutorial
             _list_tutorials_query(db, principal) -> Query[Tutorial]
PaymentCard: _get_payment_card_or_403(db, principal, card_id) -> PaymentCard
Node:        _get_node_or_403(db, principal, node_id) -> Node
             _scoped_node_ids(db, principal) -> Optional[set[int]]
AdminUser:   _get_admin_by_telegram_or_403(db, principal, telegram_id) -> Optional[AdminUser]   # admin-by-telegram, get_admin_by_telegram
             _get_admin_or_403(db, principal, admin_id) -> AdminUser                             # admin-username, get_admin_telegram_id/username
```
شمارش: User=۳, Package=۲, Tutorial=۲, PaymentCard=۱, Node=۲, AdminUser=۲ → **۱۲**. هر دو تابع `AdminUser` از `require_bot_resource_owner_access` با `allowed_owner_ids_fn = lambda p: hierarchy.owned_admin_ids(db, db.get(models.AdminUser, p.owner_admin_id)) | {p.owner_admin_id}` استفاده می‌کنند - یک principal فقط می‌تواند خودش یا زیردرخت خودش را lookup کند، نه هر admin_id/telegram_id دلخواه.

`GATED_MODELS = {User, Package, Tutorial, PaymentCard, Node, AdminUser}` - شش مدل، نه پنج.

## ۶. تست AST - هر دو مسیر (HTTP + in-process) + خودِ فایل سرویس

```python
# tests/test_bot_resource_accessor_ast.py
ALLOWED_FUNCS = {  # همان ۱۲ تابع بخش ۵، دقیقاً
    "_get_user_or_403", "_get_user_by_telegram_or_403", "_list_users_query",
    "_get_package_or_403", "_list_packages_query",
    "_get_tutorial_or_403", "_list_tutorials_query",
    "_get_payment_card_or_403",
    "_get_node_or_403", "_scoped_node_ids",
    "_get_admin_by_telegram_or_403", "_get_admin_or_403",
}
GATED_MODELS = {"User", "Package", "Tutorial", "PaymentCard", "Node", "AdminUser"}

# (۱) پیمایش AST هر دو فایل - routers/bot.py و telegram_bot/panel_bridge.py -
#     هر Call به db.query(models.X)/db.get(models.X, ...) با X در GATED_MODELS
#     باید داخل بدنه‌ی یکی از ALLOWED_FUNCS باشد (طبق تایید اثبات‌شده‌ی این
#     دور: بایپس‌های panel_bridge.py دقیقاً از همین جنس بودند، نه فقط در
#     routers/bot.py).
# (۲) پیمایش AST services/bot_resources.py - نام هر تابع سطح‌بالای این ماژول
#     باید دقیقاً برابر ALLOWED_FUNCS باشد (نه زیرمجموعه، نه ابرمجموعه) -
#     یک تابع جدید با نام فریبنده در همین فایل هم بدون شکستن این تست اضافه
#     نمی‌شود.
```

## ۷. Route-policy decorator + جدول کامل ۳۸ endpoint

```python
@dataclass(frozen=True)
class RoutePolicy:
    capability: Optional[str]
    resource_strategy: str

def bot_route_policy(capability, resource_strategy):
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, principal: BotPrincipal, **kwargs):
            if capability is None:
                _ensure_valid(principal, fn.__name__)
            else:
                require_bot_capability(principal, capability, endpoint=fn.__name__)
            return fn(*args, principal=principal, **kwargs)
        wrapper._bot_capability = capability
        wrapper._bot_resource_strategy = resource_strategy
        return wrapper
    return decorator
```
**اصلاح مسیر:** `APIRoute.path` شامل prefix کامل روتر است (`/api/bot/...`)، نه مسیر نسبی - جدول پایین برای خوانایی نسبی نوشته شده، ولی `CANONICAL` واقعی در تست هر مسیر را با پیشوند `/api/bot` کامل می‌سازد:
```python
routes = [r for r in router.routes if isinstance(r, APIRoute)]
assert len(routes) == 38
CANONICAL = {("GET", "/api/bot" + path): (cap, strat) for path, cap, strat in TABLE_ROWS}   # TABLE_ROWS = ردیف‌های زیر
for r in routes:
    method = next(iter(r.methods - {"HEAD"}))
    got = (r.endpoint._bot_capability, r.endpoint._bot_resource_strategy)
    assert got == CANONICAL[(method, r.path)], f"{method} {r.path}: {got} != {CANONICAL[(method, r.path)]}"
```

| # | Method | Path (نسبی به `/api/bot`) | Capability | resource_strategy |
|---|---|---|---|---|
| ۱ | GET | `/nodes` | *(None)* | `node_list` |
| ۲ | GET | `/packages` | `CUSTOMER_READ` | `package_list` |
| ۳ | GET | `/payment-info` | `PAYMENT_READ` | `claim` |
| ۴ | GET | `/payment-cards/{card_id}` | `PAYMENT_READ` | `payment_card_owner` |
| ۵ | POST | `/payment-cards/{card_id}/record-payment` | `PAYMENT_WRITE` | `payment_card_owner` |
| ۶ | GET | `/sales-stats` | `CUSTOMER_READ` | `claim` |
| ۷ | GET | `/customer-menu-config` | *(None)* | `none` |
| ۸ | GET | `/tutorials` | `CUSTOMER_READ` | `tutorial_list` |
| ۹ | GET | `/packages/{package_id}/files/{file_id}/download` | `FILES_READ` | `package_owner` |
| ۱۰ | GET | `/tutorials/{tutorial_id}/media/{media_id}/download` | `FILES_READ` | `tutorial_owner` |
| ۱۱ | GET | `/tutorials/{tutorial_id}/software/{software_id}/download` | `FILES_READ` | `tutorial_owner` |
| ۱۲ | GET | `/admin-by-telegram/{tg_id}` | `ADMIN_LOOKUP` | `admin_hierarchy` |
| ۱۳ | GET | `/admin-username/{admin_id}` | `ADMIN_LOOKUP` | `admin_hierarchy` |
| ۱۴ | GET | `/telegram-user-ids` | `BROADCAST` | `claim` |
| ۱۵ | POST | `/users` | `CUSTOMER_WRITE` | `claim` |
| ۱۶ | POST | `/users/{username}/purchase-package` | `CUSTOMER_WRITE` | `user` |
| ۱۷ | POST | `/referral/apply` | `CUSTOMER_WRITE` | `user` |
| ۱۸ | POST | `/discount/validate` | `CUSTOMER_WRITE` | `claim` |
| ۱۹ | POST | `/discount/redeem` | `CUSTOMER_WRITE` | `claim` |
| ۲۰ | GET | `/users` | `CUSTOMER_READ` | `user_list` |
| ۲۱ | GET | `/users/by-telegram/{telegram_id}` | `CUSTOMER_READ` | `claim` |
| ۲۲ | GET | `/users/by-telegram/{telegram_id}/all` | `CUSTOMER_READ` | `claim` |
| ۲۳ | GET | `/users/{username}` | `CUSTOMER_READ` | `user` |
| ۲۴ | POST | `/users/{username}/link-telegram` | `IDENTITY_WRITE` | `user` |
| ۲۵ | POST | `/users/{username}/connections` | `CUSTOMER_WRITE` | `user+node` |
| ۲۶ | GET | `/users/{username}/purchases` | `CUSTOMER_READ` | `user` |
| ۲۷ | POST | `/users/{username}/purchases/{purchase_id}/rename` | `CUSTOMER_WRITE` | `user` |
| ۲۸ | DELETE | `/users/{username}/purchases/{purchase_id}` | `CUSTOMER_WRITE` | `user` |
| ۲۹ | DELETE | `/users/{username}/connections/{connection_id}` | `CUSTOMER_WRITE` | `user` |
| ۳۰ | GET | `/users/{username}/subscription-link` | `CUSTOMER_READ` | `user` |
| ۳۱ | GET | `/miniapp-button-text` | *(None)* | `none` |
| ۳۲ | GET | `/panel-public-url` | *(None)* | `none` |
| ۳۳ | POST | `/users/{username}/purchases/{purchase_id}/renew` | `CUSTOMER_WRITE` | `user` |
| ۳۴ | POST | `/users/{username}/renew` | `CUSTOMER_WRITE` | `user` |
| ۳۵ | POST | `/users/{username}/reset-usage` | `CUSTOMER_WRITE` | `user` |
| ۳۶ | POST | `/users/{username}/set-enabled` | `CUSTOMER_WRITE` | `user` |
| ۳۷ | POST | `/users/{username}/add-balance` | `WALLET_WRITE` | `user` |
| ۳۸ | DELETE | `/users/{username}` | `CUSTOMER_WRITE` | `user` |

`resource_strategy` → accessor: `claim`→`resolve_claimed_owner`؛ `user`→`_get_user_or_403`؛ `*_list`→`_list_*_query`/`_scoped_node_ids`؛ `user+node`→`_get_user_or_403`+`_get_node_or_403`؛ `package_owner`/`tutorial_owner`/`payment_card_owner`→accessor مربوطه (با `require_bot_resource_owner_access` داخلش)؛ `admin_hierarchy`→`_get_admin_by_telegram_or_403`/`_get_admin_or_403`؛ `none`→فقط `_ensure_valid`.

## ۸. Provisioning - با preflight اجباری قبل از enforcement package-authorization

```python
def provision_connection(db, user, node, protocol, *, authorization_scope: NodeAuthorizationScope, ...):
    if not authorization_scope.unrestricted:
        admin = db.get(models.AdminUser, authorization_scope.admin_id)
        allowed = hierarchy.selling_scope_node_ids(db, admin) if admin else set()
        if allowed is not None and node.id not in allowed:
            raise HTTPException(403, f"سرور «{node.name}» در اختیار این مجموعه نیست")
    ...
```

**اصلاح ریسک این دور - `authorization_scope=resolve_package_authorization_scope(package)` (مسیر bundled) دیگر «همیشه فوراً فعال از C0» نیست.** فعال‌شدن آنی می‌تواند یک `PackageConnection` تاریخی (که node آن دیگر واقعاً grant نشده - داده‌ی قدیمی/drift، نه یک اتک) را در همان اولین خرید/تمدید بعدی رد کند - قطع فروش واقعی برای یک پکیج که تا امروز کار می‌کرده، بدون هشدار قبلی. مرحله‌ی تازه‌ی اجباری **قبل از فعال‌سازی enforcement این بخش:**

```python
# scripts/preflight_package_node_scope.py (یا یک تست read-only)
for pc in db.query(models.PackageConnection).all():
    package = db.get(models.Package, pc.package_id)
    scope = resolve_package_authorization_scope(package)
    if scope.unrestricted:
        continue
    admin = db.get(models.AdminUser, scope.admin_id)
    allowed = hierarchy.selling_scope_node_ids(db, admin) if admin else set()
    if allowed is not None and pc.node_id not in allowed:
        print(f"MISMATCH package={package.id} node={pc.node_id} owner={scope.admin_id}")
```
این اسکریپت **read-only** است، چیزی رد نمی‌کند، فقط گزارش می‌دهد. طبق نتیجه‌اش:
- گزارش خالی → enforcement بخش package-authorization در همان commit C0 فعال می‌شود، بدون نگرانی.
- گزارش ناخالی → enforcement این بخش به یک commit **جدا و بعدی** موکول می‌شود، بعد از این‌که هر مورد گزارش‌شده یا پاک‌سازی شود (grant صحیح، یا اصلاح/حذف آن `PackageConnection`) یا صراحتاً توسط Product پذیرفته شود («این مورد را می‌دانیم، عمداً استثناست»).
مسیر bot-authorization (`resolve_bot_authorization_scope`) از این preflight مستقل است - چون خودش همین حالا `is_scoped` را چک می‌کند و در C0 همیشه unrestricted می‌ماند (بدون تغییر رفتار، بدون نیاز به preflight).

`routers/packages.py:_sync_connections` هم از `hierarchy.selling_scope_node_ids` استفاده می‌کند (write-time، مستقل از این preflight چون فقط پکیج‌های **تازه ویرایش‌شده** را لمس می‌کند، نه تاریخی).

## ۹. `panel_bridge.py`

- **`_scope(owner_admin_id)` برای همیشه می‌ماند - هرگز حذف نمی‌شود.** اصلاح این دور: نسخه‌ی قبل گفته بود «بعد از C3 حذف می‌شود» - این غلط بود، چون `_scope()` یک تابع **سراسری مشترک** بین همه‌ی ربات‌های اختصاصی است، در حالی که C3 **per-owner** فعال می‌شود؛ حذفش بعد از فعال‌شدن اولین owner، پیش‌فرض claim را از هر ربات اختصاصی دیگری که هنوز `dedicated_bot_scope_enforced=false` دارد می‌گیرد و آن‌ها را به رفتار ربات مشترک (global) تبدیل می‌کند - یک regression واقعی، نه یک بهبود. `_scope()` کار defaulting را می‌کند؛ enforcement واقعی (رد claim خارج از hierarchy) کاملاً جدا توسط `resolve_claimed_owner` انجام می‌شود و به وجود/عدم‌وجود `_scope()` هیچ ربطی ندارد - پس حذفش هیچ‌وقت هیچ مزیت امنیتی‌ای نداشت.
- هر متد یک `BotPrincipal.internal(config.bot_owner_admin_id, scope_enforced=<AdminUser.dedicated_bot_scope_enforced>)` هم می‌سازد، **علاوه بر** (نه به‌جای) `_scope()`.
- ۵ تابع بایپس‌شده (`get_package_files`, `get_tutorial_media`, `get_tutorial_software_file`, `get_admin_telegram_id`, `get_admin_username`) به ۵ accessor مربوطه‌ی `services/bot_resources.py` (بخش ۵) وصل می‌شوند - همان تابعی که `routers/bot.py` هم صدا می‌زند.

## ۱۰. Activation اتمیک + UI (بدون تغییر)

```python
@router.post("/{key_id}/activate", dependencies=[Depends(require_confirm_password)])
def activate_tenant_key(key_id: int, db: Session = Depends(get_db)):
    result = db.execute(
        update(models.ApiKey.__table__)
        .where(models.ApiKey.id == key_id, models.ApiKey.key_type == KeyType.TENANT_INTEGRATION,
               models.ApiKey.enabled == False)  # noqa: E712
        .values(enabled=True, scope_enforced=True)
    )
    db.commit()
    if result.rowcount == 0:
        raise HTTPException(409, "کلید در وضعیت قابل‌فعال‌سازی نیست")
    return {"ok": True}
```
+ `client.js`'s `activateApiKey`، `Settings.jsx`'s دکمه‌ی فعال‌سازی، `translations.js`'s کلید fa/en تازه.

## ۱۱. Rollout C0-C4 (با preflight به‌عنوان زیرمرحله‌ی اجباری)

- **C0a - Wiring/Preflight + Shadow + گزارش preflight (یک commit، بدون enforcement package-authorization).** همه‌ی وایرینگ (principal روی ۳۸ endpoint، `panel_bridge.py` با `_scope()` دائمی، ۱۲ accessor، decorator+هر دو تست AST/coverage) + اجرای اسکریپت preflight بخش ۸ و گزارش نتیجه‌اش. بخش bot-authorization در `provision_connection` از همین commit با `resolve_bot_authorization_scope` فعال است (چون خودش C0 را unrestricted نگه می‌دارد - صفر تغییر رفتار). بخش package-authorization **فقط اگر preflight خالی باشد** همین‌جا فعال می‌شود؛ وگرنه به C0b موکول می‌شود.
- **C0b (فقط اگر C0a‌ی preflight ناخالی بود) -** بعد از پاک‌سازی/تایید موارد گزارش‌شده، فعال‌سازی enforcement package-authorization در یک commit جدا.
- **C1 -** یک کلید تست با `activate_tenant_key`.
- **C2 -** فعال‌سازی تدریجی کلیدهای HTTP واقعی.
- **C3 -** فعال‌سازی تدریجی per-owner in-process (`UPDATE admin_users SET dedicated_bot_scope_enforced=true WHERE id=...`). **`_scope()` هرگز حذف نمی‌شود، نه در این مرحله نه بعدش.**
- **C4 -** ارتقای انتخابی کلید legacy، فقط با درخواست صریح صاحبش.
- **Rollback:** HTTP فقط `enabled=false`؛ in-process فقط `dedicated_bot_scope_enforced=false` روی همان AdminUser - هیچ‌کدام به `_scope()` کاری ندارند.

## ۱۲. فهرست کامل فایل‌ها

| # | فایل | تغییر |
|---|---|---|
| ۱ | `backend/app/services/hierarchy.py` | `selling_scope_node_ids(db, admin)` |
| ۲ | `backend/app/services/bot_auth.py` | invariant §۱؛ `NodeAuthorizationScope`+۳ resolver با اعتبارسنجی (§۲)؛ `_shadow_would_allow` (§۳)؛ ۵ تابع مرکزی (§۴)؛ `RoutePolicy`/`bot_route_policy` (§۷) |
| ۳ | `backend/app/services/bot_resources.py` (تازه) | دقیقاً ۱۲ accessor §۵ - نه بیشتر، نه کمتر (تضمین‌شده با تست §۶) |
| ۴ | `backend/app/services/user_ops.py` | `provision_connection` با `authorization_scope` اجباری (§۸) |
| ۵ | `backend/app/routers/packages.py` | `_sync_connections` با `hierarchy.selling_scope_node_ids` |
| ۶ | `backend/app/deps.py` | `get_bot_principal` |
| ۷ | `backend/app/routers/bot.py` | `@bot_route_policy`+`principal` روی هر ۳۸ endpoint (§۷)؛ استفاده از `bot_resources.py` |
| ۸ | `backend/app/routers/api_keys.py` | `activate_tenant_key` (§۱۰) |
| ۹ | `backend/app/telegram_bot/panel_bridge.py` | `_scope()` دائمی + principal اضافه (§۹)؛ ۵ تابع به `bot_resources.py` وصل |
| ۱۰ | `backend/app/models.py` | `AdminUser.dedicated_bot_scope_enforced` (additive) |
| ۱۱ | `backend/scripts/preflight_package_node_scope.py` (تازه) | §۸ - read-only، بدون اثر روی رفتار زنده |
| ۱۲ | `frontend/src/api/client.js` | `activateApiKey` |
| ۱۳ | `frontend/src/pages/Settings.jsx` | دکمه‌ی فعال‌سازی |
| ۱۴ | `frontend/src/i18n/translations.js` | کلیدهای fa/en تازه |
| ۱۵ | `backend/tests/test_bot_route_policy_coverage.py` (تازه) | §۷، با نرمال‌سازی مسیر `/api/bot` |
| ۱۶ | `backend/tests/test_bot_resource_accessor_ast.py` (تازه) | §۶ - هر دو فایل + self-check `bot_resources.py` |
| ۱۷ | `backend/tests/test_api_key_phase_b.py` یا فایل تازه | lifecycle کامل `activate_tenant_key` |
| ۱۸ | `backend/tests/test_bot_auth_principal.py` | بازنویسی کامل مطابق §۱-۴، شامل تست `resolve_superadmin_actor_scope`/`resolve_bot_authorization_scope` روی principal نامعتبر/admin غیرسوپرادمین و `NodeAuthorizationScope.__post_init__` |
| ۱۹ | `backend/tests/test_bot_inprocess_trust_characterization.py` | نسخه‌ی معکوس بعد از C3 + تست «`_scope()` بعد از فعال‌شدن یک owner، برای owner دیگر هنوز پیش‌فرض را می‌گذارد» |
| ۲۰ | مستند این فایل | گزارش preflight (§۸) + گزارش بعد از هر مرحله |

هیچ‌کدام اعمال نشده - صفر کد production، صفر commit/push. رمز مادر لمس نشد.

### وضعیت

اجرای C0 همچنان متوقف است تا همین نسخه‌ی هفتم (canonical، مستقل و کامل) بازبینی/تایید شود.

**اصلاحیه ۲۰۲۶-۰۹-۲۸ (بازبینی هشتم Product): نسخه‌ی هفتم منسوخ شد.** طراحی اصلی (principal، shadow، `_scope()` دائمی، decorator، rollback) تایید شد؛ ۲ نقص P1 + ۱ نقص P2 + ۱ نکته‌ی عملیاتی (preflight بدون اثر واقعی) باقی ماند. **از این‌جا به بعد، بخش «Phase C - نسخه‌ی هشتم» تنها مرجع اجراست - کاملاً خودکفا.**

---

# Phase C - نسخه‌ی هشتم (canonical، کامل و مستقل - تنها مرجع اجرا)

## ۱. مدل پایه، `NodeAuthorizationScope`، Shadow، ۵ تابع مرکزی (کامل، بدون ارجاع)

```python
# --- مدل پایه (BotPrincipal.from_api_key: یک invariant تازه) ---
if key_type == KeyType.TENANT_INTEGRATION and not scope_enforced:
    return cls._invalid(key.id, key.label)   # هرگز unscoped - فقط enforced یا نامعتبر


# --- NodeAuthorizationScope ---
@dataclass(frozen=True)
class NodeAuthorizationScope:
    admin_id: Optional[int]
    unrestricted: bool

    def __post_init__(self):
        if self.unrestricted and self.admin_id is not None:
            raise ValueError("unrestricted=True نمی‌تواند admin_id داشته باشد")
        if not self.unrestricted and self.admin_id is None:
            raise ValueError("unrestricted=False باید admin_id واقعی داشته باشد")

    @classmethod
    def unrestricted_scope(cls) -> "NodeAuthorizationScope":
        return cls(admin_id=None, unrestricted=True)

    @classmethod
    def scoped(cls, admin_id: int) -> "NodeAuthorizationScope":
        return cls(admin_id=admin_id, unrestricted=False)


def resolve_package_authorization_scope(package: models.Package) -> NodeAuthorizationScope:
    if package.owner_admin_id is None:
        return NodeAuthorizationScope.unrestricted_scope()
    return NodeAuthorizationScope.scoped(package.owner_admin_id)


def resolve_bot_authorization_scope(principal: BotPrincipal) -> NodeAuthorizationScope:
    _ensure_valid(principal, "resolve_bot_authorization_scope")   # قبل از هر چیز - یک principal نامعتبر هرگز unrestricted نمی‌گیرد
    if not principal.is_scoped:
        return NodeAuthorizationScope.unrestricted_scope()
    return NodeAuthorizationScope.scoped(principal.owner_admin_id)


def resolve_superadmin_actor_scope(admin: models.AdminUser) -> NodeAuthorizationScope:
    if admin is None or not admin.is_superadmin:
        raise ValueError("resolve_superadmin_actor_scope فقط برای یک AdminUser واقعاً سوپرادمین معتبر است")
    return NodeAuthorizationScope.unrestricted_scope()


# --- اصلاح این دور: امضای واقعی _log_decision امروز (bot_auth.py:390-393)
# پارامتر extra ندارد - `_log_decision(..., extra={...})` که در نسخه‌های قبل
# این سند برای هر ۴ تابع مرکزی نوشته شده بود، روی کد واقعی با
# TypeError: _log_decision() got an unexpected keyword argument 'extra'
# شکست می‌خورد. چون در C0 تقریباً همه‌ی principalها unscoped‌اند (شاخه‌ای
# که extra را می‌فرستد)، این یعنی قطعی کامل کل /api/bot از همان اولین
# درخواست بعد از deploy - نه یک نقص جزئی. اصلاح: یک پارامتر نام‌دار واقعی
# اضافه می‌شود، نه یک **kwargs عمومی:
def _log_decision(
    principal: BotPrincipal, action: str, *, allowed: bool, reason: str = "",
    claimed_owner_id: Optional[int] = None, endpoint: Optional[str] = None,
    would_allow_if_enforced: Optional[bool] = None,
) -> None:
    logger.info(
        "bot_auth decision: action=%s allowed=%s reason=%r endpoint=%s "
        "key_id=%s key_type=%s key_label=%r principal_owner=%s claimed_owner=%s "
        "scope_enforced=%s would_allow_if_enforced=%s",
        action, allowed, reason, endpoint,
        principal.key_id, principal.key_type, principal.label,
        principal.owner_admin_id, claimed_owner_id, principal.scope_enforced,
        would_allow_if_enforced,
    )
# هر ۴ فراخوانی extra={"would_allow_if_enforced": would_allow} در ۵ تابع
# مرکزی پایین، به would_allow_if_enforced=would_allow (پارامتر مستقیم، نه
# دیکشنری) اصلاح می‌شود.


# --- Shadow evaluation ---
def _shadow_would_allow(principal: BotPrincipal, decide: Callable[[BotPrincipal], bool]) -> Optional[bool]:
    if principal.owner_admin_id is None or principal.scope_enforced:
        return None
    try:
        shadow = dataclasses.replace(principal, scope_enforced=True)
        return bool(decide(shadow))
    except Exception:
        logger.exception("bot_auth shadow: خطای داخلی - نتیجه نامعلوم ثبت شد، مسیر واقعی اثر نگرفت")
        return None


# --- پنج تابع مرکزی ---
def resolve_claimed_owner(db: Session, principal: BotPrincipal, claimed_owner_id: Optional[int], *, endpoint=None) -> Optional[int]:
    _ensure_valid(principal, "resolve_claimed_owner", endpoint=endpoint)
    def _decide(p: BotPrincipal) -> bool:
        admin = db.get(models.AdminUser, p.owner_admin_id)
        owned = hierarchy.owned_admin_ids(db, admin) if admin else set()
        return claimed_owner_id is None or claimed_owner_id in owned
    if not principal.is_scoped:
        would_allow = _shadow_would_allow(principal, _decide)
        _log_decision(principal, "resolve_claimed_owner", allowed=True, reason="unscoped",
                      claimed_owner_id=claimed_owner_id, endpoint=endpoint, would_allow_if_enforced=would_allow)
        return claimed_owner_id
    if not _decide(principal):
        _log_decision(principal, "resolve_claimed_owner", allowed=False, reason="claim خارج از owned_admin_ids",
                      claimed_owner_id=claimed_owner_id, endpoint=endpoint)
        raise HTTPException(403, _DENIED_MESSAGE)
    _log_decision(principal, "resolve_claimed_owner", allowed=True, reason="own tenant/subtree", endpoint=endpoint)
    return claimed_owner_id if claimed_owner_id is not None else principal.owner_admin_id


def require_bot_capability(principal: BotPrincipal, capability: str, *, endpoint=None) -> None:
    _ensure_valid(principal, "require_bot_capability", endpoint=endpoint)
    allowed = capability in principal.capabilities
    _log_decision(principal, "require_bot_capability", allowed=allowed, reason=capability, endpoint=endpoint)
    if not allowed:
        raise HTTPException(403, _DENIED_MESSAGE)
    # مستقل از scope_enforced - شادو اینجا معنا ندارد.


def require_bot_user_access(db: Session, principal: BotPrincipal, user: models.User, *, endpoint=None) -> None:
    _ensure_valid(principal, "require_bot_user_access", endpoint=endpoint)
    def _decide(p: BotPrincipal) -> bool:
        admin = db.get(models.AdminUser, p.owner_admin_id)
        return admin is not None and hierarchy.can_see_user(admin, hierarchy.owned_admin_ids(db, admin), user.owner_admin_id)
    if not principal.is_scoped:
        would_allow = _shadow_would_allow(principal, _decide)
        _log_decision(principal, "require_bot_user_access", allowed=True, reason="unscoped",
                      endpoint=endpoint, would_allow_if_enforced=would_allow)
        return
    allowed = _decide(principal)
    _log_decision(principal, "require_bot_user_access", allowed=allowed, reason="hierarchy check", endpoint=endpoint)
    if not allowed:
        raise HTTPException(404, "کاربر پیدا نشد")


def require_bot_resource_owner_access(
    principal: BotPrincipal, resource_owner_admin_id: Optional[int],
    allowed_owner_ids_fn: Callable[[BotPrincipal], set[Optional[int]]], *, endpoint=None,
) -> None:
    _ensure_valid(principal, "require_bot_resource_owner_access", endpoint=endpoint)
    def _decide(p: BotPrincipal) -> bool:
        return resource_owner_admin_id in allowed_owner_ids_fn(p)
    if not principal.is_scoped:
        would_allow = _shadow_would_allow(principal, _decide)
        _log_decision(principal, "require_bot_resource_owner_access", allowed=True, reason="unscoped",
                      endpoint=endpoint, would_allow_if_enforced=would_allow)
        return
    allowed = _decide(principal)
    _log_decision(principal, "require_bot_resource_owner_access", allowed=allowed, reason="resource policy", endpoint=endpoint)
    if not allowed:
        raise HTTPException(404, "موردی یافت نشد")


def require_bot_node_access(db: Session, principal: BotPrincipal, node: models.Node, *, endpoint=None) -> None:
    _ensure_valid(principal, "require_bot_node_access", endpoint=endpoint)
    def _decide(p: BotPrincipal) -> bool:
        admin = db.get(models.AdminUser, p.owner_admin_id)
        allowed_ids = hierarchy.selling_scope_node_ids(db, admin) if admin else set()
        return allowed_ids is None or node.id in allowed_ids
    if not principal.is_scoped:
        would_allow = _shadow_would_allow(principal, _decide)
        _log_decision(principal, "require_bot_node_access", allowed=True, reason="unscoped",
                      endpoint=endpoint, would_allow_if_enforced=would_allow)
        return
    allowed = _decide(principal)
    _log_decision(principal, "require_bot_node_access", allowed=allowed, reason="hierarchy check", endpoint=endpoint)
    if not allowed:
        raise HTTPException(404, "نود پیدا نشد")
```

## ۲. Preflight per-installation (نه per-commit)

**مسئله‌ی تاییدشده:** این پروژه روی SQLite مستقل هر مشتری deploy می‌شود (`docker compose`، یک نصب = یک فایل دیتابیس جدا). خالی‌بودن preflight روی دیتابیس dev/CI هیچ ربطی به وضعیت دیتابیس واقعی هر سرور ندارد - یک commit نمی‌تواند "شرطی" enforcement را روشن کند.

**اصلاح:** یک ستون additive تازه، **پیش‌فرض همه‌جا خاموش**، مستقل از کد:
```python
# models.py - PanelSettings
package_node_scope_enforced = Column(Boolean, default=False, nullable=False)
```
- **C0a (همه‌جا یکسان deploy می‌شود):** کد enforcement + preflight script + هر دو تست، با `provision_connection`ی که خودش `PanelSettings.package_node_scope_enforced` را می‌خواند - اگر `False` (پیش‌فرض هر نصب، حتی نصب‌های موجود بعد از آپدیت)، بخش package-authorization دقیقاً مثل امروز رفتار می‌کند (unrestricted)؛ بخش bot-authorization (`resolve_bot_authorization_scope`) مستقل است و طبق طراحی قبلی خودش C0 را unrestricted نگه می‌دارد.
- **فعال‌سازی (per-installation، نه per-commit):** هر نصب، اپراتور خودش (نه یک commit مشترک) اسکریپت preflight (بخش ۳ پایین) را روی دیتابیس **خودش** اجرا می‌کند؛ اگر گزارش خالی بود (یا بعد از پاک‌سازی خالی شد)، یک endpoint/دستور جدید (`PUT /api/settings/package-node-scope`, `require_superadmin`+`require_confirm_password`) همان یک ستون را `True` می‌کند - دقیقاً هم‌الگو با فعال‌سازی per-owner `dedicated_bot_scope_enforced` (که خودش هم از قبل per-installation/per-admin طراحی شده بود، نه per-commit).
- **این یعنی «C0b» دیگر یک commit جدا نیست - همان یک flag، توسط اپراتور هر نصب، هر زمان که preflightش خودش خالی شد.**

## ۳. خانواده‌های accessor - ۱۱ تابع کامل (`_list_users_query` با claim اجباری + `telegram_id`)

`GET /users/by-telegram/{telegram_id}` و `GET /users/by-telegram/{telegram_id}/all` (`routers/bot.py:957-993`) هر دو دقیقاً `db.query(models.User).filter(User.telegram_id==...)` + همان `_visibility_filter` را می‌زنند - همان شکل «فیلتر یک لیست با یک claim» که `_list_users_query` (برای `search`) دارد، نه یک single-object lookup. تنها فرق دو endpoint، `.first()` در برابر `.all()` روی همان query است - این تفاوت در **caller** است، نه در accessor.

**اصلاح این دور - claim اجباری، بدون مقدار پیش‌فرض:** نسخه‌ی قبل `resolve_claimed_owner` را با یک placeholder توصیفی صدا می‌زد و هیچ caller نمونه‌ای واقعاً claimed owner را پاس نمی‌داد - یعنی در عمل همیشه `None` می‌رفت و فیلتر owner (که امروز دقیقاً همان چیزی است که `_scope()` برای ربات اختصاصی می‌سازد) گم می‌شد. حالا `claimed_owner_admin_id` پارامتر **اجباری، بدون default** است - فراموش‌شدنش یک `TypeError` صریح می‌دهد، نه یک لیست بی‌فیلتر خاموش:
```python
def _list_users_query(db: Session, principal: BotPrincipal, claimed_owner_admin_id: Optional[int], *,
                       search: Optional[str] = None, telegram_id: Optional[int] = None) -> Query[models.User]:
    owner_admin_id = resolve_claimed_owner(db, principal, claimed_owner_admin_id)
    query = db.query(models.User)
    if telegram_id is not None:
        query = query.filter(models.User.telegram_id == telegram_id)
    if search:
        ... (فیلتر امروزی search)
    clause = _visibility_filter(db, owner_admin_id)
    if clause is not None:
        query = query.filter(clause)
    return query
```
```python
# routers/bot.py - claimed_owner_admin_id همان owner_admin_id ورودی endpoint، صریح پاس داده می‌شود
@bot_route_policy(capability=CUSTOMER_READ, resource_strategy="user_list")
def get_user_by_telegram(telegram_id, owner_admin_id=None, principal=..., db=...):
    user = _list_users_query(db, principal, owner_admin_id, telegram_id=telegram_id).order_by(models.User.id.desc()).first()
    ...

@bot_route_policy(capability=CUSTOMER_READ, resource_strategy="user_list")
def list_users_by_telegram(telegram_id, owner_admin_id=None, principal=..., db=...):
    users = _list_users_query(db, principal, owner_admin_id, telegram_id=telegram_id).order_by(models.User.id.desc()).all()
    ...

@bot_route_policy(capability=CUSTOMER_READ, resource_strategy="user_list")
def list_users(page=1, page_size=8, search=None, owner_admin_id=None, principal=..., db=...):
    query = _list_users_query(db, principal, owner_admin_id, search=search)
    ...
```
```python
# telegram_bot/panel_bridge.py - claim از _scope() می‌آید (هیچ‌جا حذف نشده)، principal جدا ساخته می‌شود
async def get_user_by_telegram(self, telegram_id, owner_admin_id=None):
    principal = BotPrincipal.internal(config.bot_owner_admin_id, scope_enforced=await _dedicated_scope_enforced())
    return _dump(await _call(bot_router.get_user_by_telegram, telegram_id,
                              owner_admin_id=_scope(owner_admin_id), principal=principal))
```
همین قاعده («claim از `_scope()`/پارامتر ورودی، صریح، بدون default، به هر accessor لیستی پاس داده می‌شود») برای `_list_packages_query`/`_list_tutorials_query` هم عیناً تکرار می‌شود - هر جا در جدول بخش ۶ استراتژی `claim` یا `*_list` دارد.

**فهرست کامل و نهایی ۱۱ accessor** (`_get_user_by_telegram_or_403` حذف شد - بعد از این‌که هر دو endpoint تلگرام به `_list_users_query` وصل شدند، این تابع هیچ caller واقعی نداشت؛ نگه‌داشتنش صرفاً یک تابع مرده در whitelist بود):
```text
User:        _get_user_or_403(db, principal, username) -> User
             _list_users_query(db, principal, claimed_owner_admin_id, *, search=None, telegram_id=None) -> Query[User]
Package:     _get_package_or_403(db, principal, package_id) -> Package
             _list_packages_query(db, principal, claimed_owner_admin_id) -> Query[Package]
Tutorial:    _get_tutorial_or_403(db, principal, tutorial_id) -> Tutorial
             _list_tutorials_query(db, principal, claimed_owner_admin_id) -> Query[Tutorial]
PaymentCard: _get_payment_card_or_403(db, principal, card_id) -> PaymentCard
Node:        _get_node_or_403(db, principal, node_id) -> Node
             _scoped_node_ids(db, principal) -> Optional[set[int]]
AdminUser:   _get_admin_by_telegram_or_403(db, principal, telegram_id) -> Optional[AdminUser]
             _get_admin_or_403(db, principal, admin_id) -> AdminUser
```
شمارش: User=۲, Package=۲, Tutorial=۲, PaymentCard=۱, Node=۲, AdminUser=۲ → **۱۱ تابع، نه بیشتر نه کمتر**. `GATED_MODELS = {User, Package, Tutorial, PaymentCard, Node, AdminUser}`.

## ۴. helper نام‌دار fail-closed برای AdminUser hierarchy

```python
def _admin_hierarchy_allowed_ids(db: Session):
    """اگر admin مرتبط با principal حذف/ناموجود شده باشد (رکورد stale)،
    set() خالی برمی‌گردد - نه AttributeError روی db.get(...)==None.
    همین یک تابع هم در مسیر واقعی هم در shadow استفاده می‌شود، پس هر دو
    مسیر یک‌جا این محافظت را می‌گیرند."""
    def _fn(principal: BotPrincipal) -> set[Optional[int]]:
        admin = db.get(models.AdminUser, principal.owner_admin_id)
        if admin is None:
            return set()
        return hierarchy.owned_admin_ids(db, admin) | {principal.owner_admin_id}
    return _fn
```
کاربرد در `_get_admin_by_telegram_or_403`/`_get_admin_or_403`: `require_bot_resource_owner_access(principal, target_admin_id, _admin_hierarchy_allowed_ids(db))`.

## ۵. تست AST - هر دو مسیر (HTTP + in-process) + خودِ فایل سرویس

```python
# tests/test_bot_resource_accessor_ast.py
ALLOWED_FUNCS = {  # همان ۱۱ تابع بخش ۳، دقیقاً - بدون _get_user_by_telegram_or_403 (بدون caller، حذف شد)
    "_get_user_or_403", "_list_users_query",
    "_get_package_or_403", "_list_packages_query",
    "_get_tutorial_or_403", "_list_tutorials_query",
    "_get_payment_card_or_403",
    "_get_node_or_403", "_scoped_node_ids",
    "_get_admin_by_telegram_or_403", "_get_admin_or_403",
}
GATED_MODELS = {"User", "Package", "Tutorial", "PaymentCard", "Node", "AdminUser"}
# (۱) پیمایش AST هر دو فایل - routers/bot.py و telegram_bot/panel_bridge.py - هر
#     Call به db.query(models.X)/db.get(models.X, ...) با X در GATED_MODELS باید
#     داخل بدنه‌ی یکی از ALLOWED_FUNCS باشد (بایپس‌های panel_bridge.py دقیقاً از
#     همین جنس بودند، نه فقط در routers/bot.py).
# (۲) پیمایش AST services/bot_resources.py - نام هر تابع سطح‌بالای این ماژول باید
#     دقیقاً برابر ALLOWED_FUNCS باشد (نه زیرمجموعه، نه ابرمجموعه).
```

## ۶. Route-policy decorator + جدول کامل ۳۸ endpoint

```python
@dataclass(frozen=True)
class RoutePolicy:
    capability: Optional[str]
    resource_strategy: str

def bot_route_policy(capability, resource_strategy):
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, principal: BotPrincipal, **kwargs):
            if capability is None:
                _ensure_valid(principal, fn.__name__)
            else:
                require_bot_capability(principal, capability, endpoint=fn.__name__)
            return fn(*args, principal=principal, **kwargs)
        wrapper._bot_capability = capability
        wrapper._bot_resource_strategy = resource_strategy
        return wrapper
    return decorator
```
`APIRoute.path` شامل prefix کامل روتر است (`/api/bot/...`)؛ جدول پایین برای خوانایی نسبی نوشته شده، تست واقعی هر مسیر را با پیشوند کامل می‌سازد:
```python
routes = [r for r in router.routes if isinstance(r, APIRoute)]
assert len(routes) == 38
CANONICAL = {("GET", "/api/bot" + path): (cap, strat) for path, cap, strat in TABLE_ROWS}
for r in routes:
    method = next(iter(r.methods - {"HEAD"}))
    got = (r.endpoint._bot_capability, r.endpoint._bot_resource_strategy)
    assert got == CANONICAL[(method, r.path)], f"{method} {r.path}: {got} != {CANONICAL[(method, r.path)]}"
```

| # | Method | Path (نسبی به `/api/bot`) | Capability | resource_strategy |
|---|---|---|---|---|
| ۱ | GET | `/nodes` | *(None)* | `node_list` |
| ۲ | GET | `/packages` | `CUSTOMER_READ` | `package_list` |
| ۳ | GET | `/payment-info` | `PAYMENT_READ` | `claim` |
| ۴ | GET | `/payment-cards/{card_id}` | `PAYMENT_READ` | `payment_card_owner` |
| ۵ | POST | `/payment-cards/{card_id}/record-payment` | `PAYMENT_WRITE` | `payment_card_owner` |
| ۶ | GET | `/sales-stats` | `CUSTOMER_READ` | `claim` |
| ۷ | GET | `/customer-menu-config` | *(None)* | `none` |
| ۸ | GET | `/tutorials` | `CUSTOMER_READ` | `tutorial_list` |
| ۹ | GET | `/packages/{package_id}/files/{file_id}/download` | `FILES_READ` | `package_owner` |
| ۱۰ | GET | `/tutorials/{tutorial_id}/media/{media_id}/download` | `FILES_READ` | `tutorial_owner` |
| ۱۱ | GET | `/tutorials/{tutorial_id}/software/{software_id}/download` | `FILES_READ` | `tutorial_owner` |
| ۱۲ | GET | `/admin-by-telegram/{tg_id}` | `ADMIN_LOOKUP` | `admin_hierarchy` |
| ۱۳ | GET | `/admin-username/{admin_id}` | `ADMIN_LOOKUP` | `admin_hierarchy` |
| ۱۴ | GET | `/telegram-user-ids` | `BROADCAST` | `claim` |
| ۱۵ | POST | `/users` | `CUSTOMER_WRITE` | `claim` |
| ۱۶ | POST | `/users/{username}/purchase-package` | `CUSTOMER_WRITE` | `user` |
| ۱۷ | POST | `/referral/apply` | `CUSTOMER_WRITE` | `user` |
| ۱۸ | POST | `/discount/validate` | `CUSTOMER_WRITE` | `claim` |
| ۱۹ | POST | `/discount/redeem` | `CUSTOMER_WRITE` | `claim` |
| ۲۰ | GET | `/users` | `CUSTOMER_READ` | `user_list` |
| ۲۱ | GET | `/users/by-telegram/{telegram_id}` | `CUSTOMER_READ` | `user_list` |
| ۲۲ | GET | `/users/by-telegram/{telegram_id}/all` | `CUSTOMER_READ` | `user_list` |
| ۲۳ | GET | `/users/{username}` | `CUSTOMER_READ` | `user` |
| ۲۴ | POST | `/users/{username}/link-telegram` | `IDENTITY_WRITE` | `user` |
| ۲۵ | POST | `/users/{username}/connections` | `CUSTOMER_WRITE` | `user+node` |
| ۲۶ | GET | `/users/{username}/purchases` | `CUSTOMER_READ` | `user` |
| ۲۷ | POST | `/users/{username}/purchases/{purchase_id}/rename` | `CUSTOMER_WRITE` | `user` |
| ۲۸ | DELETE | `/users/{username}/purchases/{purchase_id}` | `CUSTOMER_WRITE` | `user` |
| ۲۹ | DELETE | `/users/{username}/connections/{connection_id}` | `CUSTOMER_WRITE` | `user` |
| ۳۰ | GET | `/users/{username}/subscription-link` | `CUSTOMER_READ` | `user` |
| ۳۱ | GET | `/miniapp-button-text` | *(None)* | `none` |
| ۳۲ | GET | `/panel-public-url` | *(None)* | `none` |
| ۳۳ | POST | `/users/{username}/purchases/{purchase_id}/renew` | `CUSTOMER_WRITE` | `user` |
| ۳۴ | POST | `/users/{username}/renew` | `CUSTOMER_WRITE` | `user` |
| ۳۵ | POST | `/users/{username}/reset-usage` | `CUSTOMER_WRITE` | `user` |
| ۳۶ | POST | `/users/{username}/set-enabled` | `CUSTOMER_WRITE` | `user` |
| ۳۷ | POST | `/users/{username}/add-balance` | `WALLET_WRITE` | `user` |
| ۳۸ | DELETE | `/users/{username}` | `CUSTOMER_WRITE` | `user` |

`resource_strategy` → accessor: `claim`→`resolve_claimed_owner`؛ `user`→`_get_user_or_403`؛ `user_list`/`package_list`/`tutorial_list`/`node_list`→`_list_*_query`/`_scoped_node_ids`؛ `user+node`→`_get_user_or_403`+`_get_node_or_403`؛ `package_owner`/`tutorial_owner`/`payment_card_owner`→accessor مربوطه (با `require_bot_resource_owner_access` داخلش)؛ `admin_hierarchy`→`_get_admin_by_telegram_or_403`/`_get_admin_or_403`؛ `none`→فقط `_ensure_valid`.

## ۷. Provisioning + preflight package-authorization

```python
def provision_connection(db, user, node, protocol, *, authorization_scope: NodeAuthorizationScope, ...):
    if not authorization_scope.unrestricted:
        admin = db.get(models.AdminUser, authorization_scope.admin_id)
        allowed = hierarchy.selling_scope_node_ids(db, admin) if admin else set()
        if allowed is not None and node.id not in allowed:
            raise HTTPException(403, f"سرور «{node.name}» در اختیار این مجموعه نیست")
    ...
```
**Call siteها:** bundled (`provision_package_connections`/`apply_package_as_purchase`) → `resolve_package_authorization_scope(package)`، فقط اگر `PanelSettings.package_node_scope_enforced=True` باشد (بخش ۲ - وگرنه `unrestricted_scope()` صریح، صفر تغییر رفتار). دستی/بات (`node_id` صریح) → `resolve_bot_authorization_scope(principal)` (خودش `is_scoped` را چک می‌کند، در C0 همیشه unrestricted). superadmin مستقیم → `resolve_superadmin_actor_scope(admin)`.

```python
# backend/scripts/preflight_package_node_scope.py - read-only، هیچ‌چیز رد نمی‌کند
for pc in db.query(models.PackageConnection).all():
    package = db.get(models.Package, pc.package_id)
    scope = resolve_package_authorization_scope(package)
    if scope.unrestricted:
        continue
    admin = db.get(models.AdminUser, scope.admin_id)
    allowed = hierarchy.selling_scope_node_ids(db, admin) if admin else set()
    if allowed is not None and pc.node_id not in allowed:
        print(f"MISMATCH package={package.id} node={pc.node_id} owner={scope.admin_id}")
```
**اصلاح این دور - allowlist حذف شد.** نسخه‌ی قبل یک `PackageConnection.scope_exception_note` پیشنهاد داده بود که قرار بود توسط `hierarchy.selling_scope_node_ids(db, admin)` خوانده شود - ولی آن تابع فقط `db, admin` می‌گیرد، هیچ‌وقت خودِ ردیف `PackageConnection` را نمی‌بیند، پس اصلاً نمی‌توانست چنین استثنایی را اعمال کند (لایه‌ی اشتباه: استثنا مال یک `pc` خاص است، تابع عمومی سطح‌tenant است). به‌جای طراحی یک مسیر استثنا در لایه‌ی درست (validator خودِ `pc`، با `reason`/`created_by`/`created_at`) که پیچیدگی و سطح حمله‌ی تازه‌ای باز می‌کند، **گزینه‌ی امن‌تر برای همین Phase انتخاب شد: تنها یک راه واقعی برای بستن هر mismatch وجود دارد -**
- **اصلاح داده** - grant صحیح (`AdminNodeAccess` جدید) یا اصلاح `PackageConnection`/`owner_admin_id` پکیج، تا preflight خودش خالی شود.
هیچ allowlist/استثنای کدی در این Phase وجود ندارد. اگر در آینده نیاز واقعی به استثنا دیده شد، طراحی جدا و مستقل خودش را می‌خواهد (validator سطح `PackageConnection`، نه تابع hierarchy عمومی) - خارج از دامنه‌ی همین سند.

**اصلاح این دور (نوبت اول) - تناقض بین `_sync_connections` و وعده‌ی «C0 بدون تغییر رفتار».** نسخه‌ی قبل `_sync_connections` را «مستقل از `package_node_scope_enforced`، همیشه فعال» توصیف کرده بود، در حالی که همین بخش (بالاتر) و بخش ۱۰ (rollout) هر دو ادعا می‌کردند C0 صفر تغییر رفتار دارد تا وقتی همان یک flag فعال شود. تصمیم صریح: **`_sync_connections` هم از همان flag پیروی می‌کند، نه مستقل.**

**اصلاح این دور (نوبت دوم، P1) - pseudocode قبلی دو نام تعریف‌نشده داشت.** امضای واقعی `_sync_connections(db, pkg, specs)` هیچ پارامتر `admin`ای ندارد (سازنده‌ی پکیج در تابع `create_package`/`update_package` است، نه اینجا) و `_get_or_create_settings_readonly` هیچ‌جا تعریف نشده بود - اجرای literal این کد حتی با flag خاموش روی اولین ساخت/ویرایش پکیج مستقیم `NameError` می‌دهد. اصلاح: scope از خودِ `pkg` با همان `resolve_package_authorization_scope` ساخته می‌شود (که فقط `pkg.owner_admin_id` می‌خواهد، نه `admin`).

**اصلاح این دور (نوبت سوم، P1) - `BEGIN IMMEDIATE` فقط روی SQLite معتبر است، و یک قفل تک‌طرفه TOCTOU را کامل نمی‌بندد.** این پروژه رسماً MySQL/MariaDB هم پشتیبانی می‌کند (`README.md`'s «انتخاب نوع دیتابیس»، `install.sh`'s `USERMANAGER_DB_ENGINE=mariadb|mysql-external`، `backend/app/database.py`'s `is_sqlite`/`is_mysql`) - `BEGIN IMMEDIATE` روی MySQL یک خطای syntax است، نه فقط بی‌اثر؛ endpoint فعال‌سازی را کامل می‌شکند. علاوه بر آن، حتی روی SQLite، اگر فقط endpoint فعال‌سازی قفل بگیرد ولی `_sync_connections` صرفاً flag را می‌خواند (بدون قفل مشترک)، یک race واقعی می‌ماند: نویسنده‌ی پکیج می‌تواند `flag=false` را بخواند، فعال‌سازی موازی validation+commit کند، و نویسنده بر اساس تصمیم stale یک mapping نامعتبر را بعداً بنویسد.

**اصلاح واحد برای هر سه نوبت - یک guard تراکنشی مشترک، dialect-aware، که هم فعال‌سازی هم هر writer پکیج از همان عبور می‌کنند:**
```python
# routers/panel_settings.py (یا services/hierarchy.py - محل دقیق مهم نیست، فراخوانی مشترک است)
from ..database import is_sqlite

def _lock_package_node_scope_settings(db: Session) -> models.PanelSettings:
    """قفل نوشتن لازم برای بستن TOCTOU، دیالکت‌آگاه - هم activation هم هر
    writer‌ی PackageConnection باید از همین یک تابع رد شوند، نه دو مسیر جدا:
      - SQLite: BEGIN IMMEDIATE - کل دیتابیس را برای نوشتن قفل می‌کند (سنگین‌تر،
        ولی برای این پروژه قابل‌قبول: ادیت پکیج یک عمل کم‌تکرار ادمین است، نه
        یک مسیر پرترافیک مشتری - قفل فقط طول همین یک تراکنش کوتاه است).
      - MySQL/MariaDB: SELECT ... FOR UPDATE روی همان ردیف تکی PanelSettings
        (id=1) - قفل سطح-ردیف، سبک‌تر، کافی چون این تنظیمات یک singleton است.
    هر دو مسیر، اگر ردیف هنوز نساخته شده، آن را با flush (نه commit) می‌سازند
    - یک commit نهایی واحد بیرون از این تابع، توسط caller، انجام می‌شود."""
    if is_sqlite:
        db.execute(text("BEGIN IMMEDIATE"))
        row = db.get(models.PanelSettings, 1)
    else:
        row = db.execute(
            select(models.PanelSettings).where(models.PanelSettings.id == 1).with_for_update()
        ).scalar_one_or_none()
    if row is None:
        row = models.PanelSettings(id=1)
        db.add(row)
        db.flush()
    return row
```
**اصلاح این دور (نوبت چهارم، P1) - ترتیب guard/DML اشتباه بود، و commit داخل `_sync_connections` atomicity را می‌شکست.** تایید شد با خواندن کد واقعی (`routers/packages.py:271-354`):
- `create_package` قبل از `_sync_connections` یک `db.add(pkg); db.flush()` می‌زند (برای گرفتن `pkg.id`) - این خودش یک تراکنش SQLAlchemy را ضمنی (`autobegin`) باز می‌کند؛ اگر `_lock_package_node_scope_settings` **داخل** `_sync_connections` صدا زده شود، `BEGIN IMMEDIATE`اش روی SQLite با `OperationalError: cannot start a transaction within a transaction` می‌شکند - یعنی همان لحظه‌ی deploy، ساخت هر پکیج (نه فقط پکیج‌های scope-دار) کرش می‌کند. `update_package` حتی زودتر می‌شکند (`_get_scoped_package` خودش یک SELECT/تراکنش ضمنی است، قبل از هر `_sync_connections`ای).
- `_sync_connections` واقعی امروز **هیچ‌وقت commit نمی‌کند** - caller (`create_package`/`update_package`) دقیقاً یک `db.commit()` نهایی، بعد از `_sync_ovpn_templates` هم، می‌زند. `db.commit()`ی که در نسخه‌ی قبل داخل `_sync_connections` گذاشته شده بود، `pkg`+`PackageConnection`+ردیف تازه‌ساخته‌شده‌ی `PanelSettings` را زودتر دائمی می‌کرد؛ اگر `_sync_ovpn_templates` بعدش خطا بدهد، آن commit قبلی دیگر rollback نمی‌شود - یک پکیج نیمه‌کاره (connections ذخیره‌شده، templates نه) که امروز اصلاً ممکن نیست.

**اصلاح - guard اولین دستور DB در خودِ endpoint می‌شود، نه داخل `_sync_connections`؛ `_sync_connections` بدون commit، فقط یک پارامتر `enforce_scope: bool` می‌گیرد:**
```python
# routers/packages.py:_sync_connections
def _sync_connections(db: Session, pkg: models.Package, specs: list[schemas.PackageConnectionSpec],
                       *, enforce_scope: bool) -> None:
    if enforce_scope:
        scope = resolve_package_authorization_scope(pkg)   # از خودِ pkg، نه از admin تعریف‌نشده
        if not scope.unrestricted:
            owner = db.get(models.AdminUser, scope.admin_id)
            allowed = hierarchy.selling_scope_node_ids(db, owner) if owner else set()
            for spec in specs:
                if allowed is not None and spec.node_id not in allowed:
                    raise HTTPException(400, f"سرور (id={spec.node_id}) در اختیار شما نیست")
    for spec in specs:
        ... (بررسی وجود نود/تطابق پروتکل امروزی، بدون تغییر)
    db.query(models.PackageConnection).filter(models.PackageConnection.package_id == pkg.id).delete()
    for spec in specs:
        db.add(models.PackageConnection(package_id=pkg.id, node_id=spec.node_id, protocol=spec.protocol, flow=spec.flow or ""))
    # بدون commit - دقیقاً مثل امروز.

def create_package(payload, db=Depends(get_db), admin=Depends(get_current_admin)):
    settings = _lock_package_node_scope_settings(db)   # اولین دستور DB در کل تابع - قبل از هر db.add/flush/query
    _require_package_manager(admin)
    ...
    pkg = models.Package(**data)
    db.add(pkg)
    db.flush()
    _sync_connections(db, pkg, payload.connections, enforce_scope=settings.package_node_scope_enforced)
    _sync_ovpn_templates(db, pkg, payload.ovpn_templates)
    db.commit()   # تنها commit - pkg + connections + templates + (اگر تازه ساخته شد) ردیف PanelSettings، همه یک واحد
    db.refresh(pkg)
    return _out(pkg)

def update_package(package_id, payload, db=Depends(get_db), admin=Depends(get_current_admin)):
    settings = _lock_package_node_scope_settings(db)   # اولین دستور DB - قبل از _get_scoped_package
    _require_package_manager(admin)
    pkg = _get_scoped_package(db, package_id, admin)
    ...
    if payload.connections is not None:
        _sync_connections(db, pkg, payload.connections, enforce_scope=settings.package_node_scope_enforced)
    if payload.ovpn_templates is not None:
        _sync_ovpn_templates(db, pkg, payload.ovpn_templates)
    db.commit()
    db.refresh(pkg)
    return _out(pkg)
```

**اثر روی TOCTOU (بدون تغییر در نتیجه، فقط در محل گرفتن قفل):** حالا هر دو مسیر روی همان قفل صف می‌کشند - هرکدام اول commit کند، دیگری تصمیمش را روی وضعیت **بعد از** آن commit می‌گیرد، نه یک snapshot قدیمی:
- writer اول commit کند (وقتی flag هنوز `false` بوده، بدون بررسی نوشته) → activation، بعد از گرفتن قفل، preflight را دوباره روی داده‌ی **تازه‌نوشته‌شده** اجرا می‌کند → همان mismatch تازه را می‌بیند → ۴۰۹، فعال‌سازی رد می‌شود.
- activation اول commit کند (flag به `true` رفت، بدون mismatch) → writer، بعد از گرفتن قفل، `settings.package_node_scope_enforced` را `True` می‌بیند → چک همان لحظه‌اش نود نامربوط را رد می‌کند.
هیچ حالت سومی (هر دو موفق با یک ناسازگاری باقی‌مانده) ممکن نیست.

**هزینه‌ی صریح این تصمیم:** هر ساخت/ویرایش پکیج، صرف‌نظر از اینکه flag فعال است یا نه، از این‌جا به بعد یک `BEGIN IMMEDIATE`/`SELECT FOR UPDATE` کوتاه می‌گیرد - یک قفل نوشتن گسترده (SQLite) یا سطح‌ردیف (MySQL) برای طول یک تراکنش ادیت پکیج، نه یک هزینه‌ی دائمی روی مسیرهای مشتری/بات. این هزینه صریحاً پذیرفته شد.

**تست regression تازه (اضافه به فهرست بخش ۱۱):** یک خطای عمدی داخل `_sync_ovpn_templates` (بعد از `_sync_connections`ی موفق، قبل از `db.commit()`) باید کل ویرایش پکیج - هم فیلدهای خودِ `pkg`، هم `PackageConnection`های تازه‌جایگزین‌شده - را rollback کند؛ این امروز هم رفتار موجود است (یک commit نهایی مشترک)، تست فقط تایید می‌کند threading کردن `enforce_scope`/guard آن را نشکسته.

`routers/users.py`'s سه endpoint دستی connections (`_get_user_and_node`) دست‌نخورده می‌مانند - از قبل با `hierarchy.accessible_node_ids(db, admin)` روی caller خودشان درست عمل می‌کنند (این مسیر اصلاً به `package_node_scope_enforced` وابسته نیست، جزو دامنه‌ی این Phase هم نبود).

## ۸. `panel_bridge.py`

- **`_scope(owner_admin_id)` برای همیشه می‌ماند - هرگز حذف نمی‌شود** (یک تابع سراسری مشترک بین همه‌ی ربات‌های اختصاصی است، در حالی که C3 per-owner فعال می‌شود؛ حذفش بعد از فعال‌شدن اولین owner، پیش‌فرض claim را از هر ربات اختصاصی دیگرِ هنوز `dedicated_bot_scope_enforced=false` می‌گیرد و آن‌ها را global می‌کند - enforcement واقعی کاملاً جدا توسط `resolve_claimed_owner` انجام می‌شود).
- هر متد یک `BotPrincipal.internal(config.bot_owner_admin_id, scope_enforced=<AdminUser.dedicated_bot_scope_enforced>)` هم می‌سازد، **علاوه بر** `_scope()`.
- `get_package_files`/`get_tutorial_media`/`get_tutorial_software_file`/`get_admin_telegram_id`/`get_admin_username` به accessor مربوطه‌ی `services/bot_resources.py` (بخش ۳) وصل می‌شوند - همان تابعی که `routers/bot.py` هم صدا می‌زند.

## ۹. Activation اتمیک + UI

```python
@router.post("/{key_id}/activate", dependencies=[Depends(require_confirm_password)])
def activate_tenant_key(key_id: int, db: Session = Depends(get_db)):
    result = db.execute(
        update(models.ApiKey.__table__)
        .where(models.ApiKey.id == key_id, models.ApiKey.key_type == KeyType.TENANT_INTEGRATION,
               models.ApiKey.enabled == False)  # noqa: E712
        .values(enabled=True, scope_enforced=True)
    )
    db.commit()
    if result.rowcount == 0:
        raise HTTPException(409, "کلید در وضعیت قابل‌فعال‌سازی نیست")
    return {"ok": True}
```
**اصلاح این دور (نوبت اول) - فعال‌سازی از preflight جدا و مستعد race بود.** نسخه‌ی قبل اسکریپت read-only را ابزار «مشاهده» می‌دانست و endpoint فعال‌سازی هیچ validationای دوباره اجرا نمی‌کرد - یعنی (۱) کسی می‌توانست بدون اجرای preflight مستقیم endpoint را بزند، (۲) بین اجرای preflight و کلیک فعال‌سازی، یک `PackageConnection` تازه/ویرایش‌شده می‌توانست mismatch تازه‌ای بسازد که دیگر دیده نشده. اصلاح: خودِ همان validation، **داخل یک تراکنش نوشتنی** و بلافاصله قبل از `UPDATE`، توسط endpoint اجرا می‌شود.

**اصلاح این دور (نوبت دوم، جایگزین شد) - همان قفل مشترک `_lock_package_node_scope_settings` بخش ۷ (dialect-aware، بدون commit داخلی) اینجا هم استفاده می‌شود، نه یک نسخه‌ی جدا:**
```python
# routers/panel_settings.py
@router.put("/package-node-scope", dependencies=[Depends(require_superadmin), Depends(require_confirm_password)])
def enable_package_node_scope(db: Session = Depends(get_db)):
    settings = _lock_package_node_scope_settings(db)   # همان تابع بخش ۷ - قفل + گرفتن/ساختن ردیف، بدون commit
    mismatches = _find_package_node_scope_mismatches(db)   # همان منطق preflight، به یک تابع مشترک منتقل شد
    if mismatches:
        db.rollback()
        raise HTTPException(409, f"{len(mismatches)} ناهماهنگی هنوز حل نشده - preflight را دوباره اجرا کنید")
    settings.package_node_scope_enforced = True
    db.commit()   # یک commit نهایی، بعد از validation و بعد از UPDATE
    return {"ok": True}

@router.put("/package-node-scope/disable", dependencies=[Depends(require_superadmin), Depends(require_confirm_password)])
def disable_package_node_scope(db: Session = Depends(get_db)):
    # اصلاح متنی این دور: غیرفعال‌سازی enforcement را بازتر می‌کند، نه
    # محدودتر - دقیقاً برای همین (چون فقط دسترسی را باز می‌کند، هیچ حالت
    # میانی ناامن‌تر از امروز نمی‌سازد) نیازی به قفل/validation ندارد؛ همان
    # _get_or_create امروزی کافی است.
    settings = _get_or_create(db)
    settings.package_node_scope_enforced = False
    db.commit()
    return {"ok": True}

@router.get("/package-node-scope", dependencies=[Depends(get_current_admin)])
def get_package_node_scope_status(db: Session = Depends(get_db)):
    return {"enabled": _get_or_create(db).package_node_scope_enforced}
```
`_find_package_node_scope_mismatches(db)` همان حلقه‌ی preflight (بخش ۷ بالا) است، به یک تابع مشترک در `services/hierarchy.py`/`bot_auth.py` منتقل شده تا preflight script و endpoint فعال‌سازی **یک** پیاده‌سازی را اجرا کنند، نه دو نسخه‌ی جدا که می‌توانند از هم جدا شوند.

+ `client.js`'s `activateApiKey`/`enablePackageNodeScope`/`disablePackageNodeScope`/`getPackageNodeScopeStatus`، `Settings.jsx`'s سه دکمه (فعال‌سازی/غیرفعال‌سازی/نمایش وضعیت واقعی نصب)، `translations.js`'s کلید fa/en تازه.

## ۱۰. Rollout C0-C4

- **C0 - Wiring/Preflight + Shadow (یک commit، همه‌جا یکسان deploy می‌شود).** `deps.get_bot_principal`؛ `principal`+`@bot_route_policy` روی هر ۳۸ endpoint؛ `panel_bridge.py` هم‌زمان (`_scope()` دائمی)؛ ۱۱ accessor + هر دو تست AST/coverage؛ `provision_connection`/`_sync_connections` با `authorization_scope` (bot-authorization بخش همین‌جا فعال چون خودش `is_scoped`-gated است؛ package-authorization فقط کد را می‌آورد، `PanelSettings.package_node_scope_enforced` پیش‌فرض `False` در همه‌ی نصب‌ها - صفر تغییر رفتار)؛ `AdminUser.dedicated_bot_scope_enforced` پیش‌فرض `False`. نتیجه: صفر تغییر رفتار برای هر کلید/بات/نصب موجود.
- **فعال‌سازی per-installation (مستقل از C0، هر نصب هر زمان که خودش آماده بود):** اپراتور هر نصب اسکریپت preflight (بخش ۷) را روی دیتابیس خودش اجرا می‌کند؛ اگر خالی بود (یا بعد از پاک‌سازی خالی شد)، `PUT /api/settings/package-node-scope` را می‌زند.
- **C1 -** یک کلید تست با `activate_tenant_key`.
- **C2 -** فعال‌سازی تدریجی کلیدهای HTTP واقعی.
- **C3 -** فعال‌سازی تدریجی per-owner in-process (`UPDATE admin_users SET dedicated_bot_scope_enforced=true WHERE id=...`). `_scope()` هرگز حذف نمی‌شود.
- **C4 -** ارتقای انتخابی کلید legacy، فقط با درخواست صریح صاحبش.
- **Rollback:** HTTP فقط `enabled=false`؛ in-process فقط `dedicated_bot_scope_enforced=false`؛ package-authorization فقط `package_node_scope_enforced=false` - هرکدام مستقل، هیچ‌کدام به `_scope()` کاری ندارد.

## ۱۱. فهرست کامل و نهایی فایل‌ها

| # | فایل | تغییر |
|---|---|---|
| ۱ | `backend/app/services/hierarchy.py` | `selling_scope_node_ids(db, admin)` |
| ۲ | `backend/app/services/bot_auth.py` | invariant §۱؛ `_log_decision` با پارامتر واقعی `would_allow_if_enforced` (§۱ - جایگزین امضای امروزی که `extra` ندارد)؛ `NodeAuthorizationScope`+۳ resolver با اعتبارسنجی؛ `_shadow_would_allow`؛ ۵ تابع مرکزی؛ `_admin_hierarchy_allowed_ids`؛ `RoutePolicy`/`bot_route_policy` |
| ۳ | `backend/app/services/bot_resources.py` (تازه) | دقیقاً ۱۱ accessor §۳ - نه بیشتر نه کمتر (تضمین‌شده با تست §۵)، هرکدام با `claimed_owner_admin_id` اجباری جایی که لازم است |
| ۴ | `backend/app/services/user_ops.py` | `provision_connection` با `authorization_scope` اجباری (§۷) |
| ۵ | `backend/app/routers/packages.py` | `create_package`/`update_package` گارد `_lock_package_node_scope_settings` (§۷) را اولین دستور DB خودشان می‌کنند (قبل از `db.add`/`db.flush`/`_get_scoped_package`)؛ `_sync_connections` بدون commit می‌ماند، فقط `enforce_scope: bool` می‌گیرد و scope را از خودِ `pkg` با `resolve_package_authorization_scope` می‌سازد |
| ۶ | `backend/app/models.py` | `AdminUser.dedicated_bot_scope_enforced` + `PanelSettings.package_node_scope_enforced` (هر دو additive، پیش‌فرض `False`) |
| ۷ | `backend/app/deps.py` | `get_bot_principal` |
| ۸ | `backend/app/routers/bot.py` | `@bot_route_policy`+`principal` روی هر ۳۸ endpoint (§۶)؛ استفاده از `bot_resources.py` |
| ۹ | `backend/app/routers/api_keys.py` | `activate_tenant_key` (§۹) |
| ۱۰ | `backend/app/routers/panel_settings.py` | `enable_package_node_scope`/`disable_package_node_scope`/`get_package_node_scope_status` (§۹)، `_lock_package_node_scope_settings` dialect-aware (§۷ - همان تابع، از `create_package`/`update_package` هم صدا زده می‌شود)، `_find_package_node_scope_mismatches` مشترک |
| ۱۱ | `backend/app/telegram_bot/panel_bridge.py` | `_scope()` دائمی + principal اضافه (§۸)؛ ۵ تابع به `bot_resources.py` وصل |
| ۱۲ | `backend/scripts/preflight_package_node_scope.py` (تازه) | §۷ - read-only، فقط برای مشاهده‌ی زودهنگام؛ گیت واقعی داخل §۹ است، نه اینجا |
| ۱۳ | `frontend/src/api/client.js` | `activateApiKey`, `enablePackageNodeScope`, `disablePackageNodeScope`, `getPackageNodeScopeStatus` |
| ۱۴ | `frontend/src/pages/Settings.jsx` | دکمه‌ی فعال‌سازی کلید tenant + سه دکمه/نمایشگر وضعیت package-node-scope |
| ۱۵ | `frontend/src/i18n/translations.js` | کلیدهای fa/en تازه |
| ۱۶ | `backend/tests/test_bot_route_policy_coverage.py` (تازه) | §۶، با نرمال‌سازی مسیر `/api/bot` |
| ۱۷ | `backend/tests/test_bot_resource_accessor_ast.py` (تازه) | §۵ - هر دو فایل + self-check `bot_resources.py` |
| ۱۸ | `backend/tests/test_api_key_phase_b.py` یا فایل تازه | lifecycle کامل هر سه endpoint (`enable`/`disable`/`status`) + `activate_tenant_key` - سناریوهای اجباری: (۱) mismatch واقعی → `enable` با ۴۰۹ رد می‌شود و flag دیتابیس `False` می‌ماند؛ (۲) دیتابیس بدون ردیف `PanelSettings` از قبل → `enable` آن را می‌سازد و در همان یک تراکنش فعال می‌کند؛ (۳) `enable` بدون mismatch → موفق با دقیقاً یک commit نهایی (نه دو)؛ (۴) `disable` روی یک flag از قبل فعال؛ (۵) `status` بعد از هر یک از تغییرات بالا مقدار واقعی را برمی‌گرداند؛ (۶) **رقابت واقعی دو Session، از طریق endpoint واقعی** - یک Session `POST /api/packages` یا `PUT /api/packages/{id}` واقعی را با یک node نامربوط صدا می‌زند (نه فراخوانی مستقیم guard/`_sync_connections` بیرون از مسیر HTTP)، هم‌زمان با یک Session دیگر که `PUT /api/settings/package-node-scope` را می‌زند؛ خروجی مجاز فقط یکی از دو حالت («نویسنده اول commit، فعال‌سازی با ۴۰۹ رد می‌شود» یا «فعال‌سازی اول commit، نویسنده با ۴۰۰ رد می‌شود») - هیچ حالت سومی (هر دو موفق) قابل‌مشاهده نیست؛ (۶ب) **regression atomicity** - خطای عمدی در `_sync_ovpn_templates` بعد از `_sync_connections`ی موفق باید کل `create_package`/`update_package` (شامل `PackageConnection`های تازه) را rollback کند؛ (۷) **تست dialect/SQL compilation** برای هر دو مسیر SQLite و MySQL/MariaDB روی `_lock_package_node_scope_settings` - تضمین می‌کند `BEGIN IMMEDIATE` هرگز به مسیر MySQL نمی‌رود و `SELECT ... FOR UPDATE` هرگز به مسیر SQLite نمی‌رود |
| ۱۹ | `backend/tests/test_bot_auth_principal.py` | بازنویسی کامل مطابق §۱-۴، شامل تست `resolve_superadmin_actor_scope`/`resolve_bot_authorization_scope` روی principal نامعتبر/admin غیرسوپرادمین، `NodeAuthorizationScope.__post_init__`، و `_admin_hierarchy_allowed_ids` روی owner حذف‌شده |
| ۲۰ | `backend/tests/test_bot_inprocess_trust_characterization.py` | نسخه‌ی معکوس بعد از C3 + تست «`_scope()` بعد از فعال‌شدن یک owner، برای owner دیگر هنوز پیش‌فرض را می‌گذارد» |
| ۲۱ | مستند این فایل | گزارش preflight هر نصب + گزارش بعد از هر مرحله |

هیچ‌کدام اعمال نشده - صفر کد production، صفر commit/push در این دور هم. رمز مادر لمس نشد.

### وضعیت

اجرای C0 همچنان متوقف است تا همین نسخه‌ی هشتم بازبینی/تایید شود.

**اصلاح یک ادعای این سند (بازبینی نهم Product):** «کل سند بدون نسخه‌های قبلی بازنویسی شد» نادرست بود - بخش‌های نسخه‌ی اول تا هفتم هنوز عیناً بالای همین بخش در همین فایل هستند (نگه‌داشته شده به‌عنوان تاریخچه‌ی بازبینی، طبق همان رویه‌ای که از ابتدای این سند دنبال شده). ادعای درست: **بخش «Phase C - نسخه‌ی هشتم» خودش، به‌تنهایی، کامل و بدون هیچ ارجاع داخلی به نسخه‌های قبل است** - همان چیزی که تنها مرجع اجرا بودنش را تضمین می‌کند؛ نه اینکه فایل هیچ متن قدیمی‌تری نداشته باشد. اگر جداسازی نسخه‌های تاریخی به یک سند archive جدا مطلوب باشد، این یک تغییر ساختاری بدون اثر امنیتی است و فقط با درخواست صریح انجام می‌شود، نه خودکار در همین‌جا.
