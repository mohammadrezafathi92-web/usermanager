# طراحی Canonical نسخه‌ی ۷.۱ (frozen): Lifecycle و Provisioning پایدار (فاز P6) — ۲۰۲۶-۱۰-۰۳

**وضعیت سند:** v7.1، **frozen**. جایگزین کامل نسخه‌های قبلی همین فایل است و برای فهم آن خواندن نسخه‌ی قبلی لازم نیست.
**وضعیت کد:** این سند طراحی است؛ پیاده‌سازی به ترتیب فازهای بخش ۱۶ و از L0 شروع می‌شود.
**رابطه با `docs/receipt-void-design-2026-10-02.md` (v16.1، frozen):** این سند فاز **P6** آن سند را طراحی می‌کند و قرارداد بخش ۱۲.۳ آن را پیاده می‌کند. آن سند تغییر نمی‌کند. هر جا این سند به جدولی از آن تکیه دارد (`resource_locks`، `wallet_runtime_state`، `receipt_approval_runtime_state`، `wallet_debit_events`، `receipt_approvals`، `receipt_approval_effects`، `receipt_approval_expected_effects`، `wallet_operations` از نوع `approval_recovery`)، قرارداد لازم همین‌جا تکرار شده تا سند مستقل خوانده شود. نگاشت کامل در بخش ۱۴؛ سه نقطه‌ای که تفسیر این سند از متن frozen باید تأیید شود در ۷.۶ علامت خورده‌اند.
**مرجع یافته‌ها:** `backend/tests/test_lifecycle_provisioning_analysis.py` (commit `ce518a1`).
**رمز مادر:** کاملاً خارج از scope.

---

## فهرست

0. Scope و ممنوعیت‌ها
1. Policyهای ثابت
2. Inventory واقعی کد فعلی
3. معماری و اجزا
4. قراردادهای Schema
5. Schema
6. State machine عملیات و گام
7. مرز تراکنش‌ها
8. Reservation: reserve / capture / release
9. Adapter هر backend و idempotency
10. قفل‌ها و fencing
11. Recovery پس از crash و compensation
12. حذف (deprovision) با همان ماشین
13. API و سازگاری caller
14. نگاشت به قرارداد ۱۲.۳ Receipt Void
15. HA
16. Rollout / Rollback / Drain
17. Test Matrix
18. تصمیم‌های باز Product
19. خارج از Scope

---

## ۰. Scope و ممنوعیت‌ها

**داخل scope:** همه‌ی مسیرهایی که سرویس می‌سازند یا تمدید می‌کنند و برایش پول می‌گیرند: ساخت User، خرید پکیج، افزودن پکیج از پنل، ساخت گروهی، افزودن اتصال، تمدید User و تمدید Purchase؛ و حذف اتصال/Purchase/User.

**ممنوع:**

- ادعای atomicity بین دیتابیس و شبکه. هر فراخوانی remote بین دو commit محلی است و crash بین آن‌ها باید recovery تعریف‌شده داشته باشد.
- «اول remote، بعد تراکنش محلی» بدون ثبت durable قبلی.
- compensation فقط در حافظه (`try/except` و refund بهترین‌تلاش).
- commit داخلی در توابع سازنده، وقتی در مسیر durable صدا زده می‌شوند.
- حذف کورکورانه‌ی منبع remote بدون تطبیق هویت کامل.
- secret در JSON، log، error یا response.
- تکیه بر ترتیب relationship، بر `id` عددی قابل‌استفاده‌ی مجدد، یا بر UNIQUE روی ستون nullable (جز جایی که صریحاً «NULL-exempt عمداً» نوشته شده).
- فرض «همه‌ی instanceها یک دیتابیس هم‌زمان می‌بینند» (بخش ۱۵).
- خارج از threat model: کسی که دسترسی نوشتن مستقیم به دیتابیس دارد.

---

## ۱. Policyهای ثابت

1. **all-or-nothing:** یک عملیات یا همه‌ی اتصال‌های خواسته‌شده را می‌سازد و پول را capture می‌کند، یا هیچ‌چیز نمی‌ماند و پول آزاد می‌شود. تحویل ناقص با شارژ کامل وجود ندارد.
2. **reserve قبل از هر remote، capture فقط با ثبت فروش:** پول (کیف‌پول مشتری و اعتبار reseller) در اولین تراکنش رزرو می‌شود و فقط در همان commit که Purchase، Connectionها و LedgerEntry فروش ساخته می‌شوند capture می‌شود.
3. **ثبت durable قبل از remote:** هویت و credential هر اتصال قبل از فراخوانی remote در دیتابیس commit شده است.
4. **roll-forward اول:** عملیاتی که بعد از crash پیدا می‌شود تا مهلت مشخص به جلو کامل می‌شود؛ بعد از مهلت یا با خطای قطعی، compensate می‌شود (بخش ۱۱).
5. **پول در `cleanup_required` آزاد نمی‌شود:** تا نبودن منبع remote اثبات یا با force پذیرفته نشده، reservation می‌ماند.
6. **یک عملیات، یک `business_key`:** جلوگیری از تکرار به actor وابسته نیست.

---

## ۲. Inventory واقعی کد فعلی

همه با خواندن مستقیم `backend/app` تأیید شده‌اند.

### ۲.۱ مسیرهای ساخت و خرید

| مسیر | محل | ترتیب امروز |
|---|---|---|
| بات: ساخت User | `routers/bot.py:791` `create_user` | `create_user_record` (commit) → هر اتصال `provision_connection` (remote + commit) → `absorb_legacy_pool_into_purchase` (فقط `if payload.connections`، ۸۵۵) → `_charge_seller` → `_record_bot_sale` → commit |
| بات: خرید پکیج | `routers/bot.py:875` `purchase_package` | `apply_package_as_purchase` (remote + commit داخلی) → `_charge_seller` → `_record_bot_sale` → commit |
| بات: تمدید | `routers/bot.py:1265` `renew_service`، `:1314` `renew` | تمدید DB → شارژ → ثبت فروش |
| بات: افزودن اتصال | `routers/bot.py:1090` `add_connection` | `provision_connection` |
| پنل: ساخت User | `routers/users.py:405` `create_user` | **`charge_for_package` (commit) قبل از ساخت User** → `models.User(...)` commit → `provision_package_connections` → در شکست `refund_for_package` |
| پنل: ساخت گروهی | `routers/users.py:269` → `services/user_ops.py:657` | شارژ کل batch → هر User جدا → refund در استثنا |
| پنل: افزودن پکیج | `routers/users.py:974` `apply_package` | شارژ → `apply_package_as_purchase` → refund در شکست |
| پنل: تمدید Purchase | `routers/users.py:1059` | شارژ → `renew_purchase` |
| مینی‌اپ و بات: خرید با کیف‌پول | `routers/miniapp.py:598`، `telegram_bot/handlers/customer_purchase.py:530` | `add_balance(-price)` با commit مستقل → خرید → در استثنا `add_balance(+price)` بهترین‌تلاش (۶۳۴، ۶۰۳) |

### ۲.۲ توابع provisioning (`services/user_ops.py`)

| تابع | خط | رفتار |
|---|---|---|
| `_wg_reserve_ips` | ۹۰۰ | IP آزاد را فقط از ردیف‌های `Connection` موجود حساب می‌کند (`_wg_used_ips`، ۸۶۷). اگر subnet پر باشد آن را دو برابر و `node.mt_client_subnet` را **همان‌جا commit** می‌کند (۹۱۹-۹۲۰) |
| `provision_wireguard` | ۹۳۹ | کلید در حافظه ساخته می‌شود (۹۶۳)، نام peer با hex تصادفی (۹۶۴)، سپس `add_peer` روی router (۹۷۷) و queue (۹۸۴)، **و بعد** ردیف `Connection` commit می‌شود (۱۰۰۱-۱۰۰۲) |
| `_provision_ppp` | ۱۰۰۸ | فقط DB: username و password ساخته و commit می‌شود؛ هیچ فراخوانی remote (احراز با RADIUS همین پنل) |
| `provision_xray` | ۱۱۳۰ | email با hex تصادفی (۱۱۴۱)؛ `add_client` **بدون** uuid صدا زده می‌شود و uuid را خود client می‌سازد و برمی‌گرداند (۱۱۴۴)؛ سپس `Connection` commit |
| `provision_softether` | ۱۱۶۵ | username و password در حافظه (۱۱۸۸-۱۱۸۹)؛ `add_client` remote (۱۱۹۲)؛ سپس `Connection` commit |
| `provision_package_connections` | ۱۲۲۶ | روی `package.connections` بدون `order_by` حلقه می‌زند (۱۲۴۶)؛ شکست هر اتصال را در `skipped` می‌گذارد و ادامه می‌دهد |
| `apply_package_as_purchase` | ۱۳۶۹ | `absorb` → Purchase (flush) → حلقه‌ی اتصال‌ها با تحمل شکست جزئی (۱۴۵۸-۱۴۷۳)؛ فقط اگر **همه** شکست بخورند rollback (۱۴۸۶)؛ commit داخلی (۱۴۹۳) |
| `deprovision_connection` | ۱۶۴۸ | WireGuard: peer را فقط با `comment` پیدا می‌کند؛ اگر نبود warning و ادامه (۱۶۶۲-۱۶۷۷). Xray: `remove_client`. SoftEther: `remove_client`. PPP: هیچ |
| `delete_connection` | ۱۶۹۸ | deprovision → پاک‌کردن ردیف‌های وابسته → `db.delete` → commit |

### ۲.۳ clientهای remote

| Client | افزودن | خواندن | حذف |
|---|---|---|---|
| `MikrotikClient` (`mikrotik_client.py`) | `add_peer` (۱۸۶) یک peer تازه اضافه می‌کند و `.id` برمی‌گرداند؛ تکرار آن upsert نیست. `upsert_simple_queue` (۲۴۳) با `name` upsert است | `list_peers` (۱۷۹) همه‌ی فیلدهای peer را می‌دهد | `remove_peer(.id)` (۲۳۵)؛ `remove_simple_queue(name)` (۲۷۰) اگر نبود no-op |
| `XrayClient` SSH (`xray_client.py`) | `add_client` (۲۷۸) `client_uuid` اختیاری **می‌پذیرد**؛ اول هر ردیف با همان email را حذف و سپس اضافه می‌کند (upsert با email)؛ کل `config.json` را بازنویسی و سرویس را **restart** می‌کند | `read_config` (۱۳۰)؛ `list_client_emails` (۳۱۸) | `remove_client` (۳۰۸) با فیلتر email؛ اگر نبود هم config را می‌نویسد و restart می‌کند |
| `ThreeXUIClient` (`threexui_client.py`) | `add_client` (۳۸۶) `client_uuid` اختیاری می‌پذیرد؛ اول API جدید، بعد کلاسیک. رفتارش برای email تکراری در این کد مشخص نیست | `_get_inbound` (۱۸۷) و `list_clients_with_usage` (۳۳۴): `id`، `email`، `flow`، `enable` هر client | `remove_client` (۴۱۲): API جدید با email؛ fallback کلاسیک با uuid؛ اگر client پیدا نشد «از قبل نیست» و برمی‌گردد |
| `SoftEtherClient` (`softether_client.py`) | `add_client` (۱۵۶) `CreateUser` | متد عمومی وجود ندارد. `query_all_user_stats` (۲۱۶) `EnumUser` را صدا می‌زند ولی کاربران بدون ترافیک را حذف می‌کند. رمز هرگز خوانده نمی‌شود | `remove_client` (۱۶۹) خطای «not exist» را تحمل می‌کند. `set_client_enabled` (۱۸۱) `SetUser` با رمز است و اگر کاربر نباشد هنگام enable دوباره می‌سازد |

`client_for_node` (`xray_client.py:374`) بر اساس `node.xr_panel_mode` شش client برمی‌گرداند: `ssh` و `3xui` (جدول بالا) و چهار panel دیگر. هر چهار همان چهار متد `add_client`/`remove_client`/`set_client_enabled`/`list_client_emails` را دارند و `provision_xray` (`user_ops.py:1130`) بدون تفاوت همان `add_client(node.xr_inbound_tag, email, flow=...)` را روی همه صدا می‌زند. رفتار واقعی هرکدام، خوانده‌شده از کد:

| Client | افزودن | خواندن | حذف | هویت روی panel |
|---|---|---|---|---|
| `MarzbanClient` (`marzban_client.py`) | `add_client` (۲۸۸): `POST /api/user` با `username = email`، `proxies.vless.id = uuid`، `inbounds.vless = [tag]`. **هر `XrayError` را «احتمالاً وجود دارد» فرض می‌کند و `PUT /api/user/{email}` با همان body می‌زند**، یعنی user موجود را overwrite می‌کند | `_list_users_for_tag`: صفحه‌بندی `GET /api/users` و فیلتر سمت client روی `inbounds.vless`؛ **با اولین پاسخ غیر-۲۰۰ بی‌صدا متوقف می‌شود و فهرست ناقص برمی‌گرداند**. API خواندن تک‌کاربر (`GET /api/user/{username}`) در docstring آمده ولی متدی برایش نیست | `remove_client` (۳۱۹): `DELETE /api/user/{email}`؛ ۲۰۰، ۲۰۴ و **هر ۴۰۴** را موفق می‌داند | `username` |
| `HiddifyClient` (`hiddify_client.py`) | `add_client` (۱۷۰): `POST /api/v2/admin/user/` با `name = email` و `uuid`. در هر `XrayError` (اگر uuid داده شده) **`PATCH /api/v2/admin/user/{uuid}/` با همان body** | `_list_users`: `GET /api/v2/admin/user/`؛ **۴۰۴ را «هیچ کاربری نیست» و فهرست خالی حساب می‌کند**. `_send` پاسخ غیر-JSON را بی‌صدا `{}` می‌کند | `remove_client` (۱۹۱): با uuid، یا جست‌وجوی اولین کاربر با همان `name`؛ `_delete` هر ۴۰۴ را موفق می‌داند | `uuid` (کلید اصلی و credential)؛ `name` یکتا نیست. inbound ندارد: کاربر روی همه‌ی پروتکل‌های panel فعال است |
| `MarzneshinClient` (`marzneshin_client.py`) | `add_client` (۳۰۶): `username = sanitize_username(email)` (فقط `[a-z0-9_]`، بریده به ۳۲ کاراکتر)، `key = uuid`، `service_ids = [service_id]`، `note = email`. در هر `XrayError` **`PUT /api/users/{username}` با همان body**؛ خود کد می‌گوید این شامل «collision ناشی از sanitize_username» هم می‌شود | `_iter_users`: صفحه‌بندی `GET /api/users`؛ **با پاسخ غیر-۲۰۰ بی‌صدا تمام می‌شود**. `_email_of` = `note` یا `username` | `remove_client` (۳۲۸): `DELETE /api/users/{sanitized}`؛ ۲۰۰، ۲۰۴ و هر ۴۰۴ موفق | `username` sanitize‌شده؛ email اصلی فقط در `note` |
| `SuiClient` (`sui_client.py`) | `add_client` (۲۲۸): `_client_by_name(email)`؛ **اگر هست `edit` با همان `id` (جایگزینی کامل ردیف)**، وگرنه `new`. `config.vless.uuid = uuid`، `inbounds = [inbound_id]` | `_all_clients`: `GET /apiv2/clients` (بدون `config`)؛ `_client_full(id)`: `GET /apiv2/clients?id=N` با `config`. `list_client_emails` فقط نام‌ها | `remove_client` (۲۴۹): پیداکردن `id` با نام، سپس `del`؛ اگر نبود بی‌صدا برمی‌گردد | `name` (یکتا روی panel)؛ `edit`/`del` با `id` عددی panel |

سه نتیجه برای طراحی: (۱) `add_client` هر چهار client یک موجودیت موجود با همان نام را بازنویسی می‌کند، پس در مسیر durable استفاده نمی‌شود؛ (۲) خواندن‌های فهرستی سه client خطا را به «فهرست خالی یا ناقص» تبدیل می‌کنند، پس مبنای `absent` نیستند؛ (۳) تحمل ۴۰۴ در حذف بدون تطبیق بدنه‌ی پاسخ است. قرارداد durable هرکدام در ۹.۷ تا ۹.۱۰ است.

همه‌ی clientها فقط timeout هر فراخوانی دارند (۱۰ ثانیه؛ MikroTik ۸ ثانیه) و هیچ‌کدام سقف زمان کل ندارند (بخش ۱۰.۸).

### ۲.۴ پول

| موضوع | محل | رفتار |
|---|---|---|
| `debit_admin` | `services/admin_billing.py:118` | UPDATE شرطی اتمیک روی `AdminUser.balance` (۱۳۹)؛ **در مسیر شکست هم `db.commit()` می‌زند** (۱۴۴)؛ سپس `accounting.record(admin_credit_spend)` و commit دوم (۱۵۹) |
| `charge_for_package` | `:93` | superadmin و `units <= 0` را رد می‌کند؛ `debit_admin` |
| `charge_for_renewal` | `:247` | superadmin و حساب‌های volume-billed را معاف می‌کند |
| `refund_for_package` | `:282` | UPDATE `balance + amount` (۲۹۷) و ردیف `admin_credit_refund` |
| `_charge_seller` | `routers/bot.py:138` | مشتری بدون مالک و trial معاف؛ **بعد از** provisioning صدا زده می‌شود |
| `_record_bot_sale` | `routers/bot.py:278` | مبلغ و `payment_method` از payload |
| `apply_referral_code` | `services/user_ops.py:156` | `referred_by_id`، `referral_reward_granted`، موجودی و quota هر دو طرف را می‌نویسد و **خودش commit می‌کند** (۱۹۷). caller: `routers/bot.py:938` |
| `redeem_discount_code` | `services/user_ops.py:257` | اعتبارسنجی، سپس `used_count + 1` به شکل read-modify-write در حافظه (۲۸۲)، INSERT `DiscountCodeRedemption`، و **commit داخلی** (۲۹۱). caller: `routers/bot.py:976` |
| `advance_after_payment` | `services/payment_cards.py:100` | `accumulated_amount`، و در mode `threshold` pointer کارت فعال روی `PanelSettings` یا `AdminUser`؛ **commit داخلی** (۱۴۴). caller: `routers/bot.py:557` |

### ۲.۵ واقعیت‌های زیرساختی

- `Connection` (`models.py`) ستون وضعیت provisioning ندارد؛ هر ردیف آن برای poller، RADIUS، بات و صفحه‌ی اشتراک یک اتصال زنده است.
- `users.username` یکتاست (`models.py:67`).
- SQLite این پروژه FK را enforce نمی‌کند (`database.py:28-38` فقط WAL و `busy_timeout`).
- `_auto_migrate_missing_columns` (`main.py:234`) روی جدول موجود فقط ستون و `table.indexes` می‌سازد؛ `UniqueConstraint` نمی‌سازد و شکست index فقط warning است.
- jobهای پس‌زمینه در `_start_full_services` (`main.py:964`) با APScheduler ثبت می‌شوند.
- الگوی قفل دو-دیالکتی موجود: `BEGIN IMMEDIATE` روی SQLite و `SELECT ... FOR UPDATE` روی MySQL.

### ۲.۶ نقص‌های بازتولیدشده و نگاشت به راه‌حل

| یافته | خلاصه | بسته می‌شود با |
|---|---|---|
| A / A2 | شکست یک اتصال وسط ساخت User → User و اتصال شبح، retry ناممکن | all-or-nothing، ردیف‌های نهایی فقط در T_final (بخش ۷) |
| B / B2 | شکست وسط حلقه‌ی deprovision → DB و remote ناهم‌خوان | حذف با همان ماشین گام (بخش ۱۲) |
| C / C2 | تحویل ناقص اتصال‌های bundled با شارژ کامل | policy ۱ |
| D1 تا D3 | remote قبل از commit ردیف `Connection` → orphan | گام staged قبل از remote (بخش ۵، ۷) |
| E | پنل قبل از commit User شارژ می‌کند | reserve در T1، capture در T_final (بخش ۸) |
| G1 / G2 | بات بعد از تحویل شارژ می‌کند؛ شکست شارژ یعنی سرویس رایگان | همان |
| H | ساخت گروهی: refund batch، Userهای قبلی می‌مانند | هر User یک عملیات مستقل |
| F-race / F-race2 | دو خرید هم‌زمان پکیج یک‌بارمصرف | `one_time_package_claims` در T1 (بخش ۵.۵) |

---

## ۳. معماری و اجزا

| جزء | نقش |
|---|---|
| `provisioning_operations` | یک ردیف به‌ازای هر عملیات تجاری؛ `business_key` یکتا |
| `provisioning_steps` | یک ردیف به‌ازای هر اتصال (slot)؛ هویت remote و credential staged |
| `payment_reservations` | reservation ساخت‌یافته‌ی پول، به‌ازای هر پرداخت‌کننده |
| `one_time_package_claims` | claim پکیج یک‌بارمصرف |
| `provisioning_type_modes` | mode هر نوع عملیات (`legacy`/`durable`) |
| `provisioning_runtime_state` | هویت نصب، مالکیت host، backend قفل و mode gate (۵.۸) |
| `provisioning_node_contracts` | آمادگی هر `(node, backend)` با نسخه‌ی adapter، fingerprint و matcherهای «نیست» تأییدشده (۵.۱۰) |
| `provisioning_ownership_events` | audit دائمی claim، handoff، takeover و تغییر mode gate (۵.۱۱) |
| `provisioning_gate_stats` | شمارنده‌های contention و نویسنده‌ی instrument‌نشده به‌ازای هر node (۵.۱۲) |
| runner mutation | process فرزند قابل‌کشتن که gate را نگه می‌دارد و mutation remote را اجرا می‌کند (۱۰.۸)؛ ورودی و خروجی آن `RemoteActionDTO`/`RemoteActionResult` است (۱۰.۹) |
| `provisioning_node_reconciliations` | آخرین نتیجه‌ی reconciliation آدرس WireGuard هر node (۵.۹) |
| `resource_locks` | lease با fencing برای نوشتن‌های **DB** (همان جدول Receipt Void)؛ تضمین serialization remote نیست |
| gate مالکیت node | قفل غیرمنقضی، آزادشونده با مرگ process، دور هر mutation remote (بخش ۱۰) |
| adapter هر backend | سه عمل `ensure_present`، `ensure_absent`، `read` |
| worker | ادامه‌ی عملیات با lease منقضی |

ردیف‌های `User`، `Purchase` و `Connection` **فقط** در تراکنش نهایی ساخته می‌شوند. تا آن لحظه هیچ خواننده‌ی موجودی (poller، RADIUS، بات، اشتراک) چیزی نمی‌بیند، پس هیچ خواننده‌ای نباید عوض شود.

---

## ۴. قراردادهای Schema

| موضوع | قرارداد |
|---|---|
| `id` | `BIGINT NOT NULL PRIMARY KEY AUTOINCREMENT` (`BigInteger().with_variant(Integer, "sqlite")`) |
| مبلغ | `BIGINT` تومان |
| زمان | `DATETIME(6)` UTC؛ `created_at` همیشه `NOT NULL` |
| enum | `VARCHAR` با `CHECK (col IN (...))` |
| JSON | `TEXT` حاوی JSON معتبر؛ هرگز secret |
| `version` | `INT NOT NULL DEFAULT 0`؛ هر UPDATE با `WHERE version = :expected` |
| UNIQUE و NULL | هیچ UNIQUEی برای درستی به NULL تکیه ندارد، جز موارد «NULL-exempt عمداً» |
| FK | روی MariaDB قید واقعی. روی SQLite قید DB نیست؛ هیچ‌کدام از جدول‌های جدید مسیر حذف ندارند (تست AST، LP-70) و ارجاع به جدول‌های موجودِ قابل‌حذف (`users`، `purchases`، `connections`، `nodes`، `packages`، `admin_users`) **snapshot عددی بدون FK** است |
| migration | هر ده جدول این سند جدیدند و با `Base.metadata.create_all` ساخته می‌شوند. **این سند هیچ ستون، index یا قیدی به هیچ جدول موجودی اضافه نمی‌کند** (۵.۶). `_auto_migrate_missing_columns` (`main.py:234`) روی SQLite، MySQL و MariaDB اجرا می‌شود (`main.py:265`)، ولی شکست ساخت هر ستون یا index را فقط warning می‌کند (`main.py:309-320`)؛ پس وجود هر جدول، UNIQUE و index این سند بعد از startup با inspector و در تست هر دو دیالکت تأیید می‌شود (LP-67) و تا تأیید نشده mode `durable` رد می‌شود |
| `DB_NOW` | همیشه در SQL (MariaDB: `NOW(6)`؛ SQLite: `strftime('%Y-%m-%d %H:%M:%f','now')`) |

---

## ۵. Schema

### ۵.۱ `provisioning_operations`

```
id                    PK
operation_type        VARCHAR(24)  NOT NULL  CHECK IN ('create_user','purchase','add_connection','renew_user','renew_purchase',
                                                       'delete_connection','delete_purchase','delete_user')
business_key          VARCHAR(160) NOT NULL
request_hash          CHAR(64)     NOT NULL                 -- SHA-256 intent canonical
tenant_scope_key      VARCHAR(64)  NOT NULL
actor_kind            VARCHAR(8)   NOT NULL  CHECK IN ('admin','bot','system')       -- فقط audit
actor_id              INT          NULL                     -- فقط audit
actor_ip              VARCHAR(64)  NULL
approval_uuid         CHAR(36)     NULL                     -- اگر زیر یک receipt approval اجرا می‌شود (snapshot ؛ بدون FK)
approval_execution_version INT     NULL                     -- receipt_approvals.version (execution token) در لحظه‌ی T1
approval_key_bound    BOOLEAN      NULL                     -- 1 = اجرا به یک instance کلید bind است ؛ 0 = فقط principal in-process
execution_key_instance_uuid CHAR(36) NULL                   -- snapshot receipt_approvals.execution_key_instance_uuid در T1
optional_effects      TEXT/JSON    NULL                     -- فقط approvalدار، در T_final: برای هر effect optional {effect_type, effect_key, outcome} ؛ بدون secret
intent                TEXT/JSON    NOT NULL                 -- package_id، quota، days، مبلغ، روش پرداخت، فهرست slotها ؛ بدون secret
target_user_id        INT          NULL                     -- User موجود (snapshot عددی)
username_claim        VARCHAR(64)  NULL                     -- فقط create_user، فقط تا ترمینال
state                 VARCHAR(20)  NOT NULL  CHECK IN ('prepared','provisioning','remote_complete','completed',
                                                       'compensating','compensated','cleanup_required')
forward_deadline      DATETIME(6)  NOT NULL                 -- DB_NOW + مهلت roll-forward در لحظه‌ی T1
result_user_id        INT          NULL
result_purchase_id    INT          NULL
sale_ledger_entry_id  INT          NULL
error_code            VARCHAR(48)  NULL
attempts              INT          NOT NULL DEFAULT 0
next_retry_at         DATETIME(6)  NULL
wallet_epoch_at_start BIGINT       NOT NULL                 -- wallet_runtime_state.epoch در T1
created_at            NOT NULL
completed_at          NULL
version

UNIQUE (operation_type, business_key)
UNIQUE (username_claim)                         -- NULL-exempt عمداً: فقط عملیات غیرترمینال create_user نام را نگه می‌دارد
UNIQUE (approval_uuid, approval_execution_version)   -- NULL-exempt عمداً: عملیات بدون approval هر دو را NULL دارد
INDEX  (state, next_retry_at)
CHECK  ((approval_uuid IS NULL) = (approval_execution_version IS NULL))
CHECK  ((approval_uuid IS NULL) = (approval_key_bound IS NULL))
CHECK  ((approval_key_bound IS NOT NULL AND approval_key_bound = 1) = (execution_key_instance_uuid IS NOT NULL))
CHECK  (approval_uuid IS NULL OR operation_type IN ('create_user','purchase','renew_user','renew_purchase'))
CHECK  (optional_effects IS NULL OR (approval_uuid IS NOT NULL AND state = 'completed'))
CHECK  ((state IN ('completed','compensated')) = (completed_at IS NOT NULL))
CHECK  (state NOT IN ('completed','compensated') OR username_claim IS NULL)
```

`sale_ledger_entry_id` قید DB ندارد: فروش بدون مبلغ (trial، رایگان) و عملیات `add_connection`/`delete_*` ردیف Ledger ندارند. قاعده‌ی «فروش مبلغ‌دار ⇒ LedgerEntry» در T_final سرویس enforce و با LP-10 تست می‌شود.

| `operation_type` | `business_key` |
|---|---|
| هر عملیات زیر receipt approval | `approval:{approval_uuid}:v:{approval_execution_version}` |
| `create_user`، `purchase`، `add_connection`، `renew_*` بدون approval | `req:{tenant_scope_key}:{idempotency_token}`؛ token یک UUID که caller یک‌بار می‌سازد و در retry همان را می‌فرستد. اگر caller قدیمی نفرستد، سرور یک UUID تازه می‌سازد (یعنی هر درخواست مستقل، مثل امروز) |
| عضو ساخت گروهی | `bulk:{batch_token}:{index}` |
| `delete_*` | `delete:{resource_type}:{resource_id}:{idempotency_token}` |

درخواست تکراری با همان کلید: `request_hash` برابر → همان عملیات؛ متفاوت → ۴۰۹.

**نسل تلاش approval.** `approval_execution_version` همان execution token سند Receipt Void است: `version` ردیف approval که فقط با takeover و retry کنترل‌شده‌ی `failed` زیاد می‌شود و در طول یک اجرا (`registered → mutating → completed`) ثابت است.

- retry با همان token → همان `business_key` → همان عملیات؛ reserve دوم ساخته نمی‌شود.
- approval که `failed` شد و با retry کنترل‌شده `version + 1` گرفت → `business_key` تازه → عملیات تازه. عملیات `compensated` نسخه‌ی قبل دست نمی‌خورد و برای audit می‌ماند.
- برای هر `(approval_uuid, version)` دقیقاً یک عملیات: هم `UNIQUE(operation_type, business_key)` و هم `UNIQUE(approval_uuid, approval_execution_version)` آن را enforce می‌کنند (دومی مستقل از `operation_type`).
- کلید اجرا با دو ستون ثبت می‌شود تا «bind به in-process» (که در Receipt Void با NULL نشان داده می‌شود) از «عملیات بدون approval» جدا باشد: `approval_key_bound = 0` یعنی فقط principal in-process؛ `= 1` یعنی فقط instance کلید `execution_key_instance_uuid`.

### ۵.۲ `provisioning_steps`

```
id                    PK
operation_id          BIGINT       NOT NULL  FK provisioning_operations(id) ON DELETE RESTRICT
slot_key              VARCHAR(64)  NOT NULL                 -- 'package_connection:{id}' | 'request_slot:{index}' | 'connection:{id}' (حذف)
direction             VARCHAR(8)   NOT NULL  CHECK IN ('create','remove')
step_order            INT          NOT NULL
backend               VARCHAR(16)  NOT NULL  CHECK IN ('mikrotik_wg','radius_ppp','xray_ssh','threexui','softether',
                                                       'marzban','hiddify','marzneshin','sui')
node_id               INT          NOT NULL                 -- snapshot عددی
protocol              VARCHAR(16)  NOT NULL
flow                  VARCHAR(64)  NOT NULL DEFAULT ''
-- هویت remote (غیرمحرمانه) ؛ در T1 قطعی می‌شود و دیگر عوض نمی‌شود
wg_interface          VARCHAR(64)  NULL
wg_peer_name          VARCHAR(128) NULL
wg_public_key         VARCHAR(128) NULL
wg_client_address     VARCHAR(64)  NULL
wg_gateway_with_prefix VARCHAR(64) NULL
wg_subnet_expanded    BOOLEAN      NOT NULL DEFAULT 0
xr_inbound_tag        VARCHAR(128) NULL                     -- xray_ssh ، marzban
xr_panel_inbound_id   INT          NULL                     -- threexui: inbound id ؛ marzneshin: service id ؛ sui: inbound id
xr_email              VARCHAR(255) NULL
account_username      VARCHAR(128) NULL                     -- ppp_username یا نام کاربر SoftEther
speed_limit_mbps      INT          NULL
max_concurrent_sessions INT        NULL
-- credential staged ؛ همان سطح اعتماد ستون‌های موجود Connection ؛ در ترمینال NULL می‌شود
staged_wg_private_key VARCHAR(128) NULL
staged_password       VARCHAR(128) NULL
staged_xr_uuid        VARCHAR(64)  NULL
-- نتیجه
state                 VARCHAR(20)  NOT NULL  CHECK IN ('staged','remote_calling','remote_created','active',
                                                       'compensating','removed','cleanup_required')
remote_attempted      BOOLEAN      NOT NULL DEFAULT 0       -- در همان commit که گام 'remote_calling' می‌شود 1 می‌شود و دیگر 0 نمی‌شود
remote_outcome        VARCHAR(28)  NULL      CHECK IN ('verified_absent','delete_idempotently_absent','unverified','abandoned')
connection_id         INT          NULL                     -- direction='create': در T_final پر می‌شود ؛ 'remove': از ابتدا
forced_by_admin_id    INT          NULL                     -- فقط force ؛ snapshot عددی superadmin
force_reason          VARCHAR(500) NULL
forced_at             DATETIME(6)  NULL
attempts              INT          NOT NULL DEFAULT 0
next_retry_at         DATETIME(6)  NULL
error_code            VARCHAR(48)  NULL
error_sanitized       VARCHAR(500) NULL
created_at            NOT NULL
updated_at            NOT NULL
version

UNIQUE (operation_id, slot_key)
INDEX  (node_id, backend, state)
INDEX  (state, next_retry_at)
CHECK  (state NOT IN ('active','removed') OR
        (staged_wg_private_key IS NULL AND staged_password IS NULL AND staged_xr_uuid IS NULL))
CHECK  (state NOT IN ('compensating','cleanup_required') OR
        (staged_wg_private_key IS NULL AND staged_password IS NULL))
CHECK  (remote_outcome IS NULL OR state IN ('removed','cleanup_required'))
CHECK  (state <> 'removed' OR remote_attempted = 0
        OR (remote_outcome IS NOT NULL AND remote_outcome IN ('verified_absent','delete_idempotently_absent','abandoned')))
CHECK  (state <> 'removed' OR remote_outcome IS NULL OR remote_outcome <> 'unverified')
CHECK  (state <> 'cleanup_required' OR remote_outcome IS NULL OR remote_outcome = 'unverified')
CHECK  (state IN ('staged','removed','active') OR remote_attempted = 1 OR direction = 'remove')
CHECK  (backend <> 'radius_ppp' OR remote_attempted = 0)
CHECK  ((remote_outcome IS NOT NULL AND remote_outcome = 'abandoned') = (forced_by_admin_id IS NOT NULL))
CHECK  ((forced_by_admin_id IS NULL) = (force_reason IS NULL))
CHECK  ((forced_by_admin_id IS NULL) = (forced_at IS NULL))
CHECK  (state <> 'active' OR connection_id IS NOT NULL)
```

- `slot_key` همان هویت پایدار اتصال است که Receipt Void در manifest به کار می‌برد (`conn:package_connection:{id}`، `conn:request_slot:{index}`)؛ ترتیب relationship روی آن اثری ندارد.
- credential فقط در سه ستون `staged_*` است، هرگز در `intent`، `error_sanitized` یا هر JSON. دو CHECK اول زمان پاک‌شدن را enforce می‌کنند؛ قاعده‌ی کامل در ۶.۳.
- `backend` در T1 از `node.type` و `node.xr_panel_mode` همان لحظه تعیین و همراه `xr_inbound_tag`/`xr_panel_inbound_id` snapshot می‌شود. اگر در زمان اجرای گام، backend یا این دو مقدار روی node عوض شده باشد، گام خطای قطعی `node_backend_changed` می‌گیرد و هیچ فراخوانی نویسنده‌ای فرستاده نمی‌شود: گام با `remote_attempted = 0` مستقیم `removed`؛ گام با `remote_attempted = 1` → `cleanup_required` (هویت روی panel قبلی دیگر قابل تأیید نیست).
- `account_username` برای `marzneshin` نام sanitize‌شده‌ی panel است (۹.۹).
- `radius_ppp` فراخوانی remote ندارد؛ گامش مستقیم از `staged` به `active` (یا `removed`) می‌رود و `remote_outcome` آن NULL است.
- `wg_gateway_with_prefix` و `wg_subnet_expanded` فقط **audit**‌اند (subnet و gateway لحظه‌ی T1). هیچ تصمیمی درباره‌ی آدرس interface روی router از این دو گرفته نمی‌شود (بخش ۹.۱).
- force سه ستون `forced_*` را همراه `remote_outcome='abandoned'` می‌نویسد؛ این همان ثبت orphan برای پاک‌سازی دستی است (node، backend و هویت غیرمحرمانه از ستون‌های همین ردیف خوانده می‌شود).

### ۵.۳ `payment_reservations`

```
id                        PK
operation_id              BIGINT      NOT NULL  FK provisioning_operations(id) ON DELETE RESTRICT
payer_kind                VARCHAR(20) NOT NULL  CHECK IN ('customer_wallet','reseller_credit')
generation                VARCHAR(16) NOT NULL  CHECK IN ('legacy_balance','wallet_lot','admin_balance')
user_id                   INT         NULL      -- customer_wallet: snapshot عددی User
admin_id                  INT         NULL      -- reseller_credit: snapshot عددی AdminUser
amount                    BIGINT      NOT NULL  CHECK (amount > 0)
hold_ref                  CHAR(36)    NOT NULL  -- UUID ؛ برای wallet_lot همان wallet_debit_events.hold_ref
state                     VARCHAR(10) NOT NULL  CHECK IN ('reserved','captured','released')
capture_ledger_entry_id   INT         NULL      -- customer_wallet: LedgerEntry فروش ؛ reseller_credit: ردیف admin_credit_spend
reserved_at               NOT NULL
captured_at               NULL
released_at               NULL
version

UNIQUE (operation_id, payer_kind)
UNIQUE (hold_ref)
INDEX  (payer_kind, state)
INDEX  (user_id, state)
INDEX  (admin_id, state)
CHECK  ((payer_kind = 'customer_wallet' AND user_id IS NOT NULL AND admin_id IS NULL AND generation IN ('legacy_balance','wallet_lot'))
     OR (payer_kind = 'reseller_credit' AND admin_id IS NOT NULL AND user_id IS NULL AND generation = 'admin_balance'))
CHECK  ((state = 'reserved' AND captured_at IS NULL AND released_at IS NULL AND capture_ledger_entry_id IS NULL)
     OR (state = 'captured' AND captured_at IS NOT NULL AND released_at IS NULL AND capture_ledger_entry_id IS NOT NULL)
     OR (state = 'released' AND released_at IS NOT NULL AND captured_at IS NULL AND capture_ledger_entry_id IS NULL))
```

- پرداخت با رسید کارت reservation مشتری ندارد (پول بیرون از سیستم دریافت شده)؛ فقط reservation اعتبار reseller، اگر مالک شارژ می‌شود.
- مبلغ صفر (superadmin، حساب volume-billed، trial، مشتری بدون مالک) ردیف ندارد.

### ۵.۴ `provisioning_type_modes`

```
operation_type   VARCHAR(24) NOT NULL PRIMARY KEY
mode             VARCHAR(8)  NOT NULL DEFAULT 'legacy'  CHECK IN ('legacy','durable')
changed_at       NOT NULL
version
```

یک ردیف به‌ازای هر `operation_type`. هر درخواست در اولین statement تراکنش خود ردیف نوع خودش را با locking read می‌خواند (MariaDB: `LOCK IN SHARE MODE`؛ SQLite: زیر `BEGIN IMMEDIATE`) و مسیر legacy یا durable را اجرا می‌کند. تغییر mode یک UPDATE انحصاری است، پس منتظر پایان درخواست‌های در جریان می‌ماند.

### ۵.۵ `one_time_package_claims`

```
id                   PK
package_id           INT          NOT NULL        -- snapshot عددی
claim_key            CHAR(64)     NOT NULL        -- SHA-256 hex رشته‌ی canonical چهارتایی claim (پایین)
claim_kind           VARCHAR(8)   NOT NULL  CHECK IN ('user','telegram')
claim_user_id        INT          NULL            -- kind='user' ؛ snapshot عددی
claim_tenant_scope_key VARCHAR(64) NULL           -- kind='telegram'
claim_telegram_id    BIGINT       NULL            -- kind='telegram'
release_seq          BIGINT       NOT NULL DEFAULT 0   -- 0 = فعال ؛ در آزادسازی = id همین ردیف
operation_id         BIGINT       NOT NULL  FK provisioning_operations(id) ON DELETE RESTRICT
purchase_id          INT          NULL            -- در T_final
released_at          NULL
created_at           NOT NULL

UNIQUE (package_id, claim_key, release_seq)
CHECK  ((release_seq = 0) = (released_at IS NULL))
CHECK  ((claim_kind = 'user'     AND claim_user_id IS NOT NULL AND claim_tenant_scope_key IS NULL AND claim_telegram_id IS NULL)
     OR (claim_kind = 'telegram' AND claim_user_id IS NULL AND claim_tenant_scope_key IS NOT NULL AND claim_telegram_id IS NOT NULL))
CHECK  (LENGTH(claim_key) = 64)
```

`claim_key` طول ثابت دارد و به طول `tenant_scope_key` یا `telegram_id` وابسته نیست. ورودی hash یک رشته‌ی canonical بدون ابهام است، با طول هر جزء در خود رشته:

```
kind='user':      "user|" + decimal(claim_user_id)
kind='telegram':  "telegram|" + decimal(len(tenant_scope_key)) + "|" + tenant_scope_key + "|" + decimal(claim_telegram_id)
claim_key = lowercase_hex(SHA-256(UTF-8 رشته‌ی بالا))
```

ستون‌های تایپ‌شده برای audit و نمایش‌اند؛ یکتایی فقط روی hash است. سرویس قبل از INSERT، `claim_key` را از همان ستون‌ها دوباره حساب و برابری را assert می‌کند؛ job تطبیق همین را برای ردیف‌های ذخیره‌شده می‌سنجد. `tenant_scope_key` بلندتر از ۶۴ کاراکتر قبل از hash رد می‌شود (۴۲۲)، نه truncate.

- ستون `release_seq` یکتایی «فقط یک claim فعال» را بدون تکیه بر NULL می‌دهد: ردیف آزادشده `release_seq = id` می‌گیرد و جا را برای claim فعال بعدی باز می‌کند.
- برای هر خرید پکیج `one_time_per_user` دو claim در T1 ساخته می‌شود: یکی برای User (اگر وجود دارد) و یکی برای Telegram. برخورد UNIQUE در T1 یعنی خرید رد می‌شود، قبل از هر reserve و هر remote.
- خریدهای موجود قبل از این جدول: در لحظه‌ی durable‌شدن نوع `purchase`، برای هر `Purchase` موجودِ پکیج‌های `one_time_per_user` claim ساخته می‌شود (backfill idempotent با همان UNIQUE). بررسی فعلی `_ensure_one_time_package_not_reused` به‌عنوان لایه‌ی اول می‌ماند.

### ۵.۶ جدول‌های موجود: بدون هیچ تغییر schema

این سند **هیچ** ستون، index، قید یا جدول موجودی را تغییر نمی‌دهد؛ نه `nodes`، نه `connections`، نه `users`، نه `admin_users`. جدول‌های `resource_locks`، `wallet_debit_events` و `receipt_approvals` متعلق به سند Receipt Void‌اند و همان‌طور که آن‌جا تعریف شده‌اند استفاده می‌شوند.

آنچه عوض می‌شود فقط رفتار کد است، نه schema:

- `_wg_used_ips` علاوه بر `Connection`، آدرس گام‌های غیرترمینال `provisioning_steps` همان node را هم «استفاده‌شده» حساب می‌کند (بخش ۹.۱).
- نوشتن `Node.mt_client_subnet` هنگام بزرگ‌شدن subnet از commit مستقل داخل `_wg_reserve_ips` (`user_ops.py:919-920`) به داخل T1 منتقل می‌شود. ستون همان ستون موجود است.

### ۵.۷ شمارش schema

| جدول | ستون |
|---|---|
| `provisioning_operations` | ۲۸ |
| `provisioning_steps` | ۳۸ |
| `payment_reservations` | ۱۴ |
| `provisioning_type_modes` | ۴ |
| `one_time_package_claims` | ۱۲ |
| `provisioning_runtime_state` | ۱۵ |
| `provisioning_node_reconciliations` | ۷ |
| `provisioning_node_contracts` | ۱۲ |
| `provisioning_ownership_events` | ۱۰ |
| `provisioning_gate_stats` | ۱۱ |
| **جمع: ۱۰ جدول جدید** | **۱۵۱ ستون** |

جدول موجود تغییریافته: صفر. ستون اضافه‌شده به جدول موجود: صفر.

### ۵.۸ `provisioning_runtime_state`

```
id                    INT          NOT NULL PRIMARY KEY  CHECK (id = 1)        -- singleton
installation_uuid     CHAR(36)     NOT NULL      -- هویت نصب ؛ namespace نام قفل‌ها ؛ با هویت host یکی نیست
lock_backend          VARCHAR(24)  NOT NULL DEFAULT 'none'  CHECK IN ('none','flock','flock+get_lock')
gate_mode             VARCHAR(10)  NOT NULL DEFAULT 'off'   CHECK IN ('off','shadow','enforced')
gate_mode_epoch       BIGINT       NOT NULL DEFAULT 0      -- با هر تغییر gate_mode یکی زیاد می‌شود
gate_mode_changed_at  DATETIME(6)  NOT NULL
owner_state           VARCHAR(10)  NOT NULL DEFAULT 'none'  CHECK IN ('none','active','draining','released')
owner_host_id         CHAR(64)     NULL          -- هویت host مالک (۱۰.۴) ؛ hex SHA-256
owner_boot_id         CHAR(36)     NULL          -- boot id kernel همان host در لحظه‌ی claim
ownership_epoch       BIGINT       NOT NULL DEFAULT 0      -- fencing epoch ؛ با هر claim تازه ، release ، reclaim و takeover یکی زیاد می‌شود
owner_claimed_at      DATETIME(6)  NULL
owner_heartbeat_at    DATETIME(6)  NULL          -- فقط نمایش و هشدار ؛ در هیچ تصمیم خودکار مالکیت استفاده نمی‌شود
lock_verified_at      DATETIME(6)  NULL
changed_at            NOT NULL
version

CHECK ((owner_state IN ('active','draining')) = (owner_host_id IS NOT NULL))
CHECK ((owner_host_id IS NULL) = (owner_boot_id IS NULL))
CHECK ((owner_host_id IS NULL) = (owner_claimed_at IS NULL))
CHECK ((owner_host_id IS NULL) = (owner_heartbeat_at IS NULL))
CHECK ((lock_backend = 'none') = (lock_verified_at IS NULL))
CHECK (lock_backend <> 'none' OR gate_mode = 'off')
CHECK (gate_mode <> 'enforced' OR owner_state IN ('active','draining'))
```

یک ردیف، ساخته در L0 با `installation_uuid` تصادفی (lifecycle آن: ۱۰.۱۰)، `lock_backend='none'`، `gate_mode='off'`، `owner_state='none'`. هر نوشتن روی آن `UPDATE ... WHERE id = 1 AND version = :v` است (CAS) و یک ردیف `provisioning_ownership_events` در همان تراکنش می‌نویسد.

### ۵.۹ `provisioning_node_reconciliations`

```
node_id               INT          NOT NULL PRIMARY KEY     -- snapshot عددی ؛ بدون FK
last_run_at           DATETIME(6)  NOT NULL
result                VARCHAR(16)  NOT NULL  CHECK IN ('in_sync','repaired','router_wider','conflict','unreachable')
db_subnet             VARCHAR(64)  NOT NULL
router_address        VARCHAR(64)  NULL                     -- آخرین آدرس خوانده‌شده ؛ NULL وقتی unreachable یا بدون آدرس
consecutive_failures  INT          NOT NULL DEFAULT 0
version
```

آخرین وضعیت هر node؛ هر اجرا همان ردیف را upsert می‌کند (تاریخچه نگه داشته نمی‌شود). نه secret دارد و نه credential.

### ۵.۱۰ `provisioning_node_contracts`

آمادگی به‌ازای هر `(node, backend)` است، نه به‌ازای هر backend: دو node با backend یکسان می‌توانند نسخه، endpoint و شکل پاسخ متفاوت داشته باشند.

```
node_id               INT          NOT NULL                 -- snapshot عددی ؛ بدون FK
backend               VARCHAR(16)  NOT NULL                 -- همان مقادیر provisioning_steps.backend ، جز radius_ppp
state                 VARCHAR(12)  NOT NULL  CHECK IN ('unverified','ready','invalidated')
adapter_version       VARCHAR(40)  NULL                     -- ثابت کد همان adapter در لحظه‌ی تأیید (hash قرارداد adapter)
server_fingerprint    CHAR(64)     NULL                     -- SHA-256 نسخه یا شکل پاسخ panel/دستگاه در لحظه‌ی تأیید (پایین)
config_fingerprint    CHAR(64)     NULL                     -- SHA-256 پیکربندی غیرمحرمانه‌ی node در لحظه‌ی تأیید (پایین)
contract              TEXT/JSON    NULL                     -- نتیجه‌های تأییدشده‌ی read/create/delete و matcherها ؛ بدون secret
verified_by_admin_id  INT          NULL
verified_at           DATETIME(6)  NULL
invalidated_at        DATETIME(6)  NULL
invalidation_reason   VARCHAR(40)  NULL      CHECK IN ('adapter_version_changed','config_changed','server_changed','backend_changed',
                                                       'probe_mismatch','manual')
version

PRIMARY KEY (node_id, backend)
CHECK (state <> 'unverified' OR
       (adapter_version IS NULL AND server_fingerprint IS NULL AND config_fingerprint IS NULL AND contract IS NULL
        AND verified_by_admin_id IS NULL AND verified_at IS NULL AND invalidated_at IS NULL AND invalidation_reason IS NULL))
CHECK (state NOT IN ('ready','invalidated') OR
       (adapter_version IS NOT NULL AND server_fingerprint IS NOT NULL AND config_fingerprint IS NOT NULL AND contract IS NOT NULL
        AND verified_by_admin_id IS NOT NULL AND verified_at IS NOT NULL))
CHECK (state <> 'ready' OR (invalidated_at IS NULL AND invalidation_reason IS NULL))
CHECK (state <> 'invalidated' OR (invalidated_at IS NOT NULL AND invalidation_reason IS NOT NULL))
```

| `state` | فیلدهای verification (شش ستون) | `invalidated_at`، `invalidation_reason` |
|---|---|---|
| `unverified` | همه NULL | هر دو NULL |
| `ready` | همه پر | هر دو NULL |
| `invalidated` | همه پر: snapshot آخرین verification حفظ می‌شود | هر دو پر |

ردیف `unverified` هیچ مقدار ساختگی ندارد (نه hash جانشین، نه صفر، نه `{}`). نبودن ردیف برای یک `(node, backend)` معادل `unverified` است.

- `radius_ppp` remote ندارد و ردیف ندارد؛ بررسی آمادگی آن را نادیده می‌گیرد.
- **`config_fingerprint`** = SHA-256 رشته‌ی canonical این فیلدهای غیرمحرمانه‌ی node: `type`، `xr_panel_mode`، host/base URL، port، `mt_use_ssl`، `mt_wireguard_interface`، `se_hub_name`، `xr_inbound_tag`، `xr_panel_inbound_id`، `xr_config_path`، `xr_service_name`. username، password، token و کلید SSH در آن نیستند.
- **`server_fingerprint`**: جایی که دستگاه نسخه‌ی قابل‌اعتماد می‌دهد (RouterOS از `get_system_resources`) hash همان؛ جایی که نمی‌دهد، hash «شکل» پاسخ‌های خواندن probe (مجموعه‌ی مرتب کلیدهای JSON و نوع هر کلید، بدون مقدار). این‌که کدام panel نسخه می‌دهد در کد موجود معلوم نیست و در staging تعیین می‌شود؛ fingerprint شکل پاسخ تغییر نسخه‌ای را که شکل را عوض نکند نمی‌بیند، و سند ادعای تشخیص آن را ندارد.
- **`contract`** شامل: نتیجه‌ی ثبت‌شده‌ی probe برای read موجودیت موجود و ناموجود، ساخت تکراری، حذف موجود و ناموجود؛ و فهرست `not_exist`.
- **schema matcherهای `not_exist`** بسته و محدود است؛ هر عضو دقیقاً یکی از این دو شکل:

```
HTTP:      {"kind":"http", "op":"read"|"delete"|"list_empty", "status": <عدد دقیق>, "json_path": <مسیر از allowlist همان backend>, "equals": <رشته یا عدد دقیق>}
JSON-RPC:  {"kind":"jsonrpc", "op":"read"|"delete", "error_code": <عدد دقیق>}
```

  - `json_path` و `equals` برای `kind=http` اجباری‌اند: matcher فقط با status (مثلاً «هر ۴۰۴») رد می‌شود.
  - regex، wildcard، بازه‌ی status، `contains`، و هر کلید ناشناخته رد می‌شود (۴۲۲). حداکثر ۴ matcher برای هر `op`.
  - «هر exception یعنی absent» قابل بیان نیست: matcher فقط روی پاسخ کامل دریافت‌شده اعمال می‌شود، نه روی خطای اتصال یا timeout.
- **تأیید** (`POST /api/provisioning/nodes/{id}/contract/verify`، superadmin): probe زنده روی همان node، action `contract_probe`. **فقط وقتی `gate_mode = 'enforced'` و فقط از runner** (۱۰.۸، ۱۰.۹)؛ در `off` و `shadow` رد می‌شود (۴۰۹ `gate_not_enforced`). تأیید یک node هیچ node دیگری را آماده نمی‌کند.

  **bootstrap بدون اعتماد قبلی.** probe هیچ matcher قبلی ندارد و هیچ پاسخی را از پیش «نیست» فرض نمی‌کند:

```
۱. identity آزمایشی یکتا و namespaced: "um-probe-" + ۱۶ hex تصادفی (و uuid/رمز آزمایشی تازه) ؛ هرگز identity یک مشتری
۲. read_before: پاسخ raw خواندن این identity ثبت می‌شود → projection P1 (status ، و مسیر/مقدار JSON یا error_code)
۳. create
۴. read: باید identity ساخته‌شده را دقیقاً تأیید کند (present_match با همه‌ی اجزای هویت)
۵. delete
۶. read_after: پاسخ raw → projection P2
۷. فقط اگر P1 و P2 هر دو پاسخ کامل و معتبر و **برابر** باشند: matcher not_exist از همان projection ساخته می‌شود ؛
   ردیف با state='ready' ، adapter_version فعلی ، دو fingerprint لحظه‌ی probe و contract نوشته می‌شود
```

  - timeout، HTML، پاسخ ناقص یا غیر-JSON، اختلاف P1 و P2، شکست قدم ۴، یا هر ابهام در cleanup: قرارداد `ready` نمی‌شود، هیچ matcherی ساخته نمی‌شود، ردیف `unverified` (یا `invalidated` قبلی) می‌ماند و node برای durable بسته می‌ماند.
  - اگر بعد از create، نبودن identity آزمایشی اثبات نشود: نتیجه `probe_cleanup_unknown` با node، backend و identity آزمایشی (بدون uuid و رمز) به superadmin گزارش می‌شود تا دستی پاک شود.
  - «اولین ۴۰۴» یا هر exception هرگز خودکار `not_exist` حساب نمی‌شود: matcher فقط از دو مشاهده‌ی معتبر و یکسان قبل و بعد ساخته می‌شود، و schema پایین را هم باید بگذراند.
- **ابطال خودکار.** در T1 هر عملیات، قبل از ساخت هر `RemoteActionDTO`، در startup و در هر چرخه‌ی reconciliation:

| مقایسه | ناهم‌خوان → |
|---|---|
| `adapter_version` ردیف با ثابت کد فعلی | `invalidated` / `adapter_version_changed` |
| `config_fingerprint` ردیف با مقدار زنده‌ی node | `invalidated` / `config_changed` |
| `backend` ردیف با backend فعلی node | `invalidated` / `backend_changed` |
| `server_fingerprint` ردیف با probe خواندنی دوره‌ای (فقط reconciliation) | `invalidated` / `server_changed` |

  ابطال یک UPDATE کوچک با CAS است و بلافاصله برای مسیر durable fail-closed می‌کند: T1 durable روی آن node ۴۰۹ `node_contract_not_ready`؛ گام‌های در جریان همان node خطای قابل‌retry می‌گیرند و بعد از `forward_deadline` طبق ۱۱ ادامه می‌یابند.
- **کجا آمادگی enforce می‌شود** (و فقط همین‌جاها): تغییر هر `operation_type` از `legacy` به `durable`؛ T1 هر عملیات durable؛ `p6_ready()`؛ و فعال‌کردن node (پایین). ورود gate به `enforced` به آمادگی هیچ nodeی وابسته نیست.
- **node بدون قرارداد `ready` در `enforced`:** مسیر legacy با همان semantics امروز اجرا می‌شود (همان متدهای client امروز، بدون matcher)، ولی از runner و serialization واقعی gate می‌گذرد. action durable (adapterهای بخش ۹) روی آن node در والد و قبل از spawn رد می‌شود (۴۰۹ `node_contract_not_ready`).
- **node غیرفعال** (`Node.enabled = False`) مانع هیچ activation نیست. فعال‌کردن دوباره‌ی آن (یا ساخت node تازه با `enabled = True`) فقط وقتی رد می‌شود که حداقل یک `operation_type` که می‌تواند از آن node استفاده کند `durable` است و node ردیف `ready` با نسخه و fingerprint فعلی ندارد (۴۰۹ `node_contract_not_ready`)؛ در آن حالت node اول غیرفعال ساخته، verify و سپس فعال می‌شود. وقتی همه‌ی انواع `legacy`‌اند فعال‌کردن آزاد است.

### ۵.۱۱ `provisioning_ownership_events`

```
id                PK
event_type        VARCHAR(24)  NOT NULL  CHECK IN ('verify','claim','drain_start','drain_cancel','release','shutdown_release',
                                                   'reclaim','forced_takeover','gate_mode_change',
                                                   'installation_rotate','installation_fork_reset')
from_host_id      CHAR(64)     NULL
to_host_id        CHAR(64)     NULL
ownership_epoch   BIGINT       NOT NULL
gate_mode_from    VARCHAR(10)  NULL
gate_mode_to      VARCHAR(10)  NULL
actor_admin_id    INT          NOT NULL                 -- snapshot عددی superadmin ؛ 0 = سیستم (فقط shutdown_release)
reason            VARCHAR(500) NULL
created_at        NOT NULL

INDEX (created_at)
CHECK (event_type NOT IN ('forced_takeover','reclaim','installation_rotate','installation_fork_reset') OR reason IS NOT NULL)
CHECK ((event_type = 'gate_mode_change') = (gate_mode_to IS NOT NULL))
```

log immutable و دائمی؛ مسیر UPDATE و DELETE ندارد (LP-70).

### ۵.۱۲ `provisioning_gate_stats`

```
node_id                        INT          NOT NULL PRIMARY KEY   -- 0 = ردیف سراسری (نویسنده‌ای که node آن معلوم نیست ؛ شمارنده‌ی mode-lock)
contention_count               BIGINT       NOT NULL DEFAULT 0     -- shadow: نویسنده‌ی instrument‌شده قفل node را آزاد ندید
writer_not_instrumented_count  BIGINT       NOT NULL DEFAULT 0     -- نویسنده‌ای که اصلاً از node_gate عبور نکرده (۱۰.۷)
mode_lock_miss_count           BIGINT       NOT NULL DEFAULT 0     -- نویسنده‌ای که نتوانست قفل مشترک gate-mode را بگیرد (فقط در off ؛ ۱۰.۶)
timeout_count                  BIGINT       NOT NULL DEFAULT 0     -- node_gate_timeout
watchdog_kill_count            BIGINT       NOT NULL DEFAULT 0
max_hold_ms                    BIGINT       NOT NULL DEFAULT 0
last_contention_at             DATETIME(6)  NULL
last_not_instrumented_at       DATETIME(6)  NULL
updated_at                     NOT NULL
version
```

دو مفهوم جدا: `contention_count` فقط telemetry است و هیچ rolloutی را نمی‌بندد؛ `writer_not_instrumented_count` و `mode_lock_miss_count` blocker واقعی ورود به `enforced`‌اند (۱۰.۶). نوشتن شمارنده‌ها best-effort و بیرون از تراکنش‌های business است و شکستش هیچ عملیاتی را رد نمی‌کند. صفرکردن دو شمارنده‌ی blocker فقط با `POST /api/provisioning/gate-stats/reset` (superadmin، بعد از رفع علت و restart همه‌ی processهای backend).

---

## ۶. State machine

### ۶.۱ عملیات

```
(T1) ──► prepared ──► provisioning ──► remote_complete ──(T_final)──► completed
              │             │                 │
              └─────────────┴─────────────────┴──► compensating ──► compensated
                                                         │
                                                         └──► cleanup_required ──(retry / force)──► compensating
```

| state | معنا | ترمینال |
|---|---|---|
| `prepared` | T1 commit شده: reservation، گام‌های staged، claimها. هیچ remote | خیر |
| `provisioning` | حداقل یک گام از `staged` بیرون رفته | خیر |
| `remote_complete` | همه‌ی گام‌های remote در `remote_created`؛ منتظر T_final | خیر |
| `completed` | T_final commit شده؛ همه‌ی reservationها `captured` | بله |
| `compensating` | در حال حذف هر چه ساخته شده | خیر |
| `compensated` | هیچ منبع remote نمانده (یا با force پذیرفته شده)؛ reservationها `released` | بله |
| `cleanup_required` | نیاز به تصمیم انسانی؛ reservationها `reserved` می‌مانند. دو علت، با `error_code` جدا: (الف) حذف یک منبع remote شکست خورده یا نبودنش اثبات نشده؛ (ب) binding اجرای approval وسط کار باطل شده (`execution_key_revoked`، `approval_binding_lost`) و ادامه فقط از `approval_recovery` است (۷.۶) | خیر |

عملیاتی که remote ندارد (`renew_*`، یا خریدی که فقط اتصال `radius_ppp` دارد) از `prepared` مستقیم به T_final می‌رود. برای `renew_*` حتی T1 و T_final یک تراکنش‌اند (بخش ۷.۴).

### ۶.۲ گام (`direction='create'`)

| از | به | چه کسی | شرط |
|---|---|---|---|
| — | `staged` | T1 | هویت و credential نوشته شد |
| `staged` | `remote_calling` | executor، یک commit **قبل از** فراخوانی remote | lease معتبر (بخش ۱۰) |
| `remote_calling` | `remote_created` | executor، یک commit بعد از موفقیت `ensure_present` | — |
| `remote_calling` | `staged` | recovery، اگر `read` بگوید منبع وجود ندارد | — |
| `remote_created` | `active` | T_final | `Connection` ساخته شد؛ `staged_*` NULL |
| `staged` | `removed` | compensation | فقط وقتی `remote_attempted = 0`: هیچ فراخوانی remote برای این گام انجام نشده (چون `remote_calling` قبل از فراخوانی commit می‌شود)، پس بدون تماس با node و با `remote_outcome` NULL حذف می‌شود. گامی که recovery از `remote_calling` به `staged` برگردانده `remote_attempted = 1` دارد و باید از مسیر `compensating` و `ensure_absent` برود |
| `staged` با `remote_attempted = 1`، `remote_calling`، `remote_created` | `compensating` | compensation | — |
| `compensating` | `removed` | executor | `ensure_absent` با نتیجه‌ی `verified_absent` یا `delete_idempotently_absent`؛ یا force با `abandoned` |
| `compensating` | `cleanup_required` | executor | شکست بعد از سقف تلاش، یا `unverified` |
| `cleanup_required` | `compensating` | retry اپراتور یا worker | — |

برای `direction='remove'` (حذف یک `Connection` موجود): `staged → compensating → removed | cleanup_required`؛ همان قواعد `ensure_absent`.

### ۶.۳ چرخه‌ی عمر credential staged

هر transition زیر credential را **در همان UPDATE** که state را عوض می‌کند NULL می‌کند (یک statement، یک commit):

| transition | چه چیزی NULL می‌شود |
|---|---|
| `remote_created → active` (T_final، بعد از کپی به `Connection`) | هر سه |
| هر transition به `compensating` | `staged_wg_private_key`، `staged_password` |
| هر transition به `cleanup_required` (از `compensating` یا مستقیم با `present_conflict`) | `staged_wg_private_key`، `staged_password` |
| `staged → removed` با `remote_attempted = 0` | هر سه |
| `compensating → removed` با `verified_absent` | هر سه |
| `compensating → removed` با `delete_idempotently_absent` | هر سه |
| `cleanup_required → removed` با force (`abandoned`) | هر سه |

- حذف remote به کلید خصوصی WireGuard و رمز نیازی ندارد (هویت با public key، نام و email است)، پس با ورود به مسیر compensation پاک می‌شوند. فقط `staged_xr_uuid` تا `removed` می‌ماند، چون تطبیق هویت Xray/3X-UI و fallback حذف با uuid به آن نیاز دارد.
- عملیات `cleanup_required` با علت approval (۷.۶) گام‌هایش در `remote_created` می‌مانند و هر سه credential را نگه می‌دارند، چون گزینه‌ی «ادامه» بدون آن‌ها `Connection` نمی‌سازد. «لغو» آن‌ها را از مسیر بالا پاک می‌کند.
- **نمایش:** هیچ پاسخ API (وضعیت عملیات، فهرست orphan، recovery)، هیچ log و هیچ `error_sanitized` ستون‌های `staged_*` را نمی‌خواند؛ serializer این جدول allowlist ستون دارد و این سه ستون در آن نیستند.
- **retention:** عملیات `cleanup_required` credential را خودکار پاک نمی‌کند (پاک‌کردن خودکار گزینه‌ی «ادامه» را از بین می‌برد). عملیاتی که بیش از آستانه (پیش‌فرض ۷ روز، تصمیم باز ۱۲) در `cleanup_required` مانده در داشبورد و گزارش روزانه‌ی superadmin با سن و علت فهرست می‌شود؛ بعد از آستانه‌ی دوم (پیش‌فرض ۳۰ روز) گزینه‌ی «ادامه» بسته و فقط «لغو» مجاز است، که credential را پاک می‌کند.

---

## ۷. مرز تراکنش‌ها

هر تراکنش محلی این را در ابتدا اجرا می‌کند، به همین ترتیب (ترتیب قفل ثابت برای همه‌ی writerها):

```
۱. locking read مشترک: wallet_runtime_state → receipt_approval_runtime_state → provisioning_type_modes(ردیف همین نوع)
۲. revalidation قفل‌های lease‌دار همین عملیات (بخش ۱۰)
۳. قفل ردیف‌های هدف (User، AdminUser)، به ترتیب (نوع، id)
```

### ۷.۱ T1: prepare (یک تراکنش، بدون هیچ remote)

```
۰. (قبل از تراکنش) acquire قفل‌های lease‌دار لازم (بخش ۱۰)
۱. locking readهای runtime و revalidation lease (ابتدای بخش ۷)
۲. اگر approval دارد (جزئیات ۷.۶):
     قفل ردیف approval:  SELECT ... FROM receipt_approvals WHERE approval_uuid = :u FOR UPDATE   (SQLite: زیر همان BEGIN IMMEDIATE)
     اگر عملیاتی با (approval_uuid, version = token) وجود دارد → بدون هیچ نوشتن، همان عملیات برمی‌گردد (ROLLBACK)
     execution token = approval.version ؛ binding instance کلید اجرا ؛ mode مؤثر ؛ state = 'registered'
     پیش‌شرط target و مقایسه‌ی manifest با مشخصات زنده (Receipt Void ۵.۳)
        اختلاف → approval.state = 'cancelled' ، cancellation_reason ، COMMIT ، 409 manifest_precondition_failed   (هیچ عملیاتی ساخته نمی‌شود)
     approval.state: 'registered' → 'mutating' ، mutating_at                                    -- هنوز commit نشده
۳. INSERT provisioning_operations(state='prepared', forward_deadline, wallet_epoch_at_start,
                                  approval_uuid, approval_execution_version, approval_key_bound, execution_key_instance_uuid)
۴. create_user: username_claim = username ؛ و بررسی این‌که User با این نام وجود ندارد            -- UNIQUE(username_claim)
۵. one-time: INSERT claimها                                                                       -- UNIQUE → رد
۶. برای هر slot (برای approval: فقط از manifest، نه از PackageConnection زنده):
     هویت و credential ساخته و INSERT provisioning_steps(state='staged')                          -- بخش ۹ ؛ WireGuard زیر قفل node
۷. reserve هر پرداخت‌کننده                                                                        -- بخش ۸
۸. COMMIT                                                                                          -- یک commit برای همه‌ی قدم‌های ۲ تا ۷
```

- T1 دقیقاً یکی از سه نتیجه را دارد: (الف) commit کامل: approval `mutating` **و** عملیات `prepared` با گام‌ها، claimها و reservationها؛ (ب) commit فقط `cancelled` برای approval، بدون هیچ ردیف عملیات؛ (ج) rollback کامل: approval همان `registered`، هیچ ردیف، هیچ پول، هیچ remote.
- حالت «approval در `mutating` بدون عملیات» از هیچ مسیری ساخته نمی‌شود، چون `registered → mutating` و INSERT عملیات یک commit‌اند. این همان «پیش‌شرط اجرا»ی Receipt Void است (اولین production call، یک تراکنش، زیر قفل ردیف approval) که عملیات P6 در همان تراکنش‌اش ساخته می‌شود؛ هیچ تراکنش جدای قبلی وجود ندارد.
- برخورد `UNIQUE(operation_type, business_key)` در رقابت دو درخواست هم‌زمان: بازنده rollback می‌کند و یک‌بار از ابتدا اجرا می‌شود؛ این بار قدم ۲ عملیات برنده را پیدا می‌کند.

### ۷.۲ هر گام remote (دو commit کوچک دور یک فراخوانی remote)

```
tx الف:  step.state 'staged' → 'remote_calling' ؛ operation.state → 'provisioning'           COMMIT
remote:  adapter.ensure_present(step)          -- بیرون از هر تراکنش DB
tx ب:    step.state → 'remote_created'                                                         COMMIT
```

crash بین الف و ب یعنی گام در `remote_calling` می‌ماند: منبع remote ممکن است ساخته شده باشد یا نه. این دقیقاً حالتی است که recovery با `read` حل می‌کند (بخش ۱۱).

### ۷.۳ T_final: یک تراکنش، بدون هیچ remote

پیش‌شرط: همه‌ی گام‌های remote در `remote_created`.

```
revalidation lease ؛ wallet epoch برابر wallet_epoch_at_start
اگر approval دارد: قفل ردیف approval ؛ state = 'mutating' ؛ version = approval_execution_version عملیات ؛
                   binding کلید اجرا برابر snapshot عملیات و کلید هنوز موجود و enabled (۷.۶) ؛ وگرنه ROLLBACK و مسیر ۷.۶
create_user: INSERT User  (username_claim تضمین کرده نام آزاد است)
existing user: قفل ردیف User ؛ بررسی این‌که هنوز وجود دارد ؛ absorb pool legacy (فقط DB)
INSERT Purchase (اگر این نوع عملیات Purchase می‌سازد)
برای هر گام: INSERT Connection از ستون‌های گام (با purchase_id و purchase_batch) ؛ step.state='active' ، connection_id ، staged_* = NULL
اعمال quota/انقضا/پکیج روی User یا Purchase
capture reservation اعتبار reseller: INSERT LedgerEntry(admin_credit_spend) ؛ reservation 'captured'
INSERT LedgerEntry فروش (flush) ؛ capture reservation کیف‌پول مشتری با همان ledger_entry_id (بخش ۸)
one-time claimها: purchase_id
(اگر approval دارد):
    record_effect برای هر ردیف required manifest که این عملیات ساخته: user_created ، purchase_created ، connection_created (هر slot) ،
                  purchase_renewed ، ledger_sale ، و ledger_credit_spend اگر در manifest هست
    اعمال هر ردیف optional manifest که شرطش برقرار است، فقط-DB و از مقادیر منجمد manifest: discount_redeemed ، card_payment_recorded ،
                  پاداش‌های referral ، و بعد از P9a مدرک پرداخت و loyalty ؛ هرکدام با record_effect
    اعتبارسنجی manifest با همان سه بررسی finalize سند Receipt Void (مجموعه‌ی (effect_type, effect_key) ؛ actual_projection در برابر expected ؛
                  فیلدهای immutable منابع گروه ۱) ؛ هر ناهم‌خوانی → ROLLBACK کل T_final
    approval.state: 'mutating' → 'completed' ، completed_at
operation: state='completed' ، result_* ، sale_ledger_entry_id ، username_claim = NULL
COMMIT
```

شبه‌کد بالا قرارداد approval ثبت‌شده زیر `required` (و عملیات بدون approval) است: همه یک commit‌اند، یعنی `User`/`Purchase`/`Connection`، ردیف‌های Ledger، mutationهای referral/تخفیف/کارت، ردیف‌های `receipt_approval_effects`، capture هر reservation، `completed` شدن عملیات و `completed` شدن approval. شکست تزریق‌شده در هر نقطه، از جمله هر شکست `record_effect` یا اعتبارسنجی manifest، یعنی هیچ‌کدام: نه عملیات `completed` می‌شود و نه approval. قرارداد `shadow` جداست و در ۷.۷ آمده.

**هیچ تابعی که از T_final صدا زده می‌شود `commit`، `rollback` یا فراخوانی remote ندارد.** (savepoint فقط در مسیر shadow، ۷.۷.) برای هر تابع موجودی که امروز commit داخلی دارد یک **mutation-core بدون commit** ساخته می‌شود؛ wrapper موجود با همان نام و امضا فقط core را صدا می‌زند و سپس دقیقاً یک commit می‌کند، پس مسیر `legacy` و همه‌ی callerهای فعلی بدون تغییر رفتار می‌مانند. مسیر durable فقط core را صدا می‌زند.

| core بدون commit | wrapper legacy (همان نام امروز) | چه می‌نویسد |
|---|---|---|
| `create_user_record_core` | `create_user_record` | `User`، شمارنده‌ی خرید و loyalty |
| `absorb_legacy_pool_core` | `absorb_legacy_pool_into_purchase` | انتقال pool legacy به `Purchase` |
| `build_purchase_core` | بدنه‌ی `apply_package_as_purchase` | `Purchase`، quota/انقضا، شمارنده‌ی خرید و loyalty |
| `renew_user_core`، `renew_purchase_core` | `renew_user`، `renew_purchase` | تمدید (رزروشده یا فوری) |
| `accounting.record_core` | `accounting.record` | `LedgerEntry` |
| `apply_referral_code_core` | `apply_referral_code` (`user_ops.py:156`) | `referred_by_id`، `referral_reward_granted`، پاداش اعتباری و حجمی هر دو طرف |
| `redeem_discount_code_core` | `redeem_discount_code` (`user_ops.py:257`) | `DiscountCode.used_count`، `DiscountCodeRedemption` |
| `advance_after_payment_core` | `advance_after_payment` (`payment_cards.py:100`) | `PaymentCard.accumulated_amount`، pointer کارت فعال، و بعد از فاز مربوط در Receipt Void ردیف `payment_card_pool_events` |
| reserve/capture/release اعتبار reseller (بخش ۸) | `debit_admin`، `refund_for_package` | `AdminUser.balance`، Ledger |

قواعد سه core جدید:

- **referral.** در مسیر approvalدار معرف، مقدار هر پاداش و مقصد پاداش حجمی از ردیف‌های منجمد manifest خوانده می‌شود، نه از `PanelSettings` زنده. تغییر موجودی از `wallet_service` می‌گذرد (شاخه‌ی legacy قبل از cutover، credit source بعد از آن) و آن هم بدون commit. ردیف `User` معرف و مشتری به ترتیب `id` قفل می‌شوند.
- **تخفیف.** افزایش `used_count` دیگر read-modify-write در حافظه نیست؛ یک UPDATE شرطی است: `UPDATE discount_codes SET used_count = used_count + 1 WHERE id = :id AND (max_uses IS NULL OR used_count < max_uses)`؛ `rowcount = 0` یعنی شرط optional برقرار نیست (۷.۶). wrapper legacy همین core را صدا می‌زند، پس مسیر امروز هم اتمیک می‌شود.
- **کارت.** core همان قرارداد نویسنده‌ی کارت در Receipt Void ۱۵.۲ را اجرا می‌کند، بدون commit خودش: lease `payment_card_pool:{pool_key}` قبل از تراکنش گرفته و در ابتدای T_final revalidate می‌شود؛ locking read روی `payment_card_pool_states`؛ سپس منطق زنده‌ی امروز و (در `event_logged`) INSERT event. ترتیب lease: بعد از `customer_identity:*` و قبل از `node:*` و `provisioning_op:*` (بخش ۱۰.۱).
- loyalty و پاداش loyalty هم داخل `create_user_record_core`/`build_purchase_core`/`renew_*_core` و در همان T_final‌اند.

**گیت بسته.** LP-11 فقط فهرست نام‌ها را نمی‌بیند: LP-95 از تابع ورودی T_final (و T1، T_comp و تمدید تک‌تراکنشی) گراف فراخوانی را روی کد پروژه تا بسته‌شدن دنبال می‌کند و هر `commit`، `rollback`، `close`، ساخت client remote، `client_for_node`/`for_node`، یا import شبکه (`requests`، `paramiko`، `librouteros`، `socket`) را در هر تابع قابل‌دسترس رد می‌کند. فراخوانی‌ای که resolve نمی‌شود (dispatch پویا) خطاست، نه چشم‌پوشی. همان تست در زمان اجرا هم می‌سنجد: شمارنده‌ی رویداد `after_commit` روی Session برای یک T_final دقیقاً ۱ است و guard سوکت هیچ اتصالی نمی‌بیند.

**retry.** T_final با `UPDATE provisioning_operations SET state = 'completed' ... WHERE id = :id AND state = 'remote_complete' AND version = :v` بسته می‌شود. T_final ناموفق کاملاً rollback شده، پس retry از صفر و بدون اثر تکراری اجرا می‌شود؛ T_final موفق را retry با `rowcount = 0` می‌بیند و هیچ referral، redemption یا event کارت دومی نمی‌سازد.
- شکست T_final یعنی rollback همان تراکنش؛ عملیات در `remote_complete` می‌ماند، reservationها `reserved`، و منبع‌های remote ساخته‌شده با گام‌های `remote_created` ثبت‌اند. پس **هیچ orphan و هیچ سرویس رایگانی** ساخته نمی‌شود: یا T_final دوباره موفق می‌شود، یا compensation همان گام‌ها را حذف می‌کند.

### ۷.۴ تمدید (`renew_user`، `renew_purchase`)

تمدید فراخوانی remote لازم ندارد. کل عملیات **یک تراکنش** است: (اگر approval دارد: قدم ۲ از T1، یعنی قفل، token، binding، پیش‌شرط و `registered → mutating`)، INSERT عملیات، کسر پول و ردیف reservation مستقیم در `captured`، اعمال تمدید، LedgerEntry فروش، (effectها، اعتبارسنجی manifest و approval `completed`)، عملیات `completed`. پس approval تمدید در یک commit از `registered` به `completed` می‌رسد یا دست‌نخورده می‌ماند. بعد از commit، `reconcile_user_connections`/`reconcile_purchase_connections` موجود بهترین‌تلاش اجرا می‌شود (فعال‌کردن دوباره‌ی اتصال‌ها)؛ شکست آن تمدید را برنمی‌گرداند و poller همان را در چرخه‌ی بعد اعمال می‌کند، مثل امروز.

### ۷.۵ جدول مسیرها

| مسیر | `operation_type` | reservationها | گام‌ها |
|---|---|---|---|
| بات `create_user` با رسید | `create_user` | اعتبار reseller (اگر مالک شارژ می‌شود) | slotهای `request_slot:*` |
| بات `purchase_package` با رسید | `purchase` | اعتبار reseller | `package_connection:*` یا `request_slot:*` |
| خرید با کیف‌پول (مینی‌اپ، بات مشتری) | `create_user` یا `purchase` | کیف‌پول مشتری + اعتبار reseller | همان |
| پنل `create_user` با پکیج | `create_user` | اعتبار reseller ادمین انجام‌دهنده | `package_connection:*` |
| پنل ساخت گروهی | N عملیات `create_user`، هرکدام `bulk:{token}:{i}` | هرکدام جدا | هرکدام جدا |
| پنل `apply_package` | `purchase` | اعتبار reseller | `package_connection:*` |
| `add_connection` | `add_connection` | — | یک slot |
| تمدیدها | `renew_user` / `renew_purchase` | طبق ۷.۴ | — |

ساخت گروهی دیگر شارژ batch و refund batch ندارد: هر عضو موفق کامل است و هر عضو ناموفق هیچ اثری ندارد؛ پاسخ تعداد موفق و فهرست ناموفق‌ها را با دلیل می‌دهد.

### ۷.۶ اتصال به ReceiptApproval

approval رسیدی از نوع `new` و `renew` دقیقاً با یک عملیات این سند اجرا می‌شود (`topup` provisioning ندارد و خارج از این سند است). قراردادهای Receipt Void که این‌جا استفاده می‌شوند: `receipt_approvals.version` = execution token؛ `execution_key_instance_uuid` = تنها instance کلید مجاز به اجرا (NULL = فقط in-process)؛ قفل ردیف approval اولین statement هر تراکنش؛ effect فقط با `record_effect` و فقط در `state='mutating'`؛ `failed` فقط بدون effect؛ بعد از `mutating` takeover وجود ندارد و اگر کلید وسط اجرا revoke شود ادامه فقط از `approval_recovery` است.

**چه تراکنشی کدام state approval را می‌نویسد:**

| تراکنش | نوشتن روی approval | هم‌commit با |
|---|---|---|
| T1 | `registered → mutating`؛ یا `registered → cancelled` | INSERT عملیات، گام‌ها، claimها، reservationها (برای `cancelled`: هیچ) |
| گام‌های remote (۷.۲) | هیچ | — |
| T_final | `mutating → completed` | ردیف‌های نهایی، Ledger، effectها، capture، عملیات `completed` |
| T_comp (۱۱.۵) | `mutating → failed`، `failed_at` | release reservationها، آزادسازی claimها، عملیات `compensated` |

**بررسی binding در هر تراکنش عملیات approvalدار** (T1، T_final، T_comp)، زیر قفل ردیف approval:

```
approval.version = operation.approval_execution_version                     وگرنه approval_superseded
approval.execution_key_instance_uuid برابر snapshot عملیات (approval_key_bound / execution_key_instance_uuid)   وگرنه execution_principal_mismatch
اگر approval_key_bound = 1: ردیف ApiKey با همان key_instance_uuid در همین تراکنش وجود دارد و enabled = True   وگرنه execution_key_revoked
درخواست sync: principal فراخواننده همان binding را دارد (کلید با همان UUID ؛ یا in-process وقتی approval_key_bound = 0)
worker: principal ندارد ؛ فقط سه بررسی اول
mode مؤثر receipt_approval_runtime_state 'blocked' نیست   وگرنه 503 receipt_approval_blocked ، بدون هیچ نوشتن
```

**اگر binding بعد از T1 باطل شود** (کلید revoke/حذف/rotate شد، یا ردیف approval با snapshot نمی‌خواند): طبق Receipt Void approval در `mutating` می‌ماند. این سند هم نه T_final اجرا می‌کند و نه T_comp:

- عملیات در یک تراکنش کوچک به `cleanup_required` با `error_code = 'execution_key_revoked'` (یا `approval_binding_lost`) می‌رود و یک `wallet_operations` از نوع `approval_recovery` با `business_key = approval_uuid` ساخته می‌شود (همان جدول و همان کلید Receipt Void؛ تکرار همان ردیف را برمی‌گرداند).
- reservationها `reserved` و منبع‌های remote ساخته‌شده با گام‌های `remote_created` ثبت می‌مانند؛ هیچ فراخوانی remote تازه‌ای انجام نمی‌شود.
- superadmin از پنل (رمز و دلیل) یکی را انتخاب می‌کند: **ادامه** → همان T_final، با این تفاوت که بررسی کلید با مجوز `approval_recovery` جایگزین می‌شود و actor و دلیل روی همان recovery ثبت می‌شود؛ **لغو** → `compensating` و سپس T_comp با همان جایگزینی.

**retry و نسخه.** بعد از T_comp، approval `failed` و بدون effect است. retry کنترل‌شده‌ی Receipt Void آن را `registered` با `version + 1` می‌کند و token تازه می‌دهد؛ T1 بعدی `business_key = approval:{uuid}:v:{version جدید}` می‌سازد، یعنی عملیات تازه با گام‌ها، هویت‌ها و credentialهای تازه. درخواست با token قدیمی ۴۰۹ `approval_superseded` می‌گیرد و عملیات قدیمی را هم برنمی‌گرداند (state واقعی approval در پاسخ است).

**takeover و revoke، قبل و بعد از `mutating`:**

| لحظه | رویداد | نتیجه |
|---|---|---|
| قبل از T1 | takeover (version + 1) | T1 با token قدیمی: ۴۰۹ `approval_superseded`، rollback، هیچ عملیات. T1 با token تازه و کلید تازه: عادی |
| قبل از T1 | revoke کلید اجرا | T1: ۴۰۳ `execution_key_revoked`، rollback؛ approval `registered` می‌ماند و فقط takeover دستی آن را جلو می‌برد |
| هم‌زمان با T1 | takeover | هر دو ردیف approval را قفل می‌کنند؛ دقیقاً یکی commit می‌کند (قرارداد Receipt Void ۶.۵) |
| بعد از T1 | takeover | ۴۰۹ (state `mutating`)؛ عملیات عادی ادامه می‌یابد |
| بعد از T1 | revoke کلید اجرا | مسیر `cleanup_required` + `approval_recovery` بالا |

**effectهای optional و فراخوانی‌های جدای بات.** امروز بات بعد از production call اصلی، `apply_referral`، `redeem_discount` و `record_card_payment` را جدا صدا می‌زند (`telegram_bot/handlers/admin_pending.py:277`، `:282`، `:308`، `:382`). چون effect فقط در `mutating` نوشته می‌شود و T_final approval را `completed` می‌کند، در مسیر durable این effectها داخل T_final و از مقادیر منجمد manifest اعمال می‌شوند. همان فراخوانی‌های جدا با `approval_uuid` بعد از آن:

- پاسخ از `provisioning_operations.optional_effects` همان عملیات ساخته می‌شود، نه از بود و نبود ردیف effect: `outcome='applied'` → ۲۰۰ با همان نتیجه؛ `outcome='skipped'` (شرط optional برقرار نبود، مثلاً کد تخفیف نامعتبر) → ۲۰۰ با `applied: false`؛ همان رفتار best-effort امروز.
- در هیچ حالتی mutation تجاری دوباره اجرا نمی‌شود، حتی اگر در `shadow` ردیف effect آن نوشته نشده باشد (۷.۷).

شرط هر optional قبل از نوشتن با یک بررسی بدون اثر جانبی سنجیده می‌شود؛ برقرار نبود → رد می‌شود و ردیفی نوشته نمی‌شود. خطای غیرمنتظره در اعمال یک optional کل T_final را rollback می‌کند (نه این‌که بی‌صدا رد شود).

**mode `shadow`.** قرارداد جدا و کامل در ۷.۷.

**سه نقطه که تفسیر متن frozen است و باید تأیید شود** (هیچ‌کدام سند Receipt Void را تغییر نمی‌دهد):

0. آمادگی P6 به‌عنوان پیش‌شرط activation `required` (۷.۷).
1. Receipt Void می‌گوید `completed` و `failed` را `finalize` می‌نویسد و `failed` وقتی است که «بات صریحاً شکست را گزارش کند». این سند همان بررسی‌ها و همان نویسنده را **داخل** T_final و T_comp صدا می‌زند؛ گزارش‌دهنده‌ی شکست، اجراکننده‌ی عملیات (درخواست sync یا worker) است، نه بات. endpoint `finalize` بات بعد از آن فقط state واقعی را برمی‌گرداند.
2. اعمال effectهای optional داخل T_final و پاسخ idempotent فراخوانی‌های جدای بات (بالا).

**سازگاری‌سنجی.** worker در هر چرخه این دو را هم می‌سنجد و برای هرکدام فقط یک `approval_recovery` در `cleanup_required` می‌سازد، بدون هیچ تغییر خودکار state:

- approval `mutating` از نوع `new`/`renew` که هیچ عملیاتی با `(approval_uuid, version)` آن وجود ندارد (از مسیر عادی ساخته نمی‌شود؛ یعنی دستکاری یا باگ)؛
- عملیات ترمینال که approval آن state متناظر را ندارد (`completed` با approval غیر-`completed`؛ `compensated` با approval غیر-`failed` در همان version).

### ۷.۷ T_final برای approval `shadow`

`registered_under_mode` روی approval immutable است و قرارداد T_final را تعیین می‌کند. یک approval فقط یکی از دو ستون زیر را می‌گیرد:

| | `required` (و عملیات بدون approval) | `shadow` |
|---|---|---|
| mutation تجاری، capture، عملیات `completed` | فقط اگر همه‌ی effectها و manifest معتبر باشند | همیشه (اگر خود mutation تجاری موفق است) |
| شکست `record_effect` | ROLLBACK کل T_final | فقط همان savepoint برمی‌گردد؛ یک ردیف `receipt_approval_shadow_events` با `stage='effect'` |
| اعتبارسنجی manifest ناموفق | ROLLBACK کل T_final | approval `mutating` می‌ماند؛ ردیف `receipt_approval_shadow_events` با `stage='finalize'`؛ `approval_recovery` |
| اعتبارسنجی manifest موفق | approval `completed` | approval `completed` |
| reservation بعد از T_final | `captured` | `captured` |

```
T_final (shadow):                                  -- یک تراکنش ، یک commit
  همان قدم‌های ۷.۳ تا قبل از effectها
  برای هر effect:        SAVEPOINT ؛ record_effect ؛ شکست → ROLLBACK TO SAVEPOINT + INSERT receipt_approval_shadow_events
  اعتبارسنجی manifest:
      موفق   → approval.state 'mutating' → 'completed'
      ناموفق → approval دست نمی‌خورد (mutating) ؛ INSERT receipt_approval_shadow_events(stage='finalize')
               INSERT wallet_operations(operation_type='approval_recovery', business_key=approval_uuid, state='cleanup_required')
                      (اگر از قبل هست: همان ردیف)
  operation: state='completed' ، optional_effects
COMMIT
```

- **بدون پنجره.** `completed` شدن عملیات و INSERT `approval_recovery` در یک commit‌اند. حالتی که عملیات `completed` باشد، approval `mutating` بماند و recovery ثبت نشده باشد از هیچ مسیر و هیچ crashی ساخته نمی‌شود؛ outbox لازم نیست.
- **mutation تجاری optional در shadow.** شکست savepoint فقط INSERT ردیف effect را برمی‌گرداند؛ mutation تجاری همان optional (مثلاً redemption) قبل از savepoint نوشته شده و می‌ماند. `optional_effects` همین را ثبت می‌کند (`outcome='applied'`)، پس فراخوانی جدای بات آن را دوباره اجرا نمی‌کند.
- **درخواست تکراری.** عملیات `completed` است: پاسخ از ردیف‌های نتیجه بازسازی می‌شود؛ نه فروش، نه capture و نه effect ناموفق دوباره اجرا نمی‌شود. تکمیل effectهای گم‌شده فقط کار `approval_recovery` است.
- **T_comp در shadow** همان ۱۱.۵ است (approval بدون effect → `failed`).
- **خطای زیرساختی** (خطای DB، deadlock، شکست خود mutation تجاری) در هر دو mode کل T_final را rollback می‌کند؛ تحمل shadow فقط برای شکست correlation است.

**policy برای activation `required`.** این سند یک predicate آمادگی می‌دهد که فقط می‌خواند:

```
p6_ready() =  همه‌ی operation_typeها 'durable'
          و  gate_mode = 'enforced' و owner_state = 'active' و مالک همین host (۱۰.۴، ۱۰.۶)
          و  برای هر node با enabled = True: ردیف provisioning_node_contracts همان (node, backend) در state 'ready'
             با adapter_version و config_fingerprint فعلی (۵.۱۰)
          و  هیچ عملیات غیرترمینال با approval ثبت‌شده زیر 'shadow'
          و  هیچ approval 'mutating' ثبت‌شده زیر 'shadow' که عملیات 'completed' دارد
          و  هیچ wallet_operations از نوع approval_recovery در state غیرترمینال که approval آن زیر 'shadow' ثبت شده
```

activation مشترک Receipt Void (P9a) پیش‌شرط «P9» دارد و P9 پشت «P6 کامل» است؛ این سند «P6 کامل» را همین `p6_ready()` تعریف می‌کند. اگر برقرار نباشد activation باید fail-closed رد شود و فهرست approvalها و recoveryهای باز را برگرداند؛ superadmin اول آن‌ها را با `approval_recovery` می‌بندد. این تعریف پیش‌شرطی است که سند frozen نامش را برده ولی محتوایش را به P6 سپرده؛ کد پاسخ (پیشنهاد: ۴۰۹ `provisioning_not_ready`) در فهرست کدهای آن سند نیست و نقطه‌ی تفسیر شماره‌ی ۰ در ۷.۶ است. بدون این policy، approval `shadow` نیمه‌کاره بعد از activation طبق خود Receipt Void ۴۰۹ `approval_mode_changed` می‌گیرد و `cancelled` می‌شود، در حالی که فروشش انجام شده است.

---

## ۸. Reservation: reserve / capture / release

| عمل | `customer_wallet` / `legacy_balance` (قبل از cutover کیف‌پول) | `customer_wallet` / `wallet_lot` (بعد از cutover) | `reseller_credit` / `admin_balance` |
|---|---|---|---|
| **reserve** (در T1) | UPDATE شرطی اتمیک `User.balance = balance - amount WHERE balance >= amount` از شاخه‌ی legacy `wallet_service`؛ rowcount=0 → ۴۰۰ موجودی ناکافی | `wallet_service.hold(account, amount, hold_ref)`: `wallet_debit_events(state='held')` و allocationهای FIFO | UPDATE شرطی اتمیک روی `AdminUser.balance` با همان قاعده‌ی سقف اعتبار `debit_admin`، **بدون commit داخلی و بدون ردیف Ledger** |
| **capture** (در T_final) | reservation `captured` با `capture_ledger_entry_id` = LedgerEntry فروش | `wallet_service.capture(hold_ref, ledger_entry)`؛ reservation `captured` | INSERT یک LedgerEntry `admin_credit_spend` (تنها ردیف مبلغ‌دار این شارژ)؛ reservation `captured` |
| **release** (در compensation) | UPDATE `User.balance = balance + amount`؛ reservation `released` | `wallet_service.release(hold_ref)`؛ reservation `released` | UPDATE `AdminUser.balance = balance + amount`؛ reservation `released`؛ **هیچ ردیف Ledger** |

- capture و release هر دو `UPDATE payment_reservations ... WHERE id = :id AND state = 'reserved'` هستند؛ فقط یکی می‌برد و تکرار بی‌اثر است.
- **بدون double-count:** شارژ reseller فقط یک ردیف Ledger می‌سازد، آن هم در capture. reservation آزادشده هیچ ردیف Ledger ندارد، پس نه spend شمرده می‌شود نه refund. `refund_for_package` فقط برای برگشت یک فروش **captured** می‌ماند (مثل ابطال رسید).
- موجودی reseller در مدت reservation کم شده است؛ مبلغ رزروشده از `SUM(amount WHERE admin_id = X AND state = 'reserved')` قابل نمایش است.
- `debit_admin` برای مسیر durable صدا زده نمی‌شود (commit داخلی دارد، از جمله در شکست). منطق سقف اعتبارش در یک تابع بدون commit استخراج می‌شود و `debit_admin` موجود برای مسیر legacy همان را صدا می‌زند.
- معافیت‌ها همان امروز: superadmin، حساب volume-billed، trial و مشتری بدون مالک reservation ندارند.
- **تغییر سیاست نسبت به امروز:** بات امروز reseller را **بعد از** تحویل شارژ می‌کند. در مسیر durable اعتبار در T1 رزرو می‌شود؛ اگر اعتبار کافی نباشد عملیات قبل از هر تحویل رد می‌شود و رسید مشتری در صف می‌ماند (بخش ۱۸).

**قرارداد cutover کیف‌پول (Receipt Void ۲۰.۳):** تراکنش cutover بعد از گرفتن قفل انحصاری `wallet_runtime_state` این را می‌سنجد و در صورت وجود rollback می‌کند:

```
SELECT COUNT(*) FROM payment_reservations WHERE payer_kind = 'customer_wallet' AND state = 'reserved'
```

چون هر T1 و T_final همان ردیف را با locking read می‌خواند و `wallet_epoch_at_start` را نگه می‌دارد، عملیاتی که reservation legacy دارد نمی‌تواند از cutover عبور کند: یا قبل از آن ترمینال شده، یا cutover را متوقف می‌کند.

---

## ۹. Adapter هر backend و idempotency

هر backend سه عمل دارد. هویت و credential همیشه از ستون‌های گام خوانده می‌شود، هرگز دوباره تولید نمی‌شود.

```
read(step)            → 'present_match' | 'present_conflict' | 'absent' | 'unreadable'
ensure_present(step)  → موفق (منبع با هویت و credential همین گام وجود دارد) | خطای قابل‌retry | خطای قطعی (conflict)
ensure_absent(step)   → 'verified_absent' | 'delete_idempotently_absent' | 'unverified'
```

`ensure_present` و `ensure_absent` (و `read`ی که مبنای تصمیم آن‌هاست) همیشه **داخل gate مالکیت همان node** اجرا می‌شوند (بخش ۱۰.۲)؛ هیچ adapter خودش قفل نمی‌گیرد و هیچ mutation remote بیرون از gate وجود ندارد.

`present_conflict` یعنی چیزی با بخشی از همین هویت وجود دارد ولی با بقیه نمی‌خواند. در این حالت adapter **هیچ‌چیز را تغییر یا حذف نمی‌کند** و گام با `error_code='remote_identity_conflict'` به `cleanup_required` می‌رود.

### ۹.۱ `mikrotik_wg`

| موضوع | قرارداد |
|---|---|
| هویت | چهارتایی `(wg_interface, wg_peer_name به‌عنوان comment, wg_public_key, wg_client_address)` |
| تولید در T1 | کلید با `generate_wireguard_keypair`؛ `wg_peer_name = user-{username}-{۶ hex}`؛ IP با `_wg_reserve_ips` زیر قفل `node:{id}:wg_pool` |
| IP رزروشده | lease `node:{id}:wg_pool` فقط همین تخصیص DB را سریال می‌کند. `_wg_used_ips` آدرس گام‌های غیرترمینال همان node را هم می‌شمارد؛ دو عملیات هم‌زمان IP یکسان نمی‌گیرند |
| بزرگ‌شدن subnet | `node.mt_client_subnet` در خود T1 نوشته می‌شود (نه با commit جدا)؛ `wg_subnet_expanded = 1` فقط audit است. compensation مقدار DB را برنمی‌گرداند: subnet فقط بزرگ می‌شود. همگام‌کردن router با DB کار «قرارداد آدرس interface» پایین است، نه کار گامی که subnet را بزرگ کرده |
| `read` | `list_peers(interface)`: peer با همان `public-key` **یا** همان comment. هر چهار جزء برابر → `present_match`؛ فقط بخشی برابر → `present_conflict`؛ هیچ → `absent`؛ خطای اتصال → `unreadable` |
| `ensure_present` | داخل gate node: `ensure_wireguard_interface`؛ **قرارداد آدرس interface** (پایین)؛ `read`: اگر `absent` → `add_peer`؛ اگر `present_match` → هیچ؛ سپس اگر `speed_limit_mbps` هست `upsert_simple_queue` (با نام ثابت، upsert) |
| `ensure_absent` | `read`: `present_match` → `remove_peer` با `.id` همان لحظه، سپس `read` دوباره؛ `absent` → `verified_absent`؛ `remove_simple_queue`؛ `present_conflict` → `unverified`. **هرگز** به آدرس یا prefix interface دست نمی‌زند و قرارداد آدرس را اجرا نمی‌کند |
| نکته | `.id` RouterOS هرگز ذخیره نمی‌شود |

**قرارداد آدرس interface (repair drift بین DB و RouterOS).** امروز `ensure_interface_address` اگر interface هر آدرسی داشته باشد بدون مقایسه برمی‌گردد (`mikrotik_client.py:153-155`) و فقط گامی که خودش subnet را بزرگ کرده `set_interface_address` را صدا می‌زند (`user_ops.py:969-976`). پس اگر DB بزرگ شود ولی router نه، هیچ گام بعدی آن را اصلاح نمی‌کند. قرارداد جدید:

```
ensure_interface_address_exact_or_wider(mt, node):            -- داخل gate node ؛ از ensure_present و از reconciliation
  desired = مقدار فعلی Node.mt_client_subnet ، خوانده‌شده از DB در یک تراکنش کوتاه فقط-خواندنی ، بعد از گرفتن gate
            (نه wg_gateway_with_prefix گام ، نه مقدار کش‌شده‌ی session)
  desired_addr = "{desired.network_address + 1}/{desired.prefixlen}"
  rows = آدرس‌های /ip/address همان interface روی router
  rows خالی                                              → add(desired_addr)
  دقیقاً یک ردیف با شبکه‌ی N:
      N = desired                                        → هیچ
      N زیرشبکه‌ی اکید desired (router باریک‌تر از DB)   → جایگزینی با desired_addr                    -- repair
      desired زیرشبکه‌ی اکید N (router پهن‌تر از DB)     → هیچ تغییری روی router ؛ log هشدار wg_interface_wider_than_db
      N و desired بی‌ربط یا هم‌پوشان ناقص               → خطای قطعی wg_interface_address_conflict ؛ بدون تغییر
  بیش از یک ردیف                                         → خطای قطعی wg_interface_address_conflict ؛ بدون تغییر
  سپس: هر آدرس wg_client_address گام باید داخل desired باشد ؛ وگرنه خطای قطعی wg_ip_outside_subnet ؛ peer ساخته نمی‌شود
```

- **فقط به سمت مقدار فعلی DB و فقط پهن‌تر.** helper هیچ‌وقت prefix router را بلندتر (باریک‌تر) نمی‌کند. گام قدیمی که در T1 خودش `/24` دیده، بعد از این‌که DB `/23` شده، `desired = /23` را می‌خواند؛ snapshot `/24` خودش هیچ نقشی ندارد، پس نمی‌تواند router را کوچک کند.
- **drift repair می‌شود.** عملیات A subnet را در T1 `/23` کرد و قبل از تماس با router شکست خورد یا compensate شد: DB `/23`، router `/24`. عملیات بعدی B (که `wg_subnet_expanded = 0` دارد) در `ensure_present` خود `N=/24 ⊂ desired=/23` را می‌بیند و قبل از `add_peer` جایگزین می‌کند.
- **جایگزینی** همان دنباله‌ی `set_interface_address` امروز است (حذف ردیف، سپس افزودن؛ `mikrotik_client.py:170-177`). اگر بعد از حذف و قبل از افزودن شکست بخورد، interface بدون آدرس می‌ماند و تلاش بعدی شاخه‌ی «rows خالی» را اجرا می‌کند؛ پس همگراست. تا آن تلاش، peerهای موجود همان node مسیر ندارند؛ این پنجره همان رفتار امروز `set_interface_address` است و در staging اندازه‌گیری می‌شود (۹.۶).
- **چرا executor قدیمی نمی‌تواند router را باریک کند.** خواندن `desired`، خواندن آدرس router و جایگزینی هر سه داخل یک نگه‌داشت gate‌اند، و gate منقضی نمی‌شود (بخش ۱۰.۲). executorی که متوقف شده هنوز gate را دارد، پس هیچ executor دیگری در آن مدت آدرس همان interface را نمی‌نویسد؛ وقتی برگردد همان `desired` را اعمال می‌کند که هنوز هیچ‌کس از آن جلو نزده. اگر process بمیرد gate آزاد می‌شود و نوشتن نیمه‌کاره‌ای در راه نیست. `Node.mt_client_subnet` فقط در T1 و فقط بزرگ‌تر می‌شود؛ اگر T1 دیگری بعد از خواندن `desired` آن را بزرگ‌تر کند، این executor router را به مقداری می‌رساند که از مقدار قبلی router پهن‌تر و از DB جدید باریک‌تر است (هرگز کوچک‌کردن نیست) و نگه‌داشت بعدی gate آن را کامل می‌کند.
- **drift بدون عملیات بعدی.** درستی به «عملیات بعدی روی همان node» وابسته نیست: reconciliation دوره‌ای و زمان startup همین helper را برای هر node اجرا می‌کند (بخش ۱۰.۵).
- **ویرایش دستی node.** اگر ادمین `mt_client_subnet` را از پنل کوچک کند، router پهن‌تر می‌ماند (شاخه‌ی سوم) و گام‌هایی که IP بیرون از subnet جدید دارند با `wg_ip_outside_subnet` compensate می‌شوند؛ این سند ویرایش دستی subnet را بازطراحی نمی‌کند.

### ۹.۲ `radius_ppp` (OpenVPN، L2TP، IKEv2، SSTP، PPTP)

هیچ فراخوانی remote. در T1 `account_username` و `staged_password` ساخته می‌شود؛ در T_final ردیف `Connection`. `read`/`ensure_*` وجود ندارد. حذف فقط DB است. قطع session برقرار با `kick_ppp_session` بهترین‌تلاش بعد از commit حذف.

### ۹.۳ `xray_ssh`

| موضوع | قرارداد |
|---|---|
| هویت | `(xr_inbound_tag, xr_email, staged_xr_uuid)`. uuid credential است و فقط در ستون staged |
| تولید در T1 | `xr_email` و uuid هر دو در T1 ساخته می‌شوند. uuid به `add_client(client_uuid=...)` داده می‌شود (client آن را می‌پذیرد)؛ دیگر client آن را نمی‌سازد |
| `read` | `read_config`: client با همان email در همان inbound؛ `id` برابر → `present_match`؛ متفاوت → `present_conflict` |
| `ensure_present` | داخل gate node: `read`؛ `absent` → `add_client`؛ `present_match` → هیچ (بدون نوشتن config و بدون restart) |
| `ensure_absent` | داخل gate node: `read`؛ `present_match` → `remove_client` و `read` دوباره → `verified_absent`؛ `absent` → `verified_absent` بدون نوشتن و بدون restart |
| نکته | هر `add_client`/`remove_client` کل `config.json` را بازنویسی و سرویس را restart می‌کند. خواندن config، تغییر و نوشتن آن یک نگه‌داشت gate است؛ چون gate منقضی نمی‌شود، هیچ عملیات دیگری (durable، legacy، poller یا ویرایش دستی از پنل) بین خواندن و نوشتن این executor همان فایل را نمی‌نویسد، پس lost update ممکن نیست. خواندن قبل از نوشتن جلوی restart بی‌دلیل در retry را می‌گیرد |

### ۹.۴ `threexui`

| موضوع | قرارداد |
|---|---|
| هویت | `(inbound_id node, xr_email, staged_xr_uuid)` |
| `read` | `_get_inbound` → `settings.clients`: email برابر و `id` برابر → `present_match`؛ email برابر و `id` متفاوت → `present_conflict` |
| `ensure_present` | `read`؛ `absent` → `add_client(client_uuid=...)`؛ سپس `read` دوباره باید `present_match` بدهد، وگرنه خطای قابل‌retry |
| `ensure_absent` | `read`؛ `present_match` → `remove_client(email, uuid)`، سپس `read` دوباره → `verified_absent`؛ اگر `read` دوم هنوز client را ببیند → `unverified` |
| نکته | پاسخ خود `add`/`remove` (از جمله `_try_post` که فقط bool می‌دهد) مبنای تصمیم نیست؛ فقط `read` بعد از آن |

### ۹.۵ `softether`

| موضوع | قرارداد |
|---|---|
| هویت | `(se_hub_name node, account_username)` |
| متد جدید client | `user_exists(username)`: یک فراخوانی `EnumUser` و جست‌وجوی `Name_str`، **بدون** فیلتر `IsTrafficFilled_bool`. `query_all_user_stats` موجود تغییر نمی‌کند |
| `read` | `user_exists` → `present_match` یا `absent`. رمز قابل خواندن نیست، پس `present_conflict` ندارد |
| `ensure_present` | `read`؛ `absent` → `add_client(username, staged_password)`؛ `present_match` → `set_client_enabled(username, staged_password, True)` که `SetUser` با همان رمز است و رمز را به مقدار staged همگرا می‌کند |
| متد جدید client | `delete_user_typed(username)`: یک فراخوانی `DeleteUser` که نتیجه‌ی تایپ‌شده برمی‌گرداند: `deleted` (پاسخ JSON-RPC با `result` و بدون `error`)، `not_exist` (پاسخ JSON-RPC با `error.code` که در فهرست تأییدشده‌ی staging است)، `ambiguous` (هر چیز دیگر). `remove_client` موجود و تطبیق متنی `"not exist"` آن (`softether_client.py:177`) در مسیر durable استفاده **نمی‌شود** |
| `ensure_absent` | جدول پایین |

**نتیجه‌ی `ensure_absent` برای SoftEther** (مطابق تعریف سه outcome در Receipt Void ۱۳.۴):

| `DeleteUser` | `EnumUser` بعد از آن | `remote_outcome` | state گام |
|---|---|---|---|
| هر نتیجه | موفق، username غایب | `verified_absent` | `removed` |
| هر نتیجه | موفق، username حاضر | — (تلاش دوباره تا سقف)، سپس `unverified` | `cleanup_required` |
| `not_exist` (کد خطای دقیق و شناخته‌شده) | ناموفق | `delete_idempotently_absent` | `removed` |
| `deleted` (بدون خطا) | ناموفق: timeout، خطای اتصال، پاسخ غیر-JSON، HTML، ۴۰۳، JSON بدون `UserList` | `unverified` | `cleanup_required` |
| `ambiguous`: timeout، خطای اتصال، پاسخ غیر-JSON، HTML، ۴۰۳، خطای JSON-RPC با کد ناشناخته | ناموفق | `unverified` | `cleanup_required` |

- «`DeleteUser` بدون exception» به‌تنهایی هیچ‌وقت `delete_idempotently_absent` نمی‌دهد؛ این outcome فقط با پاسخ صریح و شناخته‌شده‌ی «از قبل نیست» است.
- فهرست `not_exist` هر node SoftEther **در ابتدا خالی است** (۵.۱۰). کد و متن دقیق پاسخ SoftEther برای `DeleteUser` روی کاربر ناموجود هنوز روی دستگاه واقعی دیده نشده؛ staging contract (بخش ۱۶، L3) باید `error.code`، `error.message` و بدنه‌ی `result` را عیناً ثبت کند و فقط بعد از آن کد به فهرست اضافه می‌شود. تا آن زمان ردیف سوم جدول عملاً رخ نمی‌دهد و SoftEther فقط از راه `EnumUser` موفق به `removed` می‌رسد.
- `EnumUser` «موفق» یعنی پاسخ JSON-RPC معتبر با `result.UserList` از نوع آرایه. آرایه‌ی خالی معتبر است (هیچ کاربری در hub نیست).
- در `cleanup_required`: reservation `reserved` می‌ماند، finalizer فقط-DB (بخش ۱۲) اجرا نمی‌شود، و ادامه فقط با retry موفق یا force است. force: superadmin + تأیید رمز + دلیل؛ `remote_outcome='abandoned'` و سه ستون `forced_*` نوشته می‌شود (۵.۲، ۱۱.۵).

### ۹.۶ محدودیت صادقانه

رفتار این موارد در کد موجود مشخص نیست و فقط روی دستگاه واقعی معلوم می‌شود:

- پاسخ RouterOS به `add_peer` با public key تکراری؛
- رفتار RouterOS هنگام حذف و افزودن دوباره‌ی آدرس interface (مدت قطعی مسیر peerها) و شکل ردیف‌های `/ip/address` وقتی interface بیش از یک آدرس دارد؛
- پاسخ 3X-UI به `add` با email تکراری؛
- برای هر چهار panel بخش‌های ۹.۷ تا ۹.۱۰: status و بدنه‌ی دقیق پاسخ خواندن، ساخت تکراری، و حذف موجودیت ناموجود؛ و نرمال‌سازی نام سمت panel (حروف کوچک، طول، کاراکتر مجاز)؛
- کد و متن دقیق خطای SoftEther برای `CreateUser` روی کاربر موجود و برای `DeleteUser` روی کاربر ناموجود؛ و این‌که `EnumUser` کاربر تازه‌ساخته و بدون ترافیک را برمی‌گرداند.

طراحی به هیچ‌کدام برای درستی تکیه ندارد (همیشه اول `read`؛ outcome ناشناخته همیشه `unverified`). staging contract هر backend (L3، بخش ۱۶) برای هر مورد بالا درخواست، کد وضعیت، `error.code`، `error.message` و شکل `result` را عیناً ثبت می‌کند؛ بدون secret. نتیجه برای **هر node جدا** در `provisioning_node_contracts` ثبت می‌شود (۵.۱۰) و `state = 'ready'` فقط بعد از probe زنده‌ی همان node. تا آن زمان T1 هر عملیاتی را که slot روی آن node دارد در mode `durable` رد می‌کند (۴۰۹ `node_contract_not_ready`، قبل از هر reserve و هر remote)، و تغییر یک `operation_type` به `durable` وقتی node فعالی بدون ردیف `ready` معتبر هست رد می‌شود. ورود gate به `enforced` به این آمادگی وابسته نیست (خود probe در `enforced` اجرا می‌شود). این شرط **activation** است؛ معماری هر نُه backend در همین سند کامل است.

### ۹.۷ `marzban`

| موضوع | قرارداد |
|---|---|
| هویت | `(username = xr_email, proxies.vless.id = staged_xr_uuid, xr_inbound_tag ∈ inbounds.vless)` |
| متد جدید client | `get_user_typed(username)`: `GET /api/user/{username}` با نتیجه‌ی تایپ‌شده `found(user)` / `not_found` / `unreadable`. `not_found` فقط وقتی status و بدنه با یک عضو `contract.not_exist` برابر است؛ هر ۴۰۴ دیگر (مسیر غلط، proxy، HTML) `unreadable` |
| `read` | `get_user_typed`: `found` و هر سه جزء برابر → `present_match`؛ `found` و uuid یا عضویت inbound متفاوت → `present_conflict`؛ `not_found` → `absent`؛ وگرنه `unreadable`. `_list_users_for_tag` مبنا نیست (فهرست ناقص بی‌صدا) |
| `ensure_present` | `read`؛ `absent` → فقط `POST /api/user`؛ سپس `read` دوباره باید `present_match` بدهد. `present_match` → هیچ. **هیچ‌وقت `PUT`**: fallback «POST شکست خورد پس PUT» (`add_client` امروز) در مسیر durable وجود ندارد |
| `ensure_absent` | `read`؛ `present_match` → `DELETE /api/user/{username}`، سپس `read` دوباره: `absent` → `verified_absent`؛ `absent` از اول → `verified_absent`؛ `present_conflict` → `unverified` بدون حذف؛ `unreadable` بعد از `DELETE`ی که پاسخ `not_exist` قراردادی داد → `delete_idempotently_absent`؛ هر حالت دیگر → `unverified` |
| نکته | `xr_email` این پروژه ۳۴ کاراکتر است (`{۱۲}{۴ hex}@usermanager.local`). این‌که Marzban چنین نامی را می‌پذیرد یا نرمال می‌کند در کد معلوم نیست؛ اگر panel نام را تغییر دهد `read` بعد از `POST` آن را `present_match` نمی‌بیند و گام خطای قابل‌retry و سپس `cleanup_required` می‌گیرد، نه موفقیت کاذب. staging این را ثبت می‌کند |

### ۹.۸ `hiddify`

| موضوع | قرارداد |
|---|---|
| هویت | `(uuid = staged_xr_uuid, name = xr_email)` و «عضویت واقعی»: همان ردیف در فهرست کاربران panel وجود دارد. inbound ندارد |
| متد جدید client | `list_users_typed()`: `GET /api/v2/admin/user/` با نتیجه‌ی `users(list)` / `unreadable`. پاسخ ۲۰۰ با آرایه‌ی JSON → `users`. ۴۰۴ فقط وقتی بدنه با matcher `list_empty` در `contract.not_exist` همان node برابر است → `users([])`. پاسخ غیر-JSON، ۲۰۰ با غیرآرایه، و هر ۴۰۴ دیگر → `unreadable` (برخلاف `_send`/`_list_users` امروز) |
| `read` | روی همان فهرست: ردیف‌های با `uuid` برابر (U) و ردیف‌های با `name` برابر (N). دقیقاً یک ردیف که هم در U و هم در N است و هیچ ردیف دیگری در U یا N نیست → `present_match`. U خالی و N خالی → `absent`. **uuid برابر با name متفاوت → `present_conflict`. name برابر با uuid متفاوت → `present_conflict`** (هر دو جدا گزارش می‌شوند: `conflict_uuid_taken`، `conflict_name_taken`). بیش از یک ردیف در N → `present_conflict` |
| `ensure_present` | `read`؛ `absent` → `POST /api/v2/admin/user/` با uuid و name؛ `read` دوباره باید `present_match`. `present_match` → اگر `enable` خاموش است `PATCH` فقط فیلد `enable` روی همان uuid. **`PATCH` فقط بعد از `read` معتبر با `present_match`**؛ fallback «POST شکست خورد پس PATCH» وجود ندارد |
| `ensure_absent` | `read`؛ `present_match` → `DELETE /api/v2/admin/user/{uuid}/`، `read` دوباره → `verified_absent`. حذف با جست‌وجوی نام (`remove_client` امروز) استفاده نمی‌شود. `present_conflict` → `unverified` بدون حذف |
| نکته | uuid در Hiddify هم کلید اصلی و هم credential است؛ فقط در `staged_xr_uuid` و `Connection.xr_uuid` می‌ماند و در هیچ log یا پیام خطا نمی‌آید (مسیر URL حاوی uuid از پیام‌های خطا حذف می‌شود) |

### ۹.۹ `marzneshin`

| موضوع | قرارداد |
|---|---|
| هویت | `(username = account_username = sanitize_username(xr_email), note = xr_email, key = staged_xr_uuid, xr_panel_inbound_id ∈ service_ids)`؛ هر چهار جزء |
| تولید در T1 | `account_username` در T1 حساب و ذخیره می‌شود. T1 همان لحظه بررسی می‌کند که این نام با هیچ `Connection` موجود همان node (با `sanitize_username` روی `xr_email` آن) و هیچ گام غیرترمینال همان node برابر نیست؛ برخورد → hex تازه برای `xr_email` و محاسبه‌ی دوباره. این فقط برخورد با موجودیت‌های خود پنل را می‌بندد؛ برخورد با کاربری که مستقیم روی Marzneshin ساخته شده را `read` می‌گیرد |
| متد جدید client | `list_users_typed()`: همان صفحه‌بندی `GET /api/users`، ولی هر صفحه‌ی غیر-۲۰۰ کل نتیجه را `unreadable` می‌کند (برخلاف `_iter_users` امروز که بی‌صدا تمام می‌شود)؛ و برابری `total` با تعداد خوانده‌شده سنجیده می‌شود |
| `read` | ردیف با `username` برابر (حداکثر یکی). نیست و هیچ ردیفی `note = xr_email` ندارد → `absent`. هست و `note`، `key` و عضویت service هر سه برابر → `present_match`. **هست ولی `note` متفاوت (collision ناشی از sanitize) → `present_conflict`**؛ هست ولی `key` یا service متفاوت → `present_conflict`. ردیف دیگری با `note = xr_email` ولی `username` متفاوت → `present_conflict` |
| `ensure_present` | `read`؛ `absent` → فقط `POST /api/users`؛ `read` دوباره باید `present_match`. `present_match` → اگر غیرفعال است `POST .../enable`. **هیچ‌وقت `PUT`**؛ user موجود overwrite نمی‌شود |
| `ensure_absent` | `read`؛ `present_match` → `DELETE /api/users/{account_username}`، `read` دوباره → `verified_absent`. `present_conflict` → `unverified` بدون حذف (حذف با نام sanitize‌شده ممکن است کاربر شخص دیگری را حذف کند) |
| نکته | این‌که فهرست کاربران `key` را برمی‌گرداند از `list_clients_with_usage` امروز (`u.get("key")`) برداشت شده و روی panel واقعی تأیید نشده. اگر `key` در پاسخ نباشد تطبیق کامل ممکن نیست و `read` باید `unreadable` بدهد، نه `present_match`؛ staging تعیین می‌کند |

### ۹.۱۰ `sui`

| موضوع | قرارداد |
|---|---|
| هویت | `(name = xr_email, config.vless.uuid = staged_xr_uuid, xr_panel_inbound_id ∈ inbounds)` |
| متد جدید client | `get_client_typed(name)`: `GET /apiv2/clients`؛ ردیف‌های با `name` برابر؛ برای ردیف یکتا `GET /apiv2/clients?id={id}` تا `config` خوانده شود. نتیجه `found(full_client)` / `not_found` / `ambiguous` / `unreadable`. `list_client_emails` و `_client_by_name` مبنا نیستند، چون `config` ندارند |
| `read` | `not_found` → `absent`. `found` و uuid داخل `config.vless` برابر و `inbound_id` عضو `inbounds` → `present_match`. **`found` با همان name ولی uuid متفاوت → `present_conflict`؛ با همان name و uuid ولی inbound متفاوت → `present_conflict`.** بیش از یک ردیف با همان name (`ambiguous`) → `present_conflict`. `success: false`، status غیر-۲۰۰ یا JSON نامعتبر → `unreadable` |
| `ensure_present` | `read`؛ `absent` → `save` با `action=new`؛ `read` دوباره باید `present_match`. `present_match` → اگر `enable` خاموش است `edit` با **رکورد کامل همان `read`** و فقط `enable` عوض‌شده. `edit` روی موجودیت conflict هرگز |
| `ensure_absent` | `read`؛ `present_match` → `save` با `action=del` و **همان `id` حاصل از این `present_match`**، در همان نگه‌داشت gate؛ `read` دوباره → `verified_absent`. `present_conflict` → `unverified` بدون حذف |
| نکته | `id` panel هرگز ذخیره نمی‌شود و بین دو نگه‌داشت gate دوباره خوانده می‌شود. فهرست `GET /apiv2/clients` `config` ندارد، پس «همین uuid زیر نام دیگر» دیده نمی‌شود؛ این برخورد را panel (یا نبود آن) تعیین می‌کند و در staging ثبت می‌شود |

### ۹.۱۱ قواعد مشترک چهار panel

- `add_client`، `remove_client` و `set_client_enabled` موجود این چهار client در مسیر durable صدا زده نمی‌شوند (هر سه overwrite کور یا تحمل کور ۴۰۴ دارند). برای مسیر legacy بدون تغییر می‌مانند و فقط از gate می‌گذرند.
- `ensure_present` روی `present_conflict` هیچ `POST`/`PUT`/`PATCH`/`edit`/`DELETE` نمی‌فرستد؛ گام `cleanup_required` با `remote_identity_conflict`.
- `ensure_absent` همیشه بعد از حذف دوباره `read` می‌کند. ۴۰۴ در حذف یا خواندن فقط وقتی «نیست» حساب می‌شود که با عضو `contract.not_exist` همان node برابر باشد؛ این فهرست برای هر node در ابتدا خالی است و فقط با probe زنده‌ی همان node (۵.۱۰) پر می‌شود.
- هر پاسخ ناشناخته (status دیگر، بدنه‌ی غیر-JSON، ساختار غیرمنتظره، timeout) → `unreadable` برای `read` و `unverified` برای `ensure_absent`؛ گام `cleanup_required` و reservation `reserved`.
- uuid/key فقط در `staged_xr_uuid`؛ در هیچ `error_sanitized`، log یا پاسخ.
- هر چهار adapter داخل gate node و با guard زمان اجرا (۱۰.۷) اجرا می‌شوند.

---

## ۱۰. قفل‌ها و fencing

دو سازوکار جدا، با دو تضمین جدا:

| سازوکار | چه چیزی را تضمین می‌کند | چه چیزی را تضمین **نمی‌کند** |
|---|---|---|
| lease در `resource_locks` + revalidation داخل تراکنش | نوشتن **DB** توسط executor کهنه commit نمی‌شود | هیچ‌چیز درباره‌ی remote: رد شدن commit محلی، اثر remoteی را که executor کهنه قبلاً فرستاده برنمی‌گرداند |
| gate مالکیت node (۱۰.۲) | در هر لحظه حداکثر یک mutation remote روی یک node، بین همه‌ی processهای همان host؛ آزادسازی فقط با پایان کار یا مرگ process | — |

**lease منقضی‌شونده به‌تنهایی remote fencing نیست**، و درستی این طراحی به `TTL > timeout` وابسته نیست. دو executor که یک lease node را پشت‌سرهم می‌گیرند لزوماً روی یک گام یا یک هویت کار نمی‌کنند؛ ممکن است دو عملیات متفاوت باشند که هر دو یک منبع مشترک (آدرس interface، فایل config) را می‌نویسند. به همین دلیل serialization remote فقط با gate است.

### ۱۰.۱ lease و fencing برای نوشتن‌های DB

از همان جدول `resource_locks` سند Receipt Void استفاده می‌شود (`resource_key` PK، `lease_owner`، `fencing_epoch`، `leased_until`، `heartbeat_at`، `version`)، با همان قرارداد:

```
acquire:    UPDATE resource_locks SET lease_owner=:o, fencing_epoch=fencing_epoch+1, leased_until=DB_NOW+:ttl, heartbeat_at=DB_NOW
            WHERE resource_key=:k AND (lease_owner IS NULL OR leased_until < DB_NOW)
renew:      ... WHERE resource_key=:k AND lease_owner=:o AND fencing_epoch=:e AND leased_until >= DB_NOW
release:    ... SET lease_owner=NULL WHERE resource_key=:k AND lease_owner=:o AND fencing_epoch=:e
revalidate (اولین کار هر تراکنش business، بعد از locking read حالت‌های runtime):
            SELECT lease_owner, fencing_epoch, (leased_until >= DB_NOW) AS live FROM resource_locks
            WHERE resource_key IN (:keys) ORDER BY resource_key FOR UPDATE        -- SQLite: زیر BEGIN IMMEDIATE
            هر کلید با owner یا epoch متفاوت، یا live = 0 → ROLLBACK
```

| کلید | چه نوشتن DB را سریال می‌کند |
|---|---|
| `customer_identity:{id}` | reserve/capture/release کیف‌پول در generation `wallet_lot` (قرارداد Receipt Void) |
| `payment_card_pool:{pool_key}` | نوشتن شمارنده و pointer کارت در T_final (قرارداد Receipt Void ۱۵.۲) |
| `node:{node_id}:wg_pool` | تخصیص IP و بزرگ‌کردن `mt_client_subnet` در T1 (فقط DB) |
| `provisioning_op:{operation_id}` | فقط یک executor (درخواست sync یا worker) ردیف‌های یک عملیات را جلو می‌برد؛ همچنین backoff و audit |

برای فایل config Xray هیچ lease وجود ندارد؛ serialization آن فقط با gate است.

### ۱۰.۲ gate مالکیت node (serialization remote)

یک gate به‌ازای هر node، نه به‌ازای هر منبع: همه‌ی mutationهای remote یک node، از هر backend، پشت‌سرهم اجرا می‌شوند.

| لایه | سازوکار | آزادسازی |
|---|---|---|
| همیشه (SQLite و MariaDB) | `flock(LOCK_EX)` روی فایل `{lock_dir}/{installation_uuid}/node-{node_id}.lock`، با یک file descriptor تازه برای هر نگه‌داشت. `lock_dir` روی فایل‌سیستم محلی است | بستن fd؛ یا مرگ process (kernel) |
| فقط MariaDB/MySQL، علاوه بر بالا | `SELECT GET_LOCK(:name, :wait)` روی یک **اتصال اختصاصی** که از pool Sessionها جداست و تا پایان نگه‌داشت باز می‌ماند | `RELEASE_LOCK`؛ یا قطع اتصال (سرور) |

**نام قفل و namespace نصب.** `GET_LOCK` در سطح کل سرور MariaDB است، نه یک database؛ پس نام باید نصب را هم داشته باشد:

```
name = "um1:" + lowercase_hex(SHA-256("um-gate|" + installation_uuid + "|" + decimal(node_id)))[0:56]
```

طول همیشه ۶۰ کاراکتر است (سقف نام `GET_LOCK` ۶۴ است) و ۲۲۴ بیت hash دارد. مسیر `flock` هم `installation_uuid` را به‌صورت شاخه دارد. دو نصب مستقل روی یک سرور MariaDB یا یک host با `node_id` یکسان یکدیگر را block نمی‌کنند؛ دو process همان نصب و همان node همیشه روی یک نام و یک فایل می‌نشینند.

`installation_uuid` (۵.۸) در L0 یک‌بار ساخته می‌شود و **هویت نصب** است، نه هویت host؛ lifecycle کامل آن (restore، fork، rotate، mismatch) در ۱۰.۱۰ است.

**خاصیت‌ها:**

- **منقضی نمی‌شود.** هیچ TTL ندارد. مالک فقط با پایان کار یا مرگ process (و برای لایه‌ی دوم قطع اتصال) عوض می‌شود.
- **بین thread و process.** قفل `flock` به open file description وابسته است؛ چون هر نگه‌داشت fd خودش را باز می‌کند، دو thread یک process و دو process مستقل هر دو سریال می‌شوند.
- **انتظار محدود.** گرفتن gate با مهلت است (حلقه‌ی `LOCK_NB`؛ و `:wait` برای `GET_LOCK`). تمام‌شدن مهلت خطای قابل‌retry است (`node_gate_timeout`)؛ هیچ مسیری gate را به‌زور نمی‌گیرد.
- **mode.** رفتار واقعی gate به `gate_mode` بستگی دارد (۱۰.۶)؛ این بخش حالت `enforced` را توصیف می‌کند.
- **هر mutation remote از gate می‌گذرد**، نه فقط مسیر durable:

| محل امروز | mutation |
|---|---|
| adapterهای بخش ۹ (`ensure_present`، `ensure_absent`) | همه |
| `services/user_ops.py:974-984`، `:1144`، `:1192` | provisioning legacy (WireGuard، هر شش client Xray، SoftEther) |
| `services/user_ops.py:1661-1693` | `deprovision_connection` legacy |
| `services/user_ops.py:1766-1793` | فعال/غیرفعال‌کردن اتصال |
| `services/quota_manager.py:632-663` | enforcement poller (برای Xray SSH همان `add_client`/`remove_client` و بازنویسی config) |
| `routers/users.py:1227-1254` | `update_connection` (rename peer، queue) |
| `routers/nodes.py:476` | ساخت دوباره‌ی clientهای موجود روی node Xray |
| reconciliation (۱۰.۵) | آدرس interface |

  این فهرست inventory امروز است، نه تضمین؛ تضمین guard زمان اجرا است (۱۰.۷).
- **بدون تراکنش نوشتن باز.** مسیر durable هنگام گرفتن و نگه‌داشتن gate هیچ تراکنش DB بازی ندارد؛ تراکنش‌های داخل نگه‌داشت کوتاه و فقط-خواندنی‌اند، و tx ب (۷.۲) بعد از آزادکردن gate اجرا می‌شود. مسیرهای legacy که امروز با Session باز remote را صدا می‌زنند gate را با مهلت کوتاه‌تر از `busy_timeout` SQLite (۱۵ ثانیه، `database.py:37`) می‌خواهند؛ پس انتظار متقابل gate و قفل نوشتن SQLite همیشه با خطای قابل‌retry یک طرف تمام می‌شود، نه بن‌بست.
- **اتصال اختصاصی MariaDB.** اگر اتصال نگه‌دارنده‌ی `GET_LOCK` قطع شود، لایه‌ی دوم آزاد می‌شود در حالی که process هنوز کار می‌کند. نگه‌دارنده قبل از هر فراخوانی نویسنده `SELECT IS_USED_LOCK(:name) = CONNECTION_ID()` را روی همان اتصال می‌سنجد و اگر برقرار نیست کار را رها می‌کند. در topology پشتیبانی‌شده (یک host) `flock` همچنان برقرار است، پس قطع اتصال DB serialization را نمی‌شکند.

### ۱۰.۳ ترتیب acquire

```
۱. lease customer_identity:*          (صعودی)
۲. lease payment_card_pool:*          (صعودی)
۳. lease node:*:wg_pool               (صعودی)
۴. lease provisioning_op:*
۵. gate node:{id}                     -- فقط وقتی هیچ تراکنش DB باز نیست ؛ در هر لحظه حداکثر یک gate
   ۵.۱ flock مشترک (LOCK_SH) روی فایل gate-mode  (۱۰.۶)
   ۵.۲ بررسی مالکیت host                          (۱۰.۴)
   ۵.۳ flock انحصاری فایل node
   ۵.۴ GET_LOCK (فقط MariaDB)
   ۵.۵ بررسی دوباره‌ی مالکیت host ؛ ناموفق → آزادسازی همه و خطای ownership_lock_host_mismatch
   آزادسازی به ترتیب عکس
```

- یک گام فقط یک node دارد و هیچ executor دو gate را هم‌زمان نگه نمی‌دارد؛ عملیات چند-node gateها را پشت‌سرهم می‌گیرد و آزاد می‌کند. پس بین gateها چرخه‌ی انتظار وجود ندارد.
- هیچ lease بعد از گرفتن gate خواسته نمی‌شود. leaseهای ۱ تا ۳ فقط دور تراکنش‌های DB (T1، T_final، T_comp) نگه داشته می‌شوند و هنگام کار remote آزادند؛ فقط lease ۴ در مدت gate نگه داشته و renew می‌شود. اگر lease ۴ وسط کار remote منقضی شود و executor دیگری آن را بگیرد، آن executor پشت همان gate منتظر می‌ماند و بعد از گرفتنش با `read` شروع می‌کند؛ نوشتن DB executor قدیمی در revalidation رد می‌شود. اثر remote executor قدیمی کامل و سریال‌شده است، نه هم‌زمان.

### ۱۰.۴ مالکیت host

تضمین کامل gate برای **یک host** است (یک یا چند process روی همان host، با SQLite یا MariaDB)، چون `flock` بین hostها مشترک نیست. این همان topology استقرار فعلی است (یک process uvicorn در `backend/Dockerfile:127`؛ بات remote فقط HTTP می‌زند و هیچ node را mutate نمی‌کند). پس در هر لحظه دقیقاً یک `(host, boot)` مالک است و مالک فقط با یک اقدام صریح عوض می‌شود.

**هویت host.** از دو منبع، هر دو بیرون از دیتابیس و بیرون از شاخه‌ی داده و backup پروژه:

```
host_secret  = محتوای فایل UM_HOST_ID_FILE (پیش‌فرض /etc/usermanager/host.id) ؛ ۳۲ بایت تصادفی ؛ mode 0600
               installer آن را روی خود host می‌سازد و فقط-خواندنی به container mount می‌کند ؛ در شاخه‌ی /app/data نیست
host_id      = lowercase_hex(SHA-256("um-host|" + host_secret))            -- CHAR(64) ؛ خود secret هرگز ذخیره یا log نمی‌شود
boot_id      = محتوای /proc/sys/kernel/random/boot_id                     -- kernel ؛ با هر boot عوض می‌شود
```

- همه‌ی processها و containerهای یک host همان فایل و همان kernel را می‌بینند، پس همان `(host_id, boot_id)` را دارند. restart container یا process `boot_id` را عوض نمی‌کند.
- فایل نبود، خالی، کوتاه‌تر از ۳۲ بایت، یا با mode بازتر از `0600` → process هویت ندارد و fail-closed است.
- backup پروژه (snapshot دیتابیس) این فایل را ندارد؛ restore روی host دیگر `host_id` متفاوت می‌بیند و fail-closed می‌شود.
- **reboot واقعی و clone از روی این داده‌ها قابل تمایز نیستند.** هر دو `host_id` یکسان و `boot_id` متفاوت دارند. این سند هیچ تشخیص خودکاری بین این دو ادعا نمی‌کند و هیچ‌کدام را خودکار نمی‌پذیرد.

**قاعده‌ی claim عادی** (`POST /api/provisioning/lock-backend/verify`، superadmin): خودآزمایی، سپس CAS. claim فقط در دو حالت پذیرفته می‌شود:

```
۱. هویت host معتبر
۲. lock_dir وجود دارد، قابل نوشتن است و روی فایل‌سیستم محلی است (نوع فایل‌سیستم از فهرست رد: nfs، cifs/smb، fuse، 9p)
۳. خودآزمایی flock: fd اول LOCK_EX ؛ fd دوم با LOCK_NB باید رد شود ؛ بعد از بستن fd اول باید بگیرد
۴. خودآزمایی مرگ process: process فرزند قفل می‌گیرد و kill می‌شود ؛ والد باید بتواند بگیرد
۵. MariaDB: همان دو خودآزمایی با GET_LOCK روی دو اتصال ، و آزادشدن با بستن اتصال
۶. claim (یک تراکنش ، CAS):
     UPDATE provisioning_runtime_state
        SET owner_state='active', owner_host_id=:me, owner_boot_id=:boot,
            owner_claimed_at = CASE WHEN owner_host_id IS NULL THEN DB_NOW ELSE owner_claimed_at END,
            owner_heartbeat_at=DB_NOW,
            ownership_epoch = ownership_epoch + (CASE WHEN owner_host_id IS NULL THEN 1 ELSE 0 END),
            lock_backend=:b, lock_verified_at=DB_NOW, version=version+1
      WHERE id=1 AND version=:v
        AND (   owner_state IN ('none','released')                                          -- (الف) بدون مالک
             OR (owner_state='active' AND owner_host_id = :me AND owner_boot_id = :boot))   -- (ب) دقیقاً همین host و همین boot
     rowcount = 0 → 409 ownership_held ؛ هیچ ستونی عوض نمی‌شود
     INSERT provisioning_ownership_events('claim' برای (الف) ، 'verify' برای (ب))
```

| وضعیت ردیف | verify عادی از این process |
|---|---|
| بدون مالک (`none`/`released`) | claim؛ `ownership_epoch + 1` |
| مالک = همین `host_id` و همین `boot_id` | پذیرفته؛ فقط heartbeat و `lock_verified_at`؛ epoch ثابت |
| مالک = همین `host_id`، **`boot_id` متفاوت** | **رد**، با هر سن heartbeat. فقط reclaim صریح (پایین) |
| مالک = `host_id` دیگر | **رد**، با هر سن heartbeat. فقط handoff یا forced takeover |
| `owner_state = 'draining'` | رد |

- heartbeat (`owner_heartbeat_at`، هر ۶۰ ثانیه با `WHERE owner_host_id = :me AND owner_boot_id = :boot`) فقط برای نمایش و هشدار است. **هیچ مسیر خودکاری مالکیت را به‌خاطر کهنه‌شدن heartbeat منتقل نمی‌کند**: مالکی که freeze یا partition شده ممکن است یک فراخوانی remote در راه داشته باشد که بعد از برگشتن کامل می‌شود، و هیچ guardی آن را پس نمی‌گیرد.
- verify هم‌زمان دو process یا دو host روی ردیف بدون مالک: هر دو همان `version` را می‌خوانند؛ CAS فقط یکی را می‌پذیرد.
- process در startup ردیف را می‌خواند: اگر مالک دقیقاً `(host_id, boot_id)` خودش است، `ownership_epoch` را کش می‌کند و بدون هیچ claimی کار می‌کند (restart process و container). وگرنه fail-closed می‌ماند تا verify، reclaim یا takeover.

**shutdown تمیز.** هنگام خاموش‌شدن عادی، process اصلی شروع runner تازه را متوقف می‌کند، پایان یا kill همه‌ی runnerهای زنده را با `waitpid` تأیید می‌کند، و سپس با CAS روی `(host_id, boot_id)` خودش `owner_state → 'released'` و `ownership_epoch + 1` می‌نویسد (event `shutdown_release`، actor سیستم). بعد از آن boot تازه‌ی همان host (یا هر host دیگر) با verify عادی claim می‌کند. **درستی به اجرای این hook وابسته نیست**: اگر اجرا نشود (crash، قطع برق، `SIGKILL`)، ردیف مالک قبلی می‌ماند و فقط reclaim صریح آن را باز می‌کند.

**reclaim صریح** (همان `host_id`، `boot_id` تازه، بدون release تمیز): `POST /api/provisioning/ownership/reclaim` روی همان host.

- فقط superadmin، با تأیید رمز، دلیل متنی اجباری، و عبارت تأیید ثابت «process قبلی این نصب دیگر در حال اجرا نیست».
- شرط: `owner_host_id` برابر `host_id` این process و `owner_boot_id` متفاوت؛ خودآزمایی‌های verify موفق. در غیر این صورت رد (برای `host_id` دیگر مسیر forced takeover است).
- یک تراکنش، CAS روی `version`: `owner_boot_id` تازه، `owner_state='active'`، **`ownership_epoch + 1`** (fencing epoch تازه)، heartbeat؛ ردیف `reclaim` دائمی با actor، دلیل، boot قبلی و boot جدید.
- این مسیر clone را رد نمی‌کند، چون clone و reboot هم‌شکل‌اند؛ تأیید انسانی تنها تمایز است. ولی clone هم نمی‌تواند **بدون** این تأیید صریح مالک شود.

**بررسی مالکیت (revalidation)** در هر gate acquisition (قدم‌های ۵.۲ و ۵.۵ بخش ۱۰.۳) و بلافاصله قبل از هر فراخوانی نویسنده (۱۰.۷)، با یک خواندن تازه‌ی ردیف singleton:

```
owner_state = 'active' و owner_host_id = host_id این process و owner_boot_id = boot_id این process و ownership_epoch = epoch داخل RemoteActionDTO
```

هرکدام برقرار نبود → هیچ gate `enforced` گرفته نمی‌شود و هیچ نویسنده‌ای فرستاده نمی‌شود (`ownership_lock_host_mismatch`). process قدیمی بعد از reclaim یا takeover در اولین revalidation متوقف می‌شود. فراخوانی‌ای که همان لحظه در راه است متوقف نمی‌شود؛ به همین دلیل هر تغییر مالکیت غیر از handoff تأیید انسانی می‌خواهد.

**handoff (انتقال برنامه‌ریزی‌شده از A به B).** همه روی host مالک فعلی، superadmin:

```
۱. POST /api/provisioning/ownership/drain      CAS: owner_state 'active' → 'draining' ؛ event drain_start
     از این لحظه: T1 هر عملیات تازه → 503 provisioning_draining ؛ هر gate acquisition تازه رد ؛
     worker عملیات در جریان را تا ترمینال ادامه می‌دهد ولی عملیات تازه برنمی‌دارد ؛ poller و reconciliation نویسنده متوقف
۲. انتظار تا: هیچ عملیات غیرترمینال (جز cleanup_required که باید قبلش بسته یا force شود) ؛
              و هیچ نگه‌دارنده‌ی gate: برای هر node ، LOCK_NB روی فایل gate باید بگیرد ؛ و flock انحصاری gate-mode باید بگیرد
۳. POST /api/provisioning/ownership/release    فقط اگر قدم ۲ برقرار است ، وگرنه 409 با فهرست عملیات و nodeهای مشغول
     CAS: owner_state 'draining' → 'released' ، owner_* = NULL ، ownership_epoch + 1 ؛ event release
۴. روی host B: verify → claim (حالت «بدون مالک»)
POST /api/provisioning/ownership/drain-cancel  در قدم ۱ یا ۲: 'draining' → 'active' ؛ event drain_cancel
```

در مدت drain هیچ mutation remoteی شروع نمی‌شود، از جمله مسیرهای legacy (چون gate `enforced` است)؛ این یک توقف کوتاه و اعلام‌شده‌ی provisioning است.

**forced takeover** (`host_id` دیگر؛ host قبلی از دست رفته و release ممکن نیست): `POST /api/provisioning/ownership/forced-takeover` روی host جدید.

- فقط superadmin، با تأیید رمز، دلیل متنی، و عبارت تأیید ثابت «host قبلی خاموش است و دوباره روشن نمی‌شود».
- رد می‌شود اگر: `owner_host_id` همین host است (مسیر آن verify یا reclaim است)؛ هویت host نامعتبر است؛ یا خودآزمایی‌های verify ناموفق‌اند. heartbeat تازه‌ی مالک قبلی یک هشدار اضافه در پاسخ preview است و تأیید دوم می‌خواهد؛ نبودنش دلیل امن‌بودن نیست.
- یک تراکنش: CAS روی `version`؛ مالک = این host و boot؛ `ownership_epoch + 1`؛ ردیف `forced_takeover` دائمی با host قبلی، host جدید، actor و دلیل.
- با SQLite، host قدیمی دیتابیس جدا دارد و اصلاً خبردار نمی‌شود؛ آن‌جا تأیید انسانی تنها دفاع است.

| وضعیت | رفتار |
|---|---|
| `lock_backend = 'none'`، هویت host نامعتبر، یا خودآزمایی ناموفق | `gate_mode` از `off` بیرون نمی‌رود؛ هیچ نوعی `durable` نمی‌شود (۴۰۹ `ownership_lock_not_ready`) |
| process با `host_id`، `boot_id` یا `epoch` ناهم‌خوان | هیچ gate `enforced`؛ هیچ mutation remote؛ worker آن ثبت نمی‌شود |
| MariaDB با بیش از یک host backend فعال | پشتیبانی نمی‌شود؛ فقط مالک می‌نویسد، بقیه ۵۰۳ |

### ۱۰.۵ reconciliation آدرس WireGuard

job جدا در `_start_full_services`: یک‌بار در startup و سپس دوره‌ای (پیش‌فرض هر ۱۰ دقیقه). فقط وقتی حداقل یک نوع `durable` است.

```
برای هر node میکروتیک فعال با WireGuard:
  with node_gate(node.id):
      ensure_interface_address_exact_or_wider(mt, node)        -- همان helper بخش ۹.۱ ؛ بدون بررسی گام
  upsert provisioning_node_reconciliations(node_id, last_run_at, result, db_subnet, router_address, consecutive_failures)
```

| یافته | کار | `result` |
|---|---|---|
| router برابر DB | هیچ | `in_sync` |
| router باریک‌تر از DB، یا بدون آدرس | repair به مقدار DB | `repaired` |
| router پهن‌تر از DB (ویرایش دستی) | بدون تغییر | `router_wider` |
| شبکه‌ی بی‌ربط یا چند آدرس | بدون تغییر | `conflict` |
| node در دسترس نیست | بدون تغییر؛ `consecutive_failures + 1` | `unreachable` |

`router_wider`، `conflict` و `unreachable` مکرر در داشبورد superadmin دیده می‌شوند. این job drift تاریخی، تغییر دستی روی router و خرابی نیمه‌کاره‌ی جایگزینی آدرس را بدون نیاز به عملیات بعدی پیدا و repair می‌کند. **جای gate را نمی‌گیرد**: درستی هم‌زمانی با gate است و reconciliation فقط چیزهایی را می‌گیرد که بیرون از این سیستم عوض شده‌اند.

### ۱۰.۶ mode gate: `off` / `shadow` / `enforced`

deploy کد gate نباید رفتار هیچ مسیر زنده‌ای را عوض کند. `provisioning_runtime_state.gate_mode` رفتار را تعیین می‌کند.

**قفل‌ها و رفتار در هر mode:**

| | `off` (پیش‌فرض deploy) | `shadow` | `enforced` |
|---|---|---|---|
| قفل مشترک gate-mode (`LOCK_SH` روی `{lock_dir}/{installation_uuid}/gate-mode.lock`) از ابتدای مسیر remote تا پایان آن | **بله** | **بله** | **بله** (در runner) |
| قفل انحصاری node (`flock`) | نه | فقط `LOCK_NB`: گرفت → تا پایان نگه می‌دارد؛ نگرفت → بدون انتظار ادامه | بله، با انتظار محدود |
| `GET_LOCK` (MariaDB) | نه | نه | بله |
| runner فرزند و `RemoteActionDTO` | نه؛ inline مثل امروز | نه؛ inline مثل امروز | بله |
| بررسی مالکیت host | نه | نه | هر acquisition و هر نویسنده |
| context ساخته‌شده | `ShadowGateContext(mode='off')` | `ShadowGateContext(mode='shadow', node_lock_held)` | `GateToken` |
| guard روی نویسنده‌ی **بدون هیچ context** | بی‌اثر | `writer_not_instrumented_count + 1`؛ فراخوانی انجام می‌شود | رد قبل از هر بایت شبکه |
| guard روی نویسنده با `ShadowGateContext` | بی‌اثر | مجاز؛ اگر قفل node را نگرفته بود قبلاً `contention_count + 1` شده | **رد** (فقط `GateToken`) |
| انتظار یا خطای قابل‌مشاهده برای درخواست legacy | هیچ | هیچ | انتظار محدود؛ `node_gate_timeout` |
| تضمین serialization روی node | **هیچ** | **هیچ**؛ فقط اندازه‌گیری | بله |

- **قفل مشترک در `off`.** هر نویسنده، در هر سه mode، اول `LOCK_SH` فایل gate-mode را می‌گیرد، `gate_mode` و `gate_mode_epoch` را می‌خواند و قفل را تا پایان کار remote خودش نگه می‌دارد. قفل‌های مشترک یکدیگر را block نمی‌کنند، پس در اجرای عادی هیچ انتظاری دیده نمی‌شود؛ تنها لحظه‌ای که منتظر می‌مانند مدت کوتاه نگه‌داشت انحصاری یک تغییر mode است. در `off` نه قفل node گرفته می‌شود، نه runner استفاده می‌شود، نه serialization node عوض می‌شود.
- **`shadow` تضمین درستی نیست** و هیچ‌جا به‌عنوان تضمین حساب نمی‌شود؛ فقط نشان می‌دهد `enforced` چه‌قدر انتظار و چه نویسنده‌ی instrument‌نشده‌ای خواهد داشت. درخواست legacy در `shadow` هرگز به‌علت gate رد یا منتظر نمی‌شود.
- **contention با instrumentation ناقص یکی نیست.** نویسنده‌ای که از `node_gate` گذشته ولی در `shadow` قفل node را آزاد ندیده `ShadowGateContext` دارد: instrument شده، و فقط `contention_count` را زیاد می‌کند. نویسنده‌ای که اصلاً از `node_gate` نگذشته هیچ contextی ندارد و `writer_not_instrumented_count` را زیاد می‌کند. فقط دومی ورود به `enforced` را می‌بندد.
- **اگر قفل مشترک در `off` گرفته نشود** (شاخه‌ی قفل نیست یا قابل نوشتن نیست): نویسنده برای حفظ «صفر تغییر رفتار» ادامه می‌دهد و `mode_lock_miss_count + 1` (ردیف `node_id = 0`) ثبت می‌شود. در `shadow` و `enforced` همین وضعیت خطاست. شاخه‌ی قفل داخل همان شاخه‌ی داده‌ای است که برنامه از قبل برای کار به نوشتن در آن نیاز دارد (`config.py:18`، فایل `.secret_key`)؛ پس این حالت در نصب سالم رخ نمی‌دهد، ولی اگر رخ دهد rollout را می‌بندد (پایین).

**تغییر mode** endpoint جداست: `POST /api/provisioning/gate-mode` (superadmin + تأیید رمز). verify موفق gate را `shadow` یا `enforced` نمی‌کند.

```
change_gate_mode(target):
  ۱. پیش‌شرط‌های جدول پایین (خواندن)
  ۲. LOCK_EX روی فایل gate-mode ، با مهلت activation_timeout (پیش‌فرض ۶۰ ثانیه ؛ حلقه‌ی LOCK_NB)
       → منتظر خروج همه‌ی نویسنده‌هایی می‌ماند که در mode قبلی شروع شده‌اند و LOCK_SH دارند ، در همه‌ی processها
       مهلت تمام شد → 409 gate_mode_change_timeout ؛ هیچ ستونی عوض نمی‌شود ؛ نویسنده‌های مشغول (pid و سن) در پاسخ
  ۳. در همان نگه‌داشت انحصاری: پیش‌شرط‌ها دوباره ؛ سپس یک تراکنش:
       UPDATE provisioning_runtime_state SET gate_mode=:target, gate_mode_epoch=gate_mode_epoch+1, gate_mode_changed_at=DB_NOW, version=version+1
        WHERE id=1 AND version=:v
       INSERT provisioning_ownership_events('gate_mode_change', gate_mode_from, gate_mode_to)
  ۴. آزادکردن LOCK_EX
```

هیچ نویسنده‌ای در مدت قدم‌های ۲ تا ۴ در حال اجرا نیست، و هر نویسنده‌ی بعدی mode تازه را بعد از گرفتن `LOCK_SH` می‌خواند. پس اولین نویسنده‌ی mode جدید با آخرین نویسنده‌ی mode قبلی هم‌پوشانی ندارد. اگر نویسنده‌ی قدیمی hang کرده باشد، تغییر mode با timeout **رد** می‌شود و هرگز بر اساس گذشت زمان موفق اعلام نمی‌شود؛ آن نویسنده باید تمام شود یا process آن بسته شود (که `LOCK_SH` را آزاد می‌کند).

| انتقال | پیش‌شرط (علاوه بر قفل انحصاری) |
|---|---|
| `off → shadow` | `lock_backend <> 'none'`؛ مالکیت host `active` روی همین `(host, boot)`؛ `mode_lock_miss_count = 0` |
| `shadow → enforced` | `writer_not_instrumented_count = 0` و `mode_lock_miss_count = 0` (روی همه‌ی ردیف‌ها)؛ خودآزمایی runner موفق. `contention_count` شرط نیست. آمادگی قرارداد nodeها شرط نیست (۵.۱۰) |
| `enforced → shadow` یا `off`، و `shadow → off` | همه‌ی `operation_type`ها `legacy`؛ هیچ عملیات غیرترمینال؛ هیچ runner زنده؛ هیچ نگه‌دارنده‌ی قفل node (probe `LOCK_NB`) |
| هر `operation_type` از `legacy` به `durable` | `gate_mode = 'enforced'` (۴۰۹ `gate_not_enforced`)؛ و برای هر node با `enabled = True` ردیف `provisioning_node_contracts` در `ready` با نسخه و fingerprint فعلی (۴۰۹ `node_contract_not_ready` با فهرست nodeها) |

- مدت ماندن در `shadow` شرط درستی نیست. کمتر از `2 × hard_deadline` در `shadow` فقط یک **هشدار عملیاتی** در پاسخ preview تغییر mode است (نمونه‌ی اندازه‌گیری کم است).
- اگر `mode_lock_miss_count > 0` است: علت رفع، همه‌ی processهای backend restart (که هر نویسنده‌ی بدون قفل را هم تمام می‌کند)، و سپس شمارنده با `gate-stats/reset` صفر می‌شود. این توالی عملیاتی است و سند تشخیص خودکار «همه restart شده‌اند» را ادعا نمی‌کند.
- `gate_mode`، `gate_mode_epoch`، مالک، `ownership_epoch`، نگه‌دارنده‌های فعلی هر node با سن، و شمارنده‌های `provisioning_gate_stats` در `GET /api/provisioning/status` و صفحه‌ی وضعیت superadmin دیده می‌شوند.

### ۱۰.۷ guard زمان اجرا

تست AST نام‌محور نویسنده‌ی تازه یا dispatch پویا را نمی‌بیند. دفاع اصلی در زمان اجراست، در پایین‌ترین لایه‌ای که mutation از آن می‌گذرد.

**دو نوع context.** `node_gate(node_id)` در `enforced` بعد از گرفتن همه‌ی قفل‌ها یک `GateToken` می‌سازد؛ در `off` و `shadow` یک `ShadowGateContext(node_id, mode, node_lock_held)` می‌سازد که هیچ ادعای انحصار ندارد و فقط ثابت می‌کند مسیر instrument شده است. هر دو در یک `ContextVar` گذاشته می‌شوند. `GateToken` فقط وقتی صادر می‌شود که قفل انحصاری node واقعاً گرفته شده و mode `enforced` است:

```
GateToken: node_id ، ownership_epoch ، installation_uuid ، pid ، nonce تصادفی ، شیء fd قفل
  فقط داخل node_gate ساخته می‌شود ؛ در یک registry خصوصی همان ماژول با nonce ثبت و در خروج حذف می‌شود
  معتبر = در registry هست و pid = pid فعلی و fd هنوز باز است
```

token در پایتون مطلقاً غیرقابل‌جعل نیست؛ «غیرقابل‌جعل» یعنی: کلاس و registry خصوصی‌اند، guard اعتبار را از registry می‌پرسد نه از خود شیء، و تست AST ساخت `GateToken` یا نوشتن روی آن `ContextVar` بیرون از `node_gate` را رد می‌کند. تهدید این‌جا باگ است، نه کد مخرب داخل process.

**binding client.** هر client remote در لحظه‌ی ساخت `bound_node_id` می‌گیرد (`client_for_node(node)`، `MikrotikClient.for_node(node)`، سازنده‌ی SoftEther). clientی که بدون node ساخته شده (مثلاً تست اتصال فرم node ذخیره‌نشده) `bound_node_id = None` دارد و هیچ نویسنده‌ای از آن عبور نمی‌کند.

**نقاط guard** (default-deny: هر چیزی که صریحاً «خواندن» اعلام نشده نویسنده است):

| transport | محل guard | خواندن‌های مجاز بدون token |
|---|---|---|
| HTTP panelها (3X-UI، Marzban، Hiddify، Marzneshin، S-UI) | یک `requests.Session` مشترک guard‌دار که هر پنج client به‌جای Session خام می‌سازند؛ در `request()` | همه‌ی `GET`ها؛ و allowlist صریح `(method, path)` برای `POST`های غیرنویسنده: ورود (`/api/admin/token`، `/api/admins/token`، ورود 3X-UI) و خواندن‌های `POST` (مثل onlines 3X-UI) |
| SoftEther JSON-RPC (همه `POST`) | `_call` | allowlist نام متد: `GetHubStatus`، `EnumUser`، `GetUser`، `EnumSession` |
| MikroTik | wrapper روی `path.add`/`path.remove`/`path.update` و هر فراخوانی command | `select` و پیمایش |
| Xray SSH | wrapper روی `exec_command` و نوشتن فایل | allowlist فرمان‌های خواندن (خواندن config، آمار، وضعیت سرویس) |

**قاعده‌ی guard** در `enforced`، قبل از هر بایت شبکه:

```
token فعلی ContextVar وجود دارد و معتبر است
token.node_id = client.bound_node_id                      وگرنه gate_token_node_mismatch
token.pid = pid فعلی و token در runner ساخته شده (۱۰.۸)
بررسی مالکیت host با خواندن تازه (۱۰.۴) ؛ ownership_epoch برابر token
context فعلی ShadowGateContext است → رد (در enforced پذیرفته نمی‌شود)
هرکدام برقرار نبود → GateViolation ؛ هیچ اتصال و هیچ درخواست فرستاده نمی‌شود ؛ اگر هیچ contextی نبود writer_not_instrumented_count + 1
```

- **nested.** `set_client_enabled → add_client`، `ensure_present → add_peer → upsert_simple_queue` و مانند آن همه در همان context اجرا می‌شوند و همان token را می‌بینند؛ `node_gate` تودرتو برای همان node همان token را برمی‌گرداند (reentrant) و برای node دیگر رد می‌شود (قاعده‌ی «حداکثر یک gate»).
- **thread تازه** `ContextVar` را به ارث نمی‌برد؛ نویسنده در thread جدید بدون `node_gate` خودش fail-closed است.
- **خواندن‌ها token نمی‌خواهند** و بیرون از gate مجازند (poller آمار، تست اتصال، `read` برای نمایش). `read`ی که مبنای تصمیم یک mutation است داخل همان gate اجرا می‌شود (بخش ۹)، ولی guard آن را الزام نمی‌کند.
- تست AST بخش ۱۰.۲ می‌ماند و زودتر خطا می‌دهد؛ ولی نویسنده‌ای که در فهرست آن نیست هم در زمان اجرا متوقف می‌شود، چون از یکی از چهار transport بالا می‌گذرد.
- transport تازه (backend آینده با پروتکل دیگر) باید guard خودش را داشته باشد؛ تست LP-143 یک نویسنده‌ی ساختگی روی transport موجود را می‌سنجد و تست جدا وجود guard را روی هر کلاس client ثبت‌شده در `client_for_node` assert می‌کند.

### ۱۰.۸ runner قابل‌کشتن و thread گیرکرده

gate منقضی نمی‌شود، پس thread گیرکرده‌ای که نمی‌میرد node را تا مرگ نگه‌دارنده قفل می‌کند. این از نظر integrity درست است؛ قرارداد availability آن:

**سقف زمان clientها.** امروز هر client فقط timeout هر فراخوانی دارد (۱۰ ثانیه؛ MikroTik ۸ ثانیه) و سقف کل ندارد. برای هر backend سه مقدار صریح و قابل‌اندازه‌گیری تعریف می‌شود: `connect_timeout`، `read_timeout` (هر دو به همه‌ی فراخوانی‌های آن client داده می‌شوند؛ برای `requests` به‌صورت tuple) و `hard_deadline` برای کل یک نگه‌داشت gate (پیش‌فرض ۱۲۰ ثانیه؛ تصمیم باز ۱۴). مدت هر نگه‌داشت در `max_hold_ms` ثبت می‌شود.

**runner.** در `enforced`، هر نگه‌داشت gate در یک **process فرزند** اجرا می‌شود (start method `spawn`):

```
والد:  run_gated(node_id, action, args)          -- action نام یک تابع ثبت‌شده است ، نه callable دلخواه
         فرزند را می‌سازد ؛ args (شامل credential لازم) فقط از pipe داده می‌شود ، نه argv و نه env
فرزند: node_gate(node_id) را خودش می‌گیرد (flock ، GET_LOCK روی اتصال خودش ، بررسی مالکیت) → GateToken در فرزند
       action را اجرا می‌کند ؛ نتیجه‌ی تایپ‌شده را روی pipe می‌فرستد ؛ قفل‌ها را آزاد می‌کند ؛ خارج می‌شود
والد:  تا hard_deadline منتظر نتیجه می‌ماند
```

- token فقط در runner معتبر است (`token.pid` = pid فرزند)؛ پس در `enforced` هیچ نویسنده‌ای، از جمله مسیرهای legacy و poller، inline در process اصلی اجرا نمی‌شود. همه‌ی نویسنده‌های inventory ۱۰.۲ به actionهای ثبت‌شده تبدیل می‌شوند. در `off` و `shadow` اجرای inline امروز دست‌نخورده است.
- **گذشتن از `hard_deadline`:** والد فرزند را `SIGKILL` می‌کند و با `waitpid` مرگش را تأیید می‌کند. با مرگ process، kernel `flock` را آزاد می‌کند و بسته‌شدن socket اتصال اختصاصی `GET_LOCK` را. **هیچ مسیری قفل را در حالی که نگه‌دارنده ممکن است ادامه دهد آزاد نمی‌کند**: نه TTL، نه دستور دستی، نه `RELEASE_LOCK` از اتصال دیگر. `watchdog_kill_count + 1`.
- بعد از kill، نتیجه‌ی mutation نامعلوم است: گام در `remote_calling` (یا `compensating`) می‌ماند و recovery با `read` ادامه می‌دهد (۱۱.۳). executor بعدی فقط بعد از تأیید مرگ فرزند (آزادشدن واقعی قفل) وارد می‌شود؛ تا آن لحظه پشت همان gate منتظر است.
- **مرگ والد:** قرارداد دقیق در ۱۰.۹ (race بین spawn و نصب `PR_SET_PDEATHSIG`).
- process اصلی (API، RADIUS، بات) هرگز برای آزادکردن gate بسته نمی‌شود؛ فقط runner کشته می‌شود.
- **دیده‌شدن.** نگه‌دارنده‌ی هر node (pid runner، action، سن) در `GET /api/provisioning/status`؛ نگه‌داشت طولانی‌تر از نصف `hard_deadline` وضعیت `degraded` می‌دهد؛ هر kill یک خط log با node، backend، action و مدت، بدون args و بدون secret.

### ۱۰.۹ قرارداد runner: `RemoteActionDTO`، `RemoteActionResult` و مرز process

نویسنده‌های امروز داخل توابعی‌اند که `Session` و modelهای زنده‌ی SQLAlchemy دارند. هیچ‌کدام از آن‌ها از مرز process عبور نمی‌کند.

**تقسیم مسئولیت:**

| | والد (process اصلی) | فرزند (runner) |
|---|---|---|
| دیتابیس برنامه | تنها مالک T1، tx الف، tx ب، T_final، T_comp و هر نوشتن دیگر | **هیچ نوشتن.** فقط یک اتصال فقط-خواندنی محدود (پایین) |
| ساخت DTO | مقادیر ساده را از ORM استخراج می‌کند، **قبل از** spawn | — |
| gate | نمی‌گیرد | خودش `LOCK_SH` gate-mode، `flock` node و `GET_LOCK` را می‌گیرد و تا پایان action نگه می‌دارد |
| شبکه‌ی remote | هیچ نویسنده‌ای | خواندن و نوشتن remote همان action |
| نتیجه | `RemoteActionResult` را در یک تراکنش تازه ثبت می‌کند | فقط آن را می‌سازد و روی pipe می‌فرستد |

اتصال DB فرزند: MariaDB با `SET SESSION TRANSACTION READ ONLY`؛ SQLite با URI `mode=ro`. روی آن فقط این‌ها اجرا می‌شود: `GET_LOCK`/`IS_USED_LOCK`/`RELEASE_LOCK`؛ خواندن ردیف singleton `provisioning_runtime_state` (revalidation مالکیت و mode)؛ و برای actionهای WireGuard خواندن `nodes.mt_client_subnet` همان node بعد از گرفتن gate (بخش ۹.۱). فرزند `SessionLocal`، modelها و هیچ ماژول سرویس دیتابیسی را import نمی‌کند (LP-157 با hook رویداد نوشتن و با AST).

**`RemoteActionDTO`** (نسخه‌دار؛ فقط نوع‌های ساده: رشته، عدد، bool، None، list و dict از همین‌ها):

```
schema_version        INT                 -- نسخه‌ی قرارداد ؛ والد و فرزند باید برابر باشند
action_id             UUID                -- یکتا برای هر spawn ؛ در log و result تکرار می‌شود
action_type           از فهرست بسته‌ی جدول پایین ؛ نام تابع ثبت‌شده ، نه callable
node_id ، backend
node_config           پارامترهای اتصال همان node (host ، port ، ssl ، interface ، hub ، inbound tag/id ، مسیر config ، نام سرویس)
node_secrets          credential مدیریتی node (رمز/توکن/کلید SSH/رمز hub) ؛ secret
identity              هویت remote همان action (peer name ، public key ، allowed address ، email ، username ، ...)
credential            credential اتصال مشتری که action لازم دارد (uuid/key ، رمز ، PSK) ؛ secret ؛ کلید خصوصی WireGuard هرگز (router به آن نیاز ندارد)
params                پارامترهای غیرمحرمانه‌ی action (speed limit ، enabled ، نام جدید ، فهرست clientهای rebuild)
timeouts              connect_timeout ، read_timeout ، hard_deadline
contract              matcherهای not_exist همان node (۵.۱۰) و adapter_version
fencing               installation_uuid ، host_id ، boot_id ، ownership_epoch ، gate_mode_epoch ، expected_parent_pid ،
                      operation_id / step_id / lease epoch (فقط برای audit ؛ None برای مسیر legacy)
```

- هیچ `Session`، `Query`، شیء ORM، callback، closure یا شیء client در DTO نیست. سازنده‌ی DTO نوع هر مقدار را بازگشتی می‌سنجد و هر نوع دیگر را رد می‌کند (`TypeError`)؛ serialization با JSON است، نه pickle، پس شیء دلخواه اصلاً قابل انتقال نیست.
- **secretها:** `node_secrets` و `credential` فقط از pipe خصوصی والد به فرزند می‌روند (یک پیام طول‌دار بعد از spawn)؛ هرگز در argv، env، نام process، یا فایل موقت. `repr`/`str` DTO و هر log فقط `redacted()` را می‌بیند که این دو بخش را با `"<redacted>"` جایگزین می‌کند و از `identity` فقط فیلدهای غیرمحرمانه را نگه می‌دارد.
- فرزند DTO را با `schema_version`، `action_type` و schema همان action اعتبارسنجی می‌کند؛ ناسازگاری → خروج قبل از gate و شبکه.

**`RemoteActionResult`:**

```
schema_version ، action_id            -- باید با DTO برابر باشند ، وگرنه والد آن را unknown حساب می‌کند
outcome                               -- یکی از هشت مقدار جدول پایین
write_attempted       BOOL            -- آیا حداقل یک فراخوانی نویسنده فرستاده شد
remote_outcome        NULL | verified_absent | delete_idempotently_absent | unverified
conflict_code         NULL | کد ثابت (مثل conflict_uuid_taken)
observed              مقادیر غیرمحرمانه‌ی خوانده‌شده (مثل آدرس interface ، نام گواهی ساخته‌شده ، نتیجه‌ی هر قلم rebuild)
error_code ، error_sanitized ، duration_ms
```

| `outcome` | چه کسی می‌سازد | معنا | گام `create` | گام `remove` / compensation |
|---|---|---|---|---|
| `succeeded` | فرزند | نوشتن انجام و با `read` بعدی تأیید شد | `remote_created` | — |
| `already_present_verified` | فرزند | `read` اول `present_match` بود؛ بدون نوشتن | `remote_created` | — |
| `absent_verified` | فرزند | نبودن تأیید شد (`remote_outcome` می‌گوید کدام نوع) | (در recovery: `staged`) | `removed` |
| `conflict` | فرزند | `present_conflict`؛ هیچ نوشتنی | `cleanup_required` | `cleanup_required` (`unverified`) |
| `unreadable` | فرزند | خواندن معتبر ممکن نبود | retry؛ بعد از سقف `cleanup_required` | retry؛ بعد از سقف `cleanup_required` |
| `transport_error` | فرزند | خطای اتصال؛ `write_attempted` می‌گوید قبل یا بعد از نوشتن | `write_attempted = false`: retry. `true`: unknown | همان |
| `timeout_unknown` | فرزند یا والد | یک فراخوانی نویسنده timeout شد | unknown | unknown |
| `killed_unknown` | **فقط والد** | `SIGKILL`، مرگ فرزند، قطع pipe، یا result ناسازگار/ناقص | unknown | unknown |

**unknown** یعنی نتیجه نه موفق فرض می‌شود و نه ناموفق: گام در `remote_calling` (یا `compensating`) می‌ماند و فقط `read` بعدی داخل gate (recovery ۱۱.۳) آن را تعیین می‌کند. برای مسیرهای legacy که گام ندارند، unknown به caller همان خطای امروز را می‌دهد و چیزی در DB نوشته نمی‌شود.

**مرگ والد (race spawn):**

```
فرزند ، اولین کارها ، قبل از import سنگین ، قبل از خواندن secret ، قبل از gate:
  ۱. prctl(PR_SET_PDEATHSIG, SIGKILL)
  ۲. اگر getppid() != expected_parent_pid (از argv ؛ غیرمحرمانه) → خروج فوری        -- والد قبل از قدم ۱ مرده بود
  ۳. خواندن DTO و secretها از pipe ؛ EOF → خروج
  قبل از هر فراخوانی نویسنده: getppid() دوباره ؛ ناهم‌خوان → خروج بدون نوشتن
```

- این قرارداد **Linux** است (و Docker روی Linux، که استقرار پروژه است). `PR_SET_PDEATHSIG` به thread سازنده‌ی فرزند وابسته است؛ پس runner از یک thread ثابت و عمری در والد spawn می‌شود، نه از thread موقت threadpool.
- روی سیستم بدون `prctl` (macOS برای توسعه و تست) فقط بررسی `getppid()` و EOF pipe وجود دارد؛ این fallback فقط برای تست است و `gate_mode = 'enforced'` روی چنین سیستمی در خودآزمایی runner رد می‌شود.

**نگاشت هر نویسنده‌ی امروز به `action_type`.** هر فراخوانی نویسنده‌ی remote در `backend/app` (inventory کامل با grep روی همه‌ی متدهای نویسنده‌ی هشت client):

| محل امروز | نویسنده‌ها | `action_type` | نکته‌ی تبدیل |
|---|---|---|---|
| `user_ops.provision_wireguard` (۹۳۹؛ ۹۶۸-۹۸۴) | `ensure_wireguard_interface`، `set/ensure_interface_address`، `add_peer`، `upsert_simple_queue` | `wg_ensure_present` | والد IP، کلید و نام peer را قبل از spawn می‌سازد؛ فرزند فقط public key می‌گیرد |
| `user_ops.provision_xray` (۱۱۳۰؛ ۱۱۴۴) | `add_client` هر شش client | `xray_ensure_present` (adapter را `backend` تعیین می‌کند) | uuid را والد می‌سازد و می‌دهد؛ دیگر client آن را نمی‌سازد |
| `user_ops.provision_softether` (۱۱۶۵؛ ۱۱۹۲) | `add_client` | `softether_ensure_present` | — |
| `user_ops.deprovision_connection` (۱۶۴۸؛ ۱۶۶۱، ۱۶۸۱، ۱۶۹۰، ۱۶۹۳) | `remove_peer`، `remove_simple_queue`، `remove_client` (Xray، SoftEther) | `wg_ensure_absent`، `xray_ensure_absent`، `softether_ensure_absent` | مسیر legacy نتیجه را با همان مدارای امروز تفسیر می‌کند (نبودن → warning)؛ مسیر durable سخت‌گیرانه |
| `user_ops.kick_connection` (۱۷۱۹؛ ۱۷۴۷، ۱۷۶۶-۱۷۶۷، ۱۷۷۹-۱۷۸۰، ۱۷۹۲-۱۷۹۳) | `kick_ppp_session`؛ خاموش و روشن پشت‌سرهم peer، client Xray، کاربر SoftEther | `ppp_kick_session`، `wg_bounce_peer`، `xray_bounce_client`، `softether_bounce_user` | خاموش و روشن یک action و یک نگه‌داشت gate است |
| `quota_manager._set_connection_enabled` (۵۸۹؛ ۶۳۲، ۶۵۱، ۶۶۳؛ callerها ۴۵۰، ۵۶۷، ۵۸۳، ۷۵۳) | `set_peer_disabled`، `set_client_enabled` (Xray، SoftEther) | `wg_set_peer_enabled`، `xray_set_client_enabled`، `softether_set_user_enabled` | nested `set_client_enabled → add_client` داخل همان action در فرزند |
| `routers/users.update_connection` (۱۱۹۹؛ ۱۲۲۷، ۱۲۴۸-۱۲۵۴) | `rename_peer`، `upsert_simple_queue`، `remove_simple_queue` | `wg_rename_peer`، `wg_set_speed_queue` | — |
| `routers/nodes.push_radius_config` (۲۳۲؛ ۲۵۴) | `push_radius_config` | `mt_push_radius` | RADIUS secret در `credential` |
| `routers/nodes.push_pptp_config` (۲۶۸؛ ۲۸۶، ۲۹۳) | `push_radius_config`، `push_pptp_config` | `mt_push_pptp` | هر دو در یک action |
| `routers/nodes.push_sstp_config` (۳۰۱؛ ۳۱۸، ۳۲۵) | `push_radius_config`، `push_sstp_config` (و درون آن `ensure_self_signed_certificate`، `mikrotik_client.py:465`) | `mt_push_sstp` | نام گواهی در `observed` برمی‌گردد؛ والد آن را روی node ذخیره می‌کند |
| `routers/nodes.push_l2tp_config` (۳۳۵؛ ۳۵۴، ۳۶۱) | `push_radius_config`، `push_l2tp_config` | `mt_push_l2tp` | IPsec secret در `credential` |
| `routers/nodes.push_ikev2_config` (۳۶۹؛ ۳۹۳، ۴۰۰، ۴۰۷) | `push_radius_config` (دو بار)، `push_ikev2_config` | `mt_push_ikev2` | PSK در `credential` |
| `routers/nodes.rebuild_node_clients` (۴۳۵؛ ۴۷۶) | `add_client` برای هر اتصال ذخیره‌شده | `xray_rebuild_clients` | فهرست `(email, uuid, flow)` در DTO؛ یک نگه‌داشت gate؛ نتیجه‌ی هر قلم در `observed` |
| reconciliation (۱۰.۵) | `ensure_interface_address_exact_or_wider` | `wg_reconcile_interface_address` | — |
| تأیید قرارداد (۵.۱۰) | ساخت و حذف موجودیت آزمایشی | `contract_probe` | — |
| adapterهای بخش ۹ | همه‌ی `ensure_present`/`ensure_absent` | همان `*_ensure_present` / `*_ensure_absent` بالا | هر action پارامتر `semantics` دارد: `legacy` (همان متد client امروز، بدون قرارداد) یا `durable` (adapter بخش ۹، فقط روی node با قرارداد `ready`) |

نویسنده‌های درونی clientها که caller بیرونی ندارند و فقط داخل یک action اجرا می‌شوند: `XrayClient.write_config` و `restart_service` (`xray_client.py:304-305`، `:315-316`)؛ read-modify-write کامل config در یک نگه‌داشت gate؛ self-heal `set_client_enabled → add_client` در هر هفت client (`xray_client.py:335`، `threexui_client.py:475`، `softether_client.py:210`، `marzban_client.py:338`، `hiddify_client.py:213`، `marzneshin_client.py:343`، `sui_client.py:259`)؛ و `edit` کامل ردیف در S-UI (خواندن رکورد کامل و نوشتن آن در همان action).

سه متد نویسنده‌ی `MikrotikClient` امروز هیچ callerی ندارند: `setup_socks_proxy` (۶۰۶)، `disable_socks_proxy` (۷۰۱)، `ensure_masquerade` (۵۵۱). `action_type` ندارند؛ guard (۱۰.۷) آن‌ها را در `enforced` رد می‌کند و هر caller آینده باید action ثبت کند.

بیرون از gate و runner (node را mutate نمی‌کنند): `services/wg_tunnel.py` (subprocess محلی روی host پنل)، `services/remote_deploy.py` و `local_deploy.py` (استقرار بات)، و همه‌ی خواندن‌ها (poller آمار، تست اتصال، import).

تست LP-163 این جدول را با کد تطبیق می‌دهد: هر فراخوانی متد نویسنده در `backend/app` باید داخل تابع یک action ثبت‌شده باشد، و هر `action_type` ثبت‌شده باید در این جدول باشد.

### ۱۰.۱۰ lifecycle `installation_uuid`

`installation_uuid` در دو جا نگه داشته می‌شود و باید برابر باشند: ستون `provisioning_runtime_state.installation_uuid` و فایل `{lock_dir}/installation.id`. فایل نام شاخه‌ی قفل‌ها و نام `GET_LOCK` را تعیین می‌کند؛ ستون مرجع است.

| وضعیت | رفتار |
|---|---|
| اولین startup بعد از L0 | ستون ساخته شده؛ فایل از روی ستون نوشته می‌شود |
| restore همان نصب (دیتابیس و شاخه‌ی داده با هم) | هر دو برابر؛ UUID حفظ می‌شود؛ همان namespace |
| restore فقط دیتابیس، فایل نیست | `gate_mode = 'off'`: فایل از روی ستون بازسازی می‌شود. `shadow`/`enforced`: fail-closed تا verify/reclaim/takeover موفق، که فایل را می‌نویسد |
| **فایل و ستون متفاوت** (ویرایش دستی فایل، یا شاخه‌ی داده‌ی نصب دیگر) | زیرسیستم provisioning در startup fail-closed: هیچ gate، هیچ runner، هیچ mutation remote (در `off` هم، چون namespace نامعلوم است)؛ `GET /api/provisioning/status` وضعیت `installation_mismatch`. بقیه‌ی برنامه (API، RADIUS، بات) بالا می‌آید. رفع فقط با برگرداندن فایل، یا rotate/fork-reset |
| fork: ساخت یک نصب مستقل از clone | `installation/rotate` با `fork=true` (پایین) |

**`POST /api/provisioning/installation/rotate`** (superadmin + تأیید رمز + دلیل متنی؛ event دائمی):

| حالت | پیش‌شرط | اثر |
|---|---|---|
| rotate عادی (`fork=false`) | `gate_mode = 'off'`؛ همه‌ی `operation_type`ها `legacy`؛ `owner_state IN ('none','released')`؛ هیچ runner زنده؛ هیچ عملیات غیرترمینال؛ هیچ قفل node نگه‌داشته (probe) و `LOCK_EX` gate-mode گرفته شود | UUID تازه در ستون و فایل؛ event `installation_rotate` |
| fork-reset (`fork=true`)، برای clone که حالت نصب اصلی را کپی کرده | عبارت تأیید ثابت «این یک نصب مستقل جدید است و نصب اصلی جدا ادامه می‌دهد»؛ هیچ عملیات غیرترمینال در این کپی؛ هویت host این process با `owner_host_id` کپی‌شده **متفاوت** است (روی host مالک اصلی رد می‌شود) | یک تراکنش: همه‌ی انواع → `legacy`؛ `gate_mode → 'off'`؛ `owner_* → NULL`، `owner_state='none'`؛ همه‌ی ردیف‌های `provisioning_node_contracts` → `invalidated`/`manual`؛ UUID تازه؛ event `installation_fork_reset` |

- fork با عملیات غیرترمینال رد می‌شود: آن عملیات به منبع‌های remote نصب اصلی اشاره می‌کنند و نباید از دو نصب ادامه یابند. snapshot برای fork باید از حالت drain‌شده گرفته شود.
- rotate نام همه‌ی قفل‌ها را عوض می‌کند؛ به همین دلیل فقط وقتی مجاز است که هیچ قفلی با نام قبلی نگه داشته نشده.
- دو نصب که بعد از fork هنوز یک node را مدیریت می‌کنند قفل مشترکی ندارند؛ این پشتیبانی نمی‌شود و در fork-reset قراردادهای node باطل می‌شوند تا هر node دوباره آگاهانه verify شود.

---

## ۱۱. Recovery پس از crash و compensation

### ۱۱.۱ worker

یک job APScheduler در `_start_full_services`. هر چرخه عملیات غیرترمینال با `next_retry_at <= DB_NOW` را که قفل `provisioning_op:{id}` آن آزاد یا منقضی است برمی‌دارد.

### ۱۱.۲ تصمیم برای هر عملیات

| state عملیات | کار |
|---|---|
| `prepared`، `provisioning` | برای هر گام طبق ۱۱.۳ ادامه؛ اگر `DB_NOW > forward_deadline` یا خطای قطعی → `compensating` |
| `remote_complete` | T_final؛ اگر شکست قطعی (جدول ۱۱.۴) → `compensating` |
| `compensating` | هر گام با `remote_attempted = 1` → `ensure_absent`؛ هر گام با `remote_attempted = 0` مستقیم `removed`؛ وقتی همه `removed` → T_comp |
| `cleanup_required` | فقط با retry اپراتور یا backoff بلند؛ هرگز خودکار `compensated` نمی‌شود |

### ۱۱.۳ هر گام بعد از crash

| state گام | کار |
|---|---|
| `staged` | مسیر عادی ۷.۲ |
| `remote_calling` | `read`: `present_match` → `remote_created`؛ `absent` → `ensure_present` (همان هویت، همان credential، پس منبع دوم ساخته نمی‌شود)؛ `present_conflict` → `cleanup_required`؛ `unreadable` → retry با backoff، بعد از سقف → `cleanup_required` |
| `remote_created` | هیچ؛ منتظر T_final |
| `compensating` | `ensure_absent` |

credential در ستون‌های staged است، پس recovery می‌تواند همان منبع را با همان کلید/رمز/uuid کامل کند؛ هیچ credentialی فقط در حافظه نبوده.

### ۱۱.۴ خطای قطعی (بدون roll-forward)

| وضعیت | نتیجه |
|---|---|
| `present_conflict` روی هر گام | `cleanup_required` (نه compensation خودکار: منبع ناشناس دست نمی‌خورد) |
| node حذف یا غیرفعال شده | `compensating` |
| User هدف (existing) دیگر وجود ندارد | `compensating` |
| `wg_interface_address_conflict`، `wg_ip_outside_subnet` (۹.۱) | `compensating` |
| approval: binding کلید اجرا باطل شد، version با snapshot نمی‌خواند، یا approval دیگر `mutating` نیست | **نه** `compensating`: `cleanup_required` + `approval_recovery` (۷.۶)؛ T_final و T_comp هر دو متوقف |
| wallet epoch عوض شده | `compensating` |
| `forward_deadline` گذشته | `compensating` |

### ۱۱.۵ T_comp

```
پیش‌شرط: هر گام create در 'removed' (با remote_outcome معتبر یا 'abandoned')
tx: locking readهای runtime ؛ revalidation lease
    اگر approval دارد: قفل ردیف approval ؛ بررسی binding (۷.۶) ؛ state = 'mutating' ؛ NOT EXISTS (receipt_approval_effects همین approval)
                       هرکدام برقرار نبود → ROLLBACK و مسیر approval_recovery (۷.۶)
    release هر reservation با state='reserved'
    one-time claimها: release_seq = id ، released_at
    operation: state='compensated' ، username_claim = NULL ، completed_at ، error_code (کد ثابت علت، پایین)
    اگر approval دارد: approval.state 'mutating' → 'failed' ، failed_at
COMMIT
```

- همه یک commit‌اند: release reservationها، آزادسازی claimها، `compensated` شدن عملیات و `failed` شدن approval. شکست تزریق‌شده یعنی هیچ‌کدام؛ عملیات `compensating` و approval `mutating` می‌ماند و T_comp دوباره اجرا می‌شود.
- approval بدون effect است، چون effectها فقط در T_final نوشته می‌شوند و T_final این عملیات commit نشده؛ شرط `NOT EXISTS` همین را دوباره می‌سنجد تا `failed` با effect هرگز ساخته نشود.
- **خطای ثابت و auditable.** علت روی `provisioning_operations.error_code` می‌ماند، که با `(approval_uuid, approval_execution_version)` به همان تلاش approval وصل است و هرگز پاک نمی‌شود. مقادیر مجاز (فهرست بسته در کد): `step_failed_permanent`، `forward_deadline_exceeded`، `node_unavailable`، `target_user_missing`، `wallet_epoch_changed`، `wg_interface_address_conflict`، `wg_ip_outside_subnet`، `final_tx_failed_permanent`، `cancelled_by_recovery`. جزئیات هر گام در `provisioning_steps.error_code` و `error_sanitized` است؛ هیچ‌کدام secret ندارد. endpoint audit approval این ردیف را با همان دو کلید نشان می‌دهد.
- اگر حتی یک گام در `cleanup_required` باشد، T_comp اجرا نمی‌شود، پول رزروشده می‌ماند و approval `mutating` می‌ماند.
- **force:** `POST /api/provisioning/operations/{id}/force-step` فقط superadmin، با تأیید رمز و دلیل متنی. در یک تراکنش: گام `cleanup_required → removed` با `remote_outcome='abandoned'`، `forced_by_admin_id`، `force_reason`، `forced_at`. audit orphan همان ردیف گام است: node، backend و هویت غیرمحرمانه‌ی منبع در ستون‌هایش هست و جدول مسیر حذف ندارد (LP-70)؛ پروژه جدول audit عمومی ندارد و این سند هم نمی‌سازد. `GET /api/provisioning/orphans` (superadmin) همه‌ی گام‌های `abandoned` را فهرست می‌کند. سپس T_comp (برای حذف: T_final حذف).

### ۱۱.۶ چرا orphan و سرویس رایگان ممکن نیست

| لحظه‌ی crash | عملیات و پول | approval (اگر دارد) | نتیجه |
|---|---|---|---|
| قبل از شروع T1 | هیچ | `registered` | هیچ اثری؛ retry همان token عادی اجرا می‌شود |
| وسط T1 (بعد از `registered → mutating` در حافظه‌ی تراکنش، قبل از COMMIT) | rollback: هیچ ردیف، هیچ پول | `registered` (تغییر commit نشده) | نه approval سرگردان، نه عملیات ناقص |
| بعد از commit T1، قبل از هر remote | `prepared`، پول `reserved` | `mutating` | roll-forward؛ یا compensation و T_comp |
| بعد از tx الف، قبل یا بعد از remote | گام `remote_calling` | `mutating` | `read` تعیین می‌کند؛ هویت ثبت است، پس منبع ساخته‌شده قابل پیدا و حذف است |
| بعد از tx ب | گام `remote_created` | `mutating` | T_final یا compensation |
| وسط T_final | rollback؛ `remote_complete`؛ پول `reserved` | `mutating`، بدون هیچ effect | retry T_final یا compensation؛ هیچ `Connection` و هیچ effect نیمه‌کاره |
| بعد از commit T_final | `completed`، پول `captured` | `completed` با همه‌ی effectها | تمام |
| وسط حذف remote در compensation | `compensating`؛ گام `compensating` | `mutating` | `ensure_absent` دوباره (idempotent) |
| وسط T_comp | rollback؛ `compensating`؛ پول `reserved` | `mutating` | T_comp دوباره |
| بعد از commit T_comp | `compensated`، پول `released` | `failed`، بدون effect | retry کنترل‌شده‌ی approval → version + 1 → عملیات تازه |
| revoke کلید بعد از T1 | `cleanup_required`؛ پول `reserved` | `mutating` | فقط `approval_recovery` (۷.۶) |

---

## ۱۲. حذف با همان ماشین

`delete_connection`، `delete_purchase`، `delete_user`:

```
T1:  INSERT operation ؛ برای هر Connection دامنه یک گام direction='remove' با هویت کپی‌شده از ردیف Connection (بدون credential)
     Connection.enabled = False برای همه ؛ برای delete_user: مسدودی خرید و status غیرفعال
     COMMIT                                      -- RADIUS همین را زنده می‌خواند، پس PPP فوراً رد می‌شود
هر گام:  ensure_absent → removed | cleanup_required
T_final: فقط وقتی همه‌ی گام‌ها removed (یا abandoned): حذف فقط-DB با finalizerهای بدون remote و بدون commit ؛ operation completed
```

- finalizerها همان‌هایی‌اند که Receipt Void بخش ۱۳.۷ تعریف کرده (`finalize_user_deletion_after_deprovision`، `finalize_purchase_deletion_after_deprovision`)، به‌علاوه‌ی معادل تک‌اتصال. هر اتصال دقیقاً یک‌بار deprovision می‌شود.
- گام‌های ابطال رسید (`deprovision:{connection_id}` و `verify:{connection_id}`) روی همین adapterها می‌نشینند: `ensure_absent` هر دو کار را می‌کند و همان چهار `remote_outcome` را برمی‌گرداند.
- حذف reservation ندارد و برگشت‌پذیر نیست: بعد از T1 حذف، اتصال‌ها غیرفعال می‌مانند؛ `cleanup_required` یعنی حساب مسدود می‌ماند تا retry یا force.
- حذف WireGuard با چهارتایی هویت است، نه فقط comment. peer با comment برابر ولی کلید متفاوت حذف نمی‌شود و `unverified` می‌دهد؛ این جلوی حذف کورکورانه را می‌گیرد و orphan بی‌صدای `deprovision_connection` امروز (۱۶۶۲-۱۶۷۷) را به یک وضعیت قابل‌پیگیری تبدیل می‌کند.

---

## ۱۳. API و سازگاری caller

- **پاسخ موفق sync بدون تغییر.** درخواست T1، گام‌ها و T_final را در همان thread اجرا می‌کند و وقتی `completed` شد همان بدنه‌ی امروز را برمی‌گرداند (از جمله `connections` که handlerهای بات می‌خوانند).
- **بودجه‌ی زمانی.** اگر عملیات تا پایان بودجه‌ی درخواست ترمینال نشد: HTTP 409 با `{"error":"operation_in_progress","operation_id":...,"state":...}`. caller قدیمی این را مثل هر خطای دیگر می‌بیند (نه موفقیت کاذب)؛ caller جدید با همان `idempotency_token` دوباره می‌پرسد.
- **تکرار با همان کلید:** `completed` → پاسخ کامل بازسازی‌شده از ردیف‌های نتیجه؛ `compensated` → همان خطای اصلی؛ غیرترمینال → `operation_in_progress`؛ `request_hash` متفاوت → ۴۰۹.
- **شکست:** وقتی عملیات `compensated` شد، همان کد و متن خطای امروز (مثلاً ۴۰۰ با پیام node).
- هدر `X-Idempotency-Key` اختیاری است؛ نبودنش رفتار امروز را می‌دهد.
- `GET /api/provisioning/operations/{id}` با همان scope مجوز درخواست اصلی؛ `POST .../retry` و `POST .../force-step` فقط superadmin با رمز و دلیل؛ `GET /api/provisioning/orphans` فقط superadmin.
- **درخواست approvalدار:** token قدیمی → ۴۰۹ `approval_superseded` با state فعلی approval؛ کلید revoke‌شده → ۴۰۳ `execution_key_revoked`؛ کلید دیگر → ۴۰۳ `execution_principal_mismatch`؛ پیش‌شرط شکست‌خورده → ۴۰۹ `manifest_precondition_failed`؛ mode `blocked` → ۵۰۳ `receipt_approval_blocked`. همه همان کدهای سند Receipt Void‌اند. `apply_referral`، `redeem_discount` و `record_card_payment` با `approval_uuid` طبق ۷.۶ پاسخ idempotent می‌دهند.
- هیچ credentialی در هیچ پاسخ وضعیت نیست؛ `connections` فقط بعد از `completed` و از ردیف‌های `Connection`.
- UI: وضعیت‌های `in_progress`، `cleanup_required` (با node و اتصال شکست‌خورده)، retry و force؛ ساخت گروهی فهرست موفق/ناموفق با دلیل.

---

## ۱۴. نگاشت به قرارداد ۱۲.۳ Receipt Void

| خواسته‌ی قرارداد | در این سند |
|---|---|
| reference مشترک `hold_ref` | `payment_reservations.hold_ref`؛ برای `wallet_lot` همان `wallet_debit_events.hold_ref`؛ `wallet_debit_events.provisioning_ref` = `provisioning_operations.id` |
| ترتیب: hold → ثبت durable گام‌ها قبل از remote → remote → `remote_created` → تراکنش نهایی | T1 (reserve و گام‌های staged در یک commit) → ۷.۲ → T_final |
| idempotency token remote: هویت قطعی مشتق از عملیات و slot، نوشته‌شده در منبع | ستون‌های هویت گام (`wg_peer_name`، `xr_email`، `account_username`) در T1 ثابت می‌شوند؛ `slot_key` همان slot manifest |
| stateهای گام: `staged → remote_calling → remote_created → active`؛ `compensating → removed` یا `cleanup_required` | بخش ۶.۲، با همین نام‌ها |
| زمان ساخت ردیف‌ها: debit در قدم ۱، LedgerEntry فروش و capture فقط در commit نهایی | بخش ۷.۱ و ۷.۳ |
| compensation: حذف remote با همان هویت، سپس release | بخش ۱۱.۵ |
| `cleanup_required`: hold آزاد نمی‌شود | policy ۵، بخش ۱۱.۵ |
| recovery: گام `remote_calling` با خواندن remote تعیین تکلیف می‌شود؛ بدون خواندن → `cleanup_required` | بخش ۱۱.۳ |
| reservation ساخت‌یافته پیش از cutover: شناسه‌ی User، مبلغ، `reserved/captured/released`، LedgerEntry بعد از capture | `payment_reservations` با `generation='legacy_balance'` |
| بررسی cutover: هیچ عملیات غیرترمینال با reservation کیف‌پولی | query بخش ۸ |
| hold بدون عملیات provisioning زنده → `wallet_hold_resolution` | در این طراحی هر hold یک عملیات دارد؛ worker آن را ترمینال می‌کند. `wallet_hold_resolution` فقط برای holdی می‌ماند که ردیف عملیاتش ناسازگار است |
| پیش‌شرط target و UNIQUE قبل از remote (RV-208) | `username_claim` و بررسی‌های T1؛ برخورد در T_final → compensation بدون orphan |
| slot اتصال پاس داده می‌شود و Connection با slot خودش برمی‌گردد | `provisioning_steps.slot_key` → `connection_id` در T_final |
| finalizerهای فقط-DB | بخش ۱۲ |
| RV-116، RV-117 | LP-21، LP-23 |
| پیش‌شرط اجرا: یک تراکنش زیر قفل ردیف approval، `registered → mutating` یا `cancelled` (۵.۳، ۶.۵) | قدم ۲ از T1؛ هم‌commit با INSERT عملیات (۷.۱، ۷.۶) |
| execution token = `version`؛ فقط takeover و retry کنترل‌شده آن را عوض می‌کند (۵.۱، ۶.۵) | `approval_execution_version`؛ `business_key = approval:{uuid}:v:{version}`؛ UNIQUE دوم (۵.۱) |
| binding instance کلید اجرا در پیش‌شرط، هر effect و finalize (۶.۴) | بررسی binding در T1، T_final و T_comp (۷.۶) |
| revoke کلید وسط `mutating`: approval `mutating` می‌ماند؛ فقط `approval_recovery` (۶.۴، RV-243) | `cleanup_required` + `approval_recovery`؛ نه T_final، نه T_comp (۷.۶) |
| `failed` فقط بدون effect؛ retry کنترل‌شده `version + 1` (۵.۱، RV-219) | T_comp با `NOT EXISTS effects`؛ عملیات تازه برای version تازه (۷.۶، ۱۱.۵) |
| سه outcome حذف remote و `unverified` هرگز `done` نمی‌شود (۱۳.۴، RV-121 تا RV-123) | بخش ۹؛ SoftEther جدول ۹.۵؛ LP-84 تا LP-88 |
| force: superadmin + رمز + دلیل؛ orphan و actor ثبت (۱۳.۵، RV-124) | ۱۱.۵؛ ستون‌های `forced_*`؛ LP-89 |

---

## ۱۵. HA

HA این پروژه snapshot یک‌طرفه است؛ standby دیتابیس جدا و عقب‌افتاده دارد و در split-brain هر دو طرف خود را primary می‌دانند. `provisioning_operations` هم داخل همان snapshot است، پس بعد از failover روی snapshot عقب‌افتاده، عملیاتی که روی primary قبلی کامل یا جلوتر رفته ممکن است این‌جا `prepared` دیده شود و worker آن را دوباره اجرا یا compensate کند.

| وضعیت | رفتار |
|---|---|
| `ha_enabled = False` | mode `durable` و worker مجاز |
| `ha_enabled = True`، بدون coordinator بیرونی | تغییر هر نوع به `durable` رد می‌شود (۴۰۹)؛ همه‌ی انواع `legacy` می‌مانند؛ worker ثبت نمی‌شود |
| حداقل یک نوع `durable` و تلاش برای روشن‌کردن HA | رد (guard روی endpoint تنظیمات HA) |

این با سند Receipt Void یکی است (فازهای P6 به بعد روی HA بدون coordinator رد می‌شوند). این سند split-brain را حل نمی‌کند.

---

## ۱۶. Rollout / Rollback / Drain

| فاز | محتوا | سوئیچ | Rollback |
|---|---|---|---|
| L0 | جدول‌ها؛ یک ردیف `legacy` برای هر نوع در `provisioning_type_modes` | — | لازم نیست |
| L1a | کد `node_gate`، guard زمان اجرا، runner و `RemoteActionDTO`، تبدیل همه‌ی نویسنده‌های جدول ۱۰.۹ به action، binding clientها، سقف زمان clientها، adapterهای هر نُه backend (`read`، `ensure_present`، `ensure_absent`) و متدهای تایپ‌شده‌ی جدید clientها. `gate_mode = 'off'`: هر مسیر remote فقط `LOCK_SH` مشترک gate-mode را می‌گیرد؛ قفل node، `GET_LOCK`، runner، انتظار عادی و serialization وجود ندارد؛ رفتار تجاری و concurrency روی node برابر baseline است | — | برگرداندن کد |
| L1b | فایل هویت host؛ `lock-backend/verify` و claim مالکیت؛ `gate_mode = 'shadow'`: فقط اندازه‌گیری contention و نویسنده‌ی instrument‌نشده | `verify`؛ `gate-mode` | `shadow → off` (شرط‌های ۱۰.۶) |
| L1c | `gate_mode = 'enforced'`: serialization واقعی و اجرای همه‌ی نویسنده‌ها در runner. پیش‌شرط: `writer_not_instrumented_count = 0` و `mode_lock_miss_count = 0`؛ contention و آمادگی قرارداد nodeها شرط نیستند. مسیرهای legacy با همان semantics قبلی، ولی سریال | `gate-mode` | `enforced → shadow/off` فقط با همه‌ی انواع `legacy`، بدون عملیات غیرترمینال و بدون نگه‌دارنده |
| L2 | همه‌ی coreهای بدون commit جدول ۷.۳ با wrapperهای legacy (از جمله referral، تخفیف و کارت)؛ `_wg_used_ips` با گام‌های غیرترمینال؛ reserve بدون commit برای اعتبار reseller | — | برگرداندن کد |
| L3 | **بعد از L1c** (probe فقط در `enforced` و از runner اجرا می‌شود): bootstrap probe زنده‌ی **هر node** و ثبت در `provisioning_node_contracts` (۵.۱۰)؛ staging contract هر نُه backend (بخش ۹.۶): ثبت عین پاسخ‌ها، از جمله حذف کاربر ناموجود SoftEther و رفتار جایگزینی آدرس interface روی RouterOS | `contract/verify` | — |
| L4 | `add_connection` و `delete_connection` → `durable` (کم‌ریسک‌ترین: یک گام، بدون پول) | ردیف همان نوع | بازگشت به `legacy` فقط وقتی هیچ عملیات غیرترمینال آن نوع نیست |
| L5 | `renew_user`، `renew_purchase` → `durable` (تک‌تراکنشی) | همان | همان |
| L6 | `purchase` → `durable`؛ backfill claimهای one-time در همان تراکنش تغییر mode | همان | همان |
| L7 | `create_user` → `durable`؛ ساخت گروهی به N عملیات | همان | همان |
| L8 | `delete_purchase`، `delete_user` → `durable` | همان | همان |
| **P6 کامل** | `p6_ready()` (۷.۷) برقرار؛ همه‌ی انواع `durable`؛ reconciliation WireGuard روشن؛ حذف `add_balance(-price)` و refund بهترین‌تلاش از `miniapp.py` و `customer_purchase.py` (خرید کیف‌پولی از همان عملیات `purchase`/`create_user` با reservation) | — | — |

- **ترتیب:** L0 → L1a → L1b → L1c → L2 → L3 → L4 تا L8. L2 اثر remote ندارد و می‌تواند قبل یا بعد از L1c باشد؛ هیچ `operation_type` قبل از تکمیل L3 برای nodeهای فعال `durable` نمی‌شود.
- **پیش‌شرط هر L4 تا L8:** `ha_enabled = False`؛ L1c و L3 انجام شده: `gate_mode = 'enforced'` و مالکیت host `active` روی همین `(host, boot)` (۱۰.۴، ۱۰.۶)؛ برای هر node با `enabled = True` ردیف `provisioning_node_contracts` در `ready` با نسخه و fingerprint فعلی (۵.۱۰).
- **Drain:** بازگشت یک نوع به `legacy` وقتی عملیات غیرترمینال دارد رد می‌شود؛ worker تا ترمینال‌شدن همه‌ی آن‌ها روشن می‌ماند. خاموش‌کردن worker با عملیات غیرترمینال رد می‌شود.
- **caller قدیمی:** بات remote قدیمی که `idempotency_token` نمی‌فرستد کار می‌کند (هر درخواست مستقل)، فقط از retry امن محروم است.
- **ترتیب با Receipt Void:** P6 کامل پیش‌شرط P9 آن سند است. RV-116 و RV-117 آن سند با LP-21 و LP-23 این سند پوشش داده می‌شوند.

---

## ۱۷. Test Matrix

همه بدون شبکه‌ی واقعی (guard سوکت fail-closed و client mock). «دو دیالکت» یعنی SQLite و MariaDB در CI. هیچ‌کدام پیاده نشده.

### عملیات و idempotency

| کد | سناریو | Invariant |
|---|---|---|
| LP-1 | همان `business_key`، همان payload | همان عملیات؛ بدون reserve دوم |
| LP-2 | همان کلید، payload متفاوت | ۴۰۹ |
| LP-3 | دو actor مختلف، یک approval | یک عملیات |
| LP-4 | درخواست بدون `idempotency_token` | عملیات مستقل، مثل امروز |
| LP-5 | دو `create_user` هم‌زمان با یک username (دو Session، دو دیالکت) | یکی در T1 رد می‌شود (`username_claim`)؛ صفر فراخوانی remote برای بازنده |

### all-or-nothing و مرز تراکنش

| کد | سناریو | Invariant |
|---|---|---|
| LP-6 | خرید پکیج سه‌اتصالی، شکست قطعی اتصال دوم | اتصال اول حذف remote؛ هیچ `Purchase`/`Connection`؛ reservationها `released`؛ `compensated` (یافته‌های A، C) |
| LP-7 | شکست T1 در هر نقطه | هیچ ردیف، هیچ تغییر موجودی، صفر remote |
| LP-8 | شکست T_final (خطای DB تزریق‌شده) | rollback؛ `remote_complete`؛ retry موفق؛ یک `Purchase`، بدون تکرار |
| LP-9 | قبل از T_final هیچ `User`/`Purchase`/`Connection` وجود ندارد | poller، RADIUS و بات چیزی نمی‌بینند |
| LP-10 | T_final موفق | همه‌ی ردیف‌ها، capture هر دو reservation و LedgerEntry فروش در یک commit |
| LP-11 | AST: `db.commit()` یا فراخوانی client remote داخل سازنده‌های T_final | صفر (نسخه‌ی بسته‌ی گراف فراخوانی: LP-95) |
| LP-12 | ساخت گروهی ۵ User، شکست سومی | ۴ کامل، یکی بدون هیچ اثر؛ بدون refund batch (یافته‌ی H) |
| LP-13 | تمدید: یک تراکنش | reservation مستقیم `captured`؛ شکست → هیچ اثر |

### Reservation

| کد | سناریو | Invariant |
|---|---|---|
| LP-14 | reserve اعتبار reseller ناکافی | رد در T1؛ صفر remote؛ هیچ ردیف Ledger (یافته‌های E، G) |
| LP-15 | reserve → capture | دقیقاً یک `admin_credit_spend` |
| LP-16 | reserve → release | موجودی برابر قبل؛ صفر ردیف Ledger |
| LP-17 | capture و release هم‌زمان | فقط یکی اثر می‌کند |
| LP-18 | کیف‌پول `legacy_balance`: reserve، capture، release | موجودی هرگز منفی؛ capture فقط با LedgerEntry فروش |
| LP-19 | کیف‌پول `wallet_lot`: همان با `hold_ref` | `wallet_debit_events` و reservation هم‌state |
| LP-20 | cutover کیف‌پول با reservation `reserved` | rollback cutover |
| LP-21 | remote ساخته شد، T_final شکست قطعی | حذف remote با همان هویت، سپس release؛ اگر حذف remote شکست بخورد پول `reserved` می‌ماند |
| LP-22 | `debit_admin` در مسیر durable | صدا زده نمی‌شود؛ هیچ commit در شکست |

### Recovery

| کد | سناریو | Invariant |
|---|---|---|
| LP-23 | crash در `remote_calling`، منبع ساخته شده | `read` → `remote_created`؛ منبع دوم ساخته نمی‌شود |
| LP-24 | crash در `remote_calling`، منبع ساخته نشده | `ensure_present` با همان credential staged |
| LP-25 | crash بعد از هر commit در ۷.۱ تا ۷.۳ (تزریق در هر نقطه) | جدول ۱۱.۶ |
| LP-26 | `forward_deadline` گذشته | compensation |
| LP-27 | `present_conflict` | `cleanup_required`؛ منبع remote دست‌نخورده؛ پول `reserved` |
| LP-28 | `unreadable` تا سقف تلاش | `cleanup_required` |
| LP-29 | force یک گام | `abandoned`؛ orphan ثبت؛ T_comp؛ release |
| LP-30 | credential بعد از ترمینال | هر سه ستون `staged_*` NULL؛ CHECK |
| LP-31 | جست‌وجوی مقادیر credential تست در `intent`، `error_sanitized`، پاسخ‌ها و log | صفر |

### قفل و هم‌زمانی

| کد | سناریو | Invariant |
|---|---|---|
| LP-32 | درخواست sync و worker روی یک عملیات | یکی lease دارد؛ دیگری کنار می‌رود |
| LP-33 | executor با lease منقضی نتیجه می‌نویسد | rollback نوشتن DB در revalidation (serialization remote: LP-98 تا LP-101) |
| LP-34 | دو عملیات WireGuard هم‌زمان روی یک node | IPهای متفاوت |
| LP-35 | دو عملیات هم‌زمان روی یک Xray SSH node | read-modify-write سریال با gate node؛ هر دو client در config (با توقف طولانی executor: LP-100) |
| LP-36 | بزرگ‌شدن subnet: (الف) داخل T1 که rollback می‌شود؛ (ب) T1 commit شد و عملیات بعداً compensate شد؛ (ج) T1 commit شد و هیچ گامی هنوز router را ندیده | (الف) `mt_client_subnet` بدون تغییر. (ب) و (ج) `mt_client_subnet` بزرگ می‌ماند، هرگز کوچک نمی‌شود، و router با اولین نگه‌داشت gate همان node به آن می‌رسد: `ensure_present` بعدی یا reconciliation (جزئیات: LP-79 تا LP-83، LP-102) |
| LP-37 | ترتیب acquire در عملیات چندمنبعی | بدون deadlock |

### Adapterها

| کد | سناریو | Invariant |
|---|---|---|
| LP-38 | WireGuard `ensure_present` دو بار | یک peer |
| LP-39 | WireGuard: comment برابر، کلید متفاوت | `present_conflict`؛ بدون حذف |
| LP-40 | WireGuard `ensure_absent`: peer موجود؛ ناموجود | `verified_absent` در هر دو؛ queue حذف |
| LP-41 | Xray SSH: uuid staged به `add_client` داده می‌شود | uuid config = uuid گام |
| LP-42 | Xray SSH: retry `ensure_present` روی `present_match` | بدون نوشتن config، بدون restart |
| LP-43 | Xray: email برابر، uuid متفاوت | `present_conflict` |
| LP-44 | 3X-UI: `add` موفق ولی `read` بعدی client را نمی‌بیند | خطای قابل‌retry، نه موفقیت |
| LP-45 | 3X-UI `ensure_absent`: `remove` موفق اعلام شد ولی client هنوز هست | `unverified` |
| LP-46 | SoftEther `user_exists` برای کاربر بدون ترافیک | True |
| LP-47 | SoftEther: retry بعد از timeout مبهم `CreateUser` | `SetUser` با رمز staged؛ یک کاربر |
| LP-48 | SoftEther `ensure_absent` با شکست `EnumUser` | `delete_idempotently_absent` **فقط** اگر `DeleteUser` پاسخ صریح و شناخته‌شده‌ی `not_exist` داد؛ `DeleteUser` بدون خطا → `unverified` (جزئیات: LP-84 تا LP-88) |
| LP-49 | `radius_ppp` | صفر فراخوانی remote؛ `staged → active` |

### One-time و حذف

| کد | سناریو | Invariant |
|---|---|---|
| LP-50 | دو خرید هم‌زمان پکیج یک‌بارمصرف، یک User (دو دیالکت) | یکی در T1 رد (F-race) |
| LP-51 | دو account با یک Telegram | یکی رد (F-race2) |
| LP-52 | خرید compensate شد | claim آزاد؛ خرید بعدی مجاز |
| LP-53 | خرید در `cleanup_required` | claim فعال می‌ماند |
| LP-54 | backfill claim دو بار | بدون ردیف تکراری |
| LP-55 | `delete_user` با سه اتصال، شکست دومی | `cleanup_required`؛ حساب مسدود؛ ردیف‌های DB باقی؛ retry ادامه می‌دهد (یافته‌ی B) |
| LP-56 | هر اتصال در حذف | دقیقاً یک `ensure_absent` موفق |
| LP-57 | حذف WireGuard با peer ناهم‌خوان | `unverified`، نه حذف کور و نه warning بی‌صدا |

### API، HA، rollout

| کد | سناریو | Invariant |
|---|---|---|
| LP-58 | caller قدیمی، عملیات موفق | بدنه‌ی پاسخ بایت‌به‌بایت مثل امروز |
| LP-59 | بودجه‌ی زمانی تمام شد | ۴۰۹ `operation_in_progress`؛ هرگز ۲xx |
| LP-60 | retry بعد از `completed` | همان پاسخ کامل |
| LP-61 | وضعیت عملیات tenant دیگر | ۴۰۴ |
| LP-62 | `durable` با HA روشن؛ روشن‌کردن HA با نوع `durable` | هر دو رد |
| LP-63 | با HA روشن | worker ثبت نمی‌شود |
| LP-64 | بازگشت به `legacy` با عملیات غیرترمینال | رد |
| LP-65 | تغییر mode هم‌زمان با درخواست در جریان | منتظر می‌ماند؛ درخواست کاملاً در یک mode |
| LP-66 | mode `legacy` | رفتار دقیقاً مثل امروز؛ هیچ ردیف عملیات |
| LP-67 | migration روی دیتابیس قدیمی، دو دیالکت | جدول‌ها و UNIQUEها با inspector تأیید |
| LP-68 | INSERT مستقیم ناسازگار با هر CHECK بخش ۵، دو دیالکت | رد |
| LP-69 | خرید کیف‌پولی از مینی‌اپ و بات مشتری بعد از P6 | هیچ `add_balance(-price)` مستقل؛ شکست → release قطعی |
| LP-70 | AST: `db.delete` یا bulk delete روی جدول‌های این سند | صفر |

### اتصال به ReceiptApproval

| کد | سناریو | Invariant |
|---|---|---|
| LP-71 | crash تزریق‌شده بین validation approval (بعد از `registered → mutating` در تراکنش) و commit T1 | approval `registered`؛ هیچ ردیف عملیات، گام، claim یا reservation؛ موجودی بدون تغییر؛ retry همان token موفق |
| LP-72 | retry با همان execution token، بعد از commit T1 و بعد از `completed` | همان عملیات؛ بدون reserve دوم؛ بدون گام تازه |
| LP-73 | عملیات `compensated` و approval `failed`؛ retry کنترل‌شده‌ی approval (`version + 1`)؛ T1 با token تازه | عملیات تازه با `business_key` نسخه‌ی جدید و credential تازه؛ عملیات قدیمی بدون تغییر؛ token قدیمی ۴۰۹ `approval_superseded` |
| LP-74 | INSERT مستقیم دو عملیات با همان `(approval_uuid, approval_execution_version)` و `operation_type` متفاوت؛ و ردیف با فقط یکی از دو ستون (SQLite و MariaDB) | اولی با UNIQUE رد؛ دومی با CHECK رد؛ دو عملیات بدون approval (هر دو NULL) پذیرفته |
| LP-75 | شکست تزریق‌شده در هر نقطه‌ی T_final (بعد از هر `record_effect`، بعد از capture، قبل از COMMIT) | هیچ `User`/`Purchase`/`Connection`، هیچ Ledger، هیچ effect؛ reservation `reserved`؛ approval `mutating`؛ retry → همه با هم و approval `completed` |
| LP-76 | شکست تزریق‌شده در T_comp (بعد از release، قبل از COMMIT)؛ و T_comp موفق | شکست: reservation `reserved`، عملیات `compensating`، approval `mutating`. موفق: `released`، `compensated`، approval `failed` بدون effect، `error_code` ثابت |
| LP-77 | (الف) takeover قبل از T1، سپس T1 با token قدیمی؛ (ب) revoke کلید قبل از T1؛ (ج) takeover بعد از T1؛ (د) revoke کلید بعد از T1 و قبل از T_final؛ (ه) همان قبل از T_comp؛ (و) takeover و T1 هم‌زمان با دو Session (دو دیالکت) | (الف) ۴۰۹، هیچ عملیات. (ب) ۴۰۳، approval `registered`. (ج) ۴۰۹، عملیات ادامه می‌یابد. (د) و (ه) عملیات `cleanup_required` با `execution_key_revoked`، یک `approval_recovery`، پول `reserved`، approval `mutating`، صفر فراخوانی remote تازه. (و) دقیقاً یکی commit می‌کند |
| LP-78 | (الف) approval `mutating` با عملیات `prepared`/`provisioning`/`remote_complete` متناظر، بعد از restart؛ (ب) fixture ناسازگار: approval `mutating` بدون هیچ عملیات؛ (ج) fixture: عملیات `completed` با approval `mutating` | (الف) worker عادی ادامه می‌دهد تا `completed` یا `failed`. (ب) و (ج) فقط یک `approval_recovery` در `cleanup_required`؛ هیچ تغییر خودکار state؛ تکرار چرخه ردیف دوم نمی‌سازد |

### WireGuard: subnet و آدرس interface

| کد | سناریو | Invariant |
|---|---|---|
| LP-79 | عملیات A در T1 subnet را `/24 → /23` می‌کند و commit؛ crash قبل از هر تماس router؛ A compensate می‌شود؛ عملیات B (با `wg_subnet_expanded = 0`) | DB `/23` می‌ماند؛ `ensure_present` B قبل از `add_peer` آدرس router را `/23` می‌کند؛ peer B با IP خارج از `/24` قدیمی ساخته می‌شود |
| LP-80 | جایگزینی آدرس interface شکست می‌خورد: (الف) قبل از حذف؛ (ب) بعد از حذف و قبل از افزودن | گام خطای قابل‌retry؛ هیچ peer ساخته نشده؛ retry بعدی در (الف) جایگزین و در (ب) از شاخه‌ی «بدون آدرس» اضافه می‌کند؛ نتیجه `/23` |
| LP-81 | گام قدیمی با snapshot `/24` بعد از این‌که DB و router `/23` شده‌اند اجرا می‌شود | هیچ نوشتن آدرس روی router؛ router `/23` می‌ماند |
| LP-82 | دو عملیات هم‌زمان وقتی فقط یک IP آزاد مانده (دو Session، SQLite و MariaDB) | دقیقاً یک بزرگ‌شدن subnet؛ دو IP متفاوت؛ هر دو داخل subnet نهایی DB؛ router یک‌بار به مقدار نهایی می‌رسد |
| LP-83 | `ensure_absent` یک peer (موجود و ناموجود) | صفر فراخوانی نوشتن یا حذف روی `/ip/address` |

### SoftEther: قرارداد حذف

| کد | سناریو | Invariant |
|---|---|---|
| LP-84 | `DeleteUser` موفق؛ `EnumUser` timeout | `unverified`؛ گام `cleanup_required` |
| LP-85 | `DeleteUser` موفق؛ `EnumUser` پاسخ HTML، ۴۰۳، یا JSON بدون `UserList` (هرکدام جدا) | `unverified` |
| LP-86 | `EnumUser` موفق و username غایب (بعد از `DeleteUser` موفق؛ و وقتی کاربر از اول نبوده) | `verified_absent`؛ گام `removed` |
| LP-87 | `DeleteUser` پاسخ JSON-RPC با کد `not_exist` ثبت‌شده در فهرست؛ `EnumUser` ناموفق. و همان با فهرست خالی | با کد در فهرست: `delete_idempotently_absent`. با فهرست خالی: `unverified` |
| LP-88 | `DeleteUser` پاسخ مبهم (timeout، خطای JSON-RPC با کد ناشناخته، متن حاوی «not exist» ولی کد ناشناخته)؛ `EnumUser` ناموفق | `unverified`؛ `cleanup_required`؛ reservation `reserved`؛ finalizer DB اجرا نشده؛ ردیف‌های `Connection` باقی |
| LP-89 | force روی گام LP-88: بدون superadmin؛ رمز غلط؛ بدون دلیل؛ و معتبر | سه مورد اول رد، بدون تغییر. معتبر: `abandoned`، `forced_by_admin_id`/`force_reason`/`forced_at` پر، گام در فهرست orphan؛ سپس T_comp و release (برای حذف: finalizer) |

### claim_key

| کد | سناریو | Invariant |
|---|---|---|
| LP-90 | `tenant_scope_key` با ۶۴ کاراکتر و `telegram_id` با بیشینه‌ی BIGINT (SQLite و MariaDB) | `claim_key` دقیقاً ۶۴ کاراکتر؛ مقدار ذخیره‌شده برابر مقدار محاسبه‌شده (بدون truncate)؛ `tenant_scope_key` با ۶۵ کاراکتر ۴۲۲ |
| LP-91 | دو tuple متفاوت که بدون پیشوند طول الحاق یکسانی می‌داشتند (tenant `a|1` با telegram `2`، در برابر tenant `a` با telegram `12`)؛ user و telegram با عدد یکسان؛ و همان tuple دو بار (دو دیالکت) | tupleهای متفاوت `claim_key` متفاوت و هر دو پذیرفته؛ tuple تکراری با UNIQUE واقعی DB رد |

### T_final بدون commit داخلی

| کد | سناریو | Invariant |
|---|---|---|
| LP-92 | شکست تزریق‌شده بعد از mutation referral (موجودی و quota هر دو طرف نوشته شده) و قبل از پایان T_final | هیچ `User`/`Purchase`/`Connection`؛ `referred_by_id`، موجودی و quota معرف بدون تغییر؛ هیچ effect؛ reservation `reserved` |
| LP-93 | شکست تزریق‌شده بعد از redemption تخفیف | `used_count` برابر قبل؛ هیچ `DiscountCodeRedemption`؛ هیچ ردیف فروش |
| LP-94 | شکست تزریق‌شده بعد از افزایش `accumulated_amount` و تعویض کارت فعال (mode `threshold`، سقف رسیده) | `accumulated_amount`، pointer کارت فعال و (در `event_logged`) eventها برابر قبل؛ هیچ ردیف فروش |
| LP-95 | گراف فراخوانی بسته از ورودی‌های T1، T_final، T_comp و تمدید تک‌تراکنشی، شامل سه core جدید و همه‌ی helperهای transitively-called؛ و اجرای واقعی با شمارنده‌ی commit و guard سوکت | صفر `commit`/`rollback`/`close`/ساخت client remote/import شبکه در هر تابع قابل‌دسترس؛ فراخوانی resolve‌نشده خطاست؛ در اجرا دقیقاً یک commit و صفر اتصال |
| LP-96 | `apply_referral_code`، `redeem_discount_code`، `advance_after_payment` و بقیه‌ی wrapperهای جدول ۷.۳ از callerهای امروز | هرکدام دقیقاً یک commit؛ نتیجه و مقدار بازگشتی مثل امروز؛ دو redemption هم‌زمان روی کدی با یک ظرفیت باقی‌مانده: فقط یکی موفق |
| LP-97 | retry T_final بعد از شکست؛ و retry بعد از موفقیت؛ و فراخوانی جدای بات بعد از آن | یک پاداش referral، یک redemption، یک event کارت؛ retry بعد از موفقیت بدون هیچ نوشتن؛ فراخوانی جدا از `optional_effects` پاسخ می‌گیرد |

### gate مالکیت node و reconciliation

هر تست این بخش دو نسخه‌ی جدا دارد: SQLite (`flock`) و MariaDB (`flock+get_lock`).

| کد | سناریو | Invariant |
|---|---|---|
| LP-98 | دو process مستقل؛ executor اول gate را دارد و بیش از TTL lease `provisioning_op` متوقف می‌شود؛ executor دوم تلاش می‌کند | executor دوم تا آزادشدن gate هیچ فراخوانی نویسنده‌ی remote نمی‌فرستد (client mock مشترک هیچ هم‌پوشانی زمانی ثبت نمی‌کند)؛ مهلت → `node_gate_timeout` قابل‌retry |
| LP-99 | process دارنده‌ی gate با SIGKILL می‌میرد | gate بدون هیچ force دستی آزاد؛ worker گام را با `read` ادامه می‌دهد |
| LP-100 | دو عملیات متفاوت Xray SSH روی یک node، اولی بین خواندن و نوشتن config متوقف می‌شود؛ و همان با یک عملیات durable و یک enforcement poller | config نهایی هر دو client را دارد؛ هیچ lost update؛ AST: هیچ متد نویسنده بیرون از `node_gate` |
| LP-101 | executor قدیمی WireGuard با `desired = /24` متوقف؛ DB به `/23` می‌رود؛ executor دوم | executor دوم پشت gate می‌ماند؛ بعد از آزادی router را `/23` می‌کند؛ در هیچ لحظه‌ای router از `/23` به `/24` برنمی‌گردد |
| LP-102 | drift تاریخی (DB `/23`، router `/24`) و هیچ عملیات بعدی؛ و router بدون آدرس؛ و router پهن‌تر؛ و conflict | reconciliation startup و دوره‌ای: دو مورد اول `repaired`؛ سومی `router_wider` بدون تغییر؛ چهارمی `conflict` بدون تغییر؛ ردیف `provisioning_node_reconciliations` به‌روز |
| LP-103 | تغییر نوع به `durable` با: `lock_backend='none'`؛ `lock_dir` روی فایل‌سیستم شبکه‌ای؛ خودآزمایی `flock` ناموفق؛ هویت host ناهم‌خوان با مالک؛ node فعال بدون ردیف `ready` معتبر در `provisioning_node_contracts` | همه رد؛ هیچ ردیف mode عوض نمی‌شود؛ process روی host غیرمالک هیچ mutation remote نمی‌فرستد (جزئیات مالکیت: LP-126 تا LP-132) |

### T_final در shadow

| کد | سناریو | Invariant |
|---|---|---|
| LP-104 | approval `required`؛ `record_effect` با projection ناهم‌خوان؛ و manifest با required گم‌شده | ROLLBACK کامل؛ عملیات `remote_complete`؛ approval `mutating`؛ reservation `reserved`؛ هیچ ردیف نهایی |
| LP-105 | approval `shadow`؛ همان دو خطا | فروش، `Connection`ها و capture commit؛ عملیات `completed`؛ approval `mutating`؛ ردیف `receipt_approval_shadow_events`؛ یک `approval_recovery` در `cleanup_required`؛ reservation `captured` |
| LP-106 | crash تزریق‌شده در هر نقطه‌ی T_final shadow (قبل و بعد از INSERT recovery) | هرگز «عملیات `completed` + approval `mutating` + بدون recovery»؛ یا هیچ‌کدام یا همه |
| LP-107 | retry همان درخواست shadow بعد از LP-105؛ و فراخوانی جدای `redeem_discount`/`record_card_payment` | یک `Purchase`، یک capture، یک LedgerEntry فروش؛ optional با effect ناموفق دوباره اجرا نمی‌شود؛ پاسخ از `optional_effects` |
| LP-108 | `p6_ready()` با: approval `shadow` در `mutating` با عملیات `completed`؛ `approval_recovery` باز برای approval `shadow`؛ عملیات غیرترمینال با approval `shadow` | هر سه false و فهرست موارد؛ بعد از بستن همه با recovery: true |

### credential در compensation

| کد | سناریو | Invariant |
|---|---|---|
| LP-109 | چهار مسیر به `removed`: `remote_attempted = 0`؛ `verified_absent`؛ `delete_idempotently_absent`؛ force/`abandoned`. و ورود به `compensating` و به `cleanup_required` | در همان commit که state عوض می‌شود: برای `removed` هر سه `staged_*` NULL؛ برای `compensating`/`cleanup_required` کلید خصوصی و رمز NULL. INSERT/UPDATE مستقیم مغایر با CHECK رد (دو دیالکت) |
| LP-110 | عملیات در `cleanup_required` (هر دو علت): پاسخ وضعیت عملیات، فهرست orphan، پاسخ recovery، `error_sanitized`، و همه‌ی logهای ضبط‌شده | هیچ‌کدام مقدار هیچ `staged_*` تست را ندارد؛ serializer ستون‌های `staged_*` را در allowlist ندارد (AST)؛ بعد از آستانه‌ی دوم retention «ادامه» رد می‌شود |

### چهار panel دیگر Xray

هر تست با client mock در سطح HTTP (بدون شبکه) و پاسخ‌های ثبت‌شده‌ی هر panel.

| کد | سناریو | Invariant |
|---|---|---|
| LP-111 | Marzban: `ensure_present` روی `absent`؛ retry بعد از timeout مبهم `POST` (کاربر ساخته شده) | یک `POST`؛ retry با `read` → `present_match` بدون `POST` دوم؛ **صفر `PUT`** |
| LP-112 | Hiddify: همان | یک `POST`؛ retry بدون `POST` و بدون `PATCH` body کامل |
| LP-113 | Marzneshin: همان | یک `POST`؛ صفر `PUT`؛ `account_username` برابر مقدار T1 |
| LP-114 | S-UI: همان | یک `save new`؛ retry بدون `edit` |
| LP-115 | Marzban: username برابر با uuid متفاوت؛ و با inbound tag متفاوت | `present_conflict`؛ صفر `POST`/`PUT`/`DELETE`؛ کاربر panel بدون تغییر؛ گام `cleanup_required` |
| LP-116 | Hiddify: name برابر با uuid متفاوت؛ uuid برابر با name متفاوت؛ دو کاربر با همان name | هر سه `present_conflict` با کد جدا؛ صفر `POST`/`PATCH`/`DELETE` |
| LP-117 | Marzneshin: username برابر با `key` متفاوت؛ با service متفاوت؛ `note` برابر زیر username دیگر | هر سه `present_conflict`؛ صفر `POST`/`PUT`/`DELETE` |
| LP-118 | S-UI: name برابر با inbound متفاوت؛ `ambiguous` | `present_conflict`؛ صفر `save` |
| LP-119 | Marzban `ensure_absent`: موجود → حذف → `read` غایب؛ `DELETE` پاسخ مبهم (۵۰۰، HTML، ۴۰۴ بدون بدنه‌ی قراردادی) و `read` ناموفق؛ سپس retry موفق | `verified_absent`؛ `unverified` و `cleanup_required`؛ retry → `verified_absent` |
| LP-120 | Hiddify `ensure_absent`: همان سه حالت؛ و فهرست با ۴۰۴ بدون بدنه‌ی قراردادی | مثل LP-119؛ ۴۰۴ ناشناخته `unreadable` است، نه «فهرست خالی» |
| LP-121 | Marzneshin `ensure_absent`: همان سه حالت؛ و صفحه‌ی دوم فهرست غیر-۲۰۰ | مثل LP-119؛ فهرست ناقص `unreadable` است، نه `absent` |
| LP-122 | S-UI `ensure_absent`: همان سه حالت؛ `del` فقط با `id` همان `present_match` | مثل LP-119؛ `id` از `read` همان نگه‌داشت gate |
| LP-123 | Marzneshin: دو `xr_email` که `sanitize_username` یکسان می‌دهند (یکی `Connection` موجود همان node، یکی گام تازه)؛ و کاربری با همان نام sanitize‌شده که مستقیم روی panel ساخته شده (`note` متفاوت) | اولی: T1 hex تازه می‌سازد و نام‌ها متفاوت می‌شوند. دومی: `present_conflict`؛ کاربر panel overwrite نمی‌شود؛ صفر `PUT` و صفر `DELETE` |
| LP-124 | S-UI: name یکسان با uuid متفاوت؛ و name و uuid یکسان با inbound متفاوت | هر دو `present_conflict`؛ صفر `edit`/`del` |
| LP-125 | node فعال از هر شش panel mode Xray (`ssh`، `3xui`، `marzban`، `hiddify`، `marzneshin`، `sui`)، هرکدام با ردیف `provisioning_node_contracts` در `ready`؛ و همان با یکی `unverified` | اولی: `p6_ready()` true (بقیه‌ی شرط‌ها برقرار)؛ `backend` هر گام درست. دومی: false با نام node ناآماده؛ T1 روی آن node ۴۰۹ `node_contract_not_ready` (آمادگی per-node: LP-170 تا LP-176) |

### مالکیت host

تست‌های دو-host با دو هویت host ساختگی روی یک دیتابیس (MariaDB واقعی؛ و SQLite برای حالت‌های تک‌دیتابیسی).

| کد | سناریو | Invariant |
|---|---|---|
| LP-126 | verify هم‌زمان Host A و Host B روی ردیف بدون مالک | دقیقاً یک مالک؛ بازنده ۴۰۹ `ownership_held`؛ یک event `claim` |
| LP-127 | A مالک است؛ verify عادی B (با heartbeat تازه‌ی A و با heartbeat کهنه‌ی A) | هر دو رد؛ هیچ ستونی عوض نمی‌شود؛ `ownership_epoch` ثابت (همان `host_id` با boot متفاوت: LP-151) |
| LP-128 | فایل هویت: نبود؛ خالی؛ mode بازتر از `0600`؛ و `host_id` کپی‌شده با `boot_id` متفاوت، با heartbeat تازه و با heartbeat کهنه‌ی اصلی | همه fail-closed: verify رد؛ هیچ gate `enforced`؛ هیچ mutation remote |
| LP-129 | `release` با: یک عملیات غیرترمینال؛ یک نگه‌دارنده‌ی gate؛ بدون `drain` قبلی | هر سه ۴۰۹ با فهرست؛ مالک عوض نمی‌شود |
| LP-130 | handoff کامل A → B: drain، انتظار، release، verify روی B | در drain: T1 تازه ۵۰۳ و gate تازه رد. بعد از release: A هیچ فراخوانی نویسنده نمی‌فرستد (guard)؛ B مجاز؛ eventهای `drain_start`، `release`، `claim` به ترتیب |
| LP-131 | forced takeover: بدون superadmin؛ رمز غلط؛ بدون دلیل؛ بدون عبارت تأیید؛ از همان `host_id` مالک؛ و معتبر (با heartbeat تازه‌ی مالک قبلی: هشدار و تأیید دوم) | پنج مورد اول رد بدون تغییر. معتبر: مالک = B؛ `ownership_epoch + 1`؛ ردیف `forced_takeover` با دلیل و actor |
| LP-132 | process قدیمی A بعد از takeover، در سه نقطه: قبل از gate acquisition؛ بعد از `flock` و قبل از بررسی دوم؛ داخل gate و قبل از فراخوانی نویسنده | در هر سه هیچ فراخوانی نویسنده فرستاده نمی‌شود؛ `ownership_lock_host_mismatch` |

### mode gate

| کد | سناریو | Invariant |
|---|---|---|
| LP-133 | deploy با `gate_mode = 'off'`: همه‌ی مسیرهای legacy inventory ۱۰.۲ و poller، با contention ساختگی | دقیقاً یک `LOCK_SH` gate-mode برای هر مسیر remote؛ صفر قفل node و صفر `GET_LOCK`؛ صفر serialization؛ صفر خطای قابل‌مشاهده؛ تعداد و ترتیب فراخوانی remote برابر baseline (هم‌زمانی دو نویسنده: LP-168) |
| LP-134 | `shadow`: دو نویسنده‌ی legacy هم‌زمان روی یک node؛ و یک نویسنده که از `node_gate` نگذشته | هر سه بدون انتظار کامل می‌شوند؛ هیچ درخواست رد نمی‌شود؛ شمارنده‌ها طبق LP-177 و LP-178 |
| LP-135 | تغییر نوع به `durable` با `gate_mode` برابر `off` و `shadow` | هر دو ۴۰۹ `gate_not_enforced`؛ T1 durable هم رد |
| LP-136 | `shadow → enforced` وقتی یک نویسنده‌ی shadow وسط mutation است | انتقال تا پایان آن نویسنده منتظر می‌ماند؛ هیچ نویسنده‌ی enforced هم‌پوشان (hang و timeout: LP-165، LP-169) |
| LP-137 | `enforced → shadow` با: یک نوع `durable`؛ یک عملیات غیرترمینال؛ یک نگه‌دارنده‌ی فعال | هر سه ۴۰۹؛ mode ثابت |
| LP-138 | `enforced`: یک عملیات durable، یک مسیر legacy و poller هم‌زمان روی یک node (دو دیالکت) | هیچ هم‌پوشانی زمانی فراخوانی نویسنده؛ هر سه از همان فایل و همان نام قفل |

### guard زمان اجرا

| کد | سناریو | Invariant |
|---|---|---|
| LP-139 | هر متد نویسنده‌ی هر نُه backend، مستقیم و بدون `node_gate`، در `enforced` | `GateViolation` قبل از هر اتصال (guard سوکت صفر اتصال می‌بیند) |
| LP-140 | داخل `node_gate(A)`، نویسنده روی client ساخته‌شده برای node B؛ و client با `bound_node_id = None` | هر دو رد با `gate_token_node_mismatch`؛ صفر اتصال |
| LP-141 | نویسنده‌ی nested (`set_client_enabled → add_client`؛ `ensure_present` WireGuard با queue)؛ `node_gate` تودرتو برای همان node و برای node دیگر؛ نویسنده در thread تازه | nested و reentrant همان token؛ node دیگر رد؛ thread تازه رد |
| LP-142 | خواندن‌های هر backend بدون gate، از جمله `POST`های allowlist (ورود، onlines) و متدهای خواندن SoftEther | همه مجاز |
| LP-143 | fixture: یک متد نویسنده‌ی تازه روی یک client موجود که در فهرست AST نیست و مستقیم `session.request("POST", ...)`، `_call` یا `path.add` می‌زند | AST آن را نمی‌بیند؛ guard زمان اجرا آن را قبل از سوکت متوقف می‌کند |

### runner و watchdog

| کد | سناریو | Invariant |
|---|---|---|
| LP-144 | فراخوانی remote عمداً hang می‌شود؛ نویسنده‌ی دوم روی همان node | نویسنده‌ی دوم تا مرگ runner اول وارد نمی‌شود؛ هیچ هم‌پوشانی |
| LP-145 | hang بیش از `hard_deadline` | والد runner را `SIGKILL` و مرگ را تأیید می‌کند؛ kernel gate را آزاد می‌کند؛ گام با `read` ادامه می‌یابد؛ `watchdog_kill_count + 1`؛ process اصلی زنده؛ log بدون secret |
| LP-146 | تلاش آزادسازی gate در حالی که runner زنده است: endpoint یا تابعی برای آزادسازی دستی؛ گذشتن زمان؛ `RELEASE_LOCK` از اتصال دیگر | هیچ مسیر آزادسازی دستی وجود ندارد (AST)؛ زمان آن را آزاد نمی‌کند؛ `RELEASE_LOCK` از اتصال غیرمالک بی‌اثر |

### namespace نصب

| کد | سناریو | Invariant |
|---|---|---|
| LP-147 | دو `installation_uuid` متفاوت، `node_id` یکسان، یک سرور MariaDB و یک `lock_dir` | هر دو هم‌زمان gate می‌گیرند |
| LP-148 | دو process همان نصب و همان node (دو دیالکت) | سریال |
| LP-149 | نام قفل برای `node_id` کوچک و بیشینه | همیشه ۶۰ کاراکتر، فقط `[a-z0-9:]`، زیر سقف `GET_LOCK`؛ دو `(installation_uuid, node_id)` متفاوت نام متفاوت، از جمله جفت‌هایی که الحاق بدون جداکننده‌شان یکسان می‌شد |

### مالکیت host: boot و reclaim

| کد | سناریو | Invariant |
|---|---|---|
| LP-150 | مالک `(H, B1)`؛ همان host و همان boot: heartbeat، verify دوباره، restart process و restart container | همه پذیرفته؛ `ownership_epoch` ثابت؛ process تازه بدون claim کار می‌کند |
| LP-151 | مالک `(H, B1)`؛ verify عادی از `(H, B2)` با heartbeat تازه، کهنه‌ی ۱ ساعته و کهنه‌ی ۳۰ روزه | هر سه رد (۴۰۹ `ownership_held`)؛ هیچ ستونی عوض نمی‌شود؛ هیچ مسیر خودکار دیگری هم claim نمی‌کند |
| LP-152 | shutdown تمیز `(H, B1)` با runner زنده و بدون runner؛ سپس verify از `(H, B2)` | با runner زنده: release فقط بعد از تأیید مرگ runner. سپس `released`، event `shutdown_release`؛ boot تازه با verify عادی claim می‌کند؛ epoch دو بار زیاد شده |
| LP-153 | reclaim صریح از `(H, B2)`: بدون superadmin؛ رمز غلط؛ بدون دلیل؛ بدون عبارت تأیید؛ از `host_id` دیگر؛ و معتبر | پنج مورد اول رد بدون تغییر. معتبر: `owner_boot_id = B2`؛ `ownership_epoch + 1`؛ ردیف `reclaim` با actor، دلیل، boot قبلی و جدید |
| LP-154 | بعد از reclaim، process قدیمی `(H, B1)` (شبیه‌سازی‌شده با همان دیتابیس): gate acquisition تازه؛ و نویسنده داخل gate قبل از فراخوانی | هر دو در اولین revalidation با `ownership_lock_host_mismatch` متوقف؛ صفر فراخوانی نویسنده |
| LP-155 | clone با host secret کپی‌شده (`host_id` برابر، `boot_id` متفاوت): verify عادی در حالی که اصلی فعال است؛ اصلی freeze شده؛ اصلی خاموش است | هر سه رد؛ مالکیت فقط با reclaim صریح و تأیید انسانی منتقل می‌شود؛ هیچ ادعای تشخیص clone از reboot |

### قرارداد runner و IPC

| کد | سناریو | Invariant |
|---|---|---|
| LP-156 | ساخت `RemoteActionDTO` با `Session`، شیء ORM، `Query`، callable، closure، یا شیء client در هر عمق | `TypeError` در والد، قبل از spawn |
| LP-157 | اجرای هر `action_type` در runner با hook رویداد نوشتن روی اتصال DB فرزند و با دیتابیس فقط-خواندنی | صفر statement نویسنده؛ هر تلاش نوشتن خطای DB؛ AST: فرزند `SessionLocal` و modelها را import نمی‌کند |
| LP-158 | یک گام کامل (tx الف → runner → tx ب) با ثبت اتصال هر statement نویسنده | هر دو تراکنش فقط از process والد؛ فرزند هیچ‌کدام |
| LP-159 | هر `action_type` با secret نشانه‌دار: `/proc/{pid}/cmdline` و `/proc/{pid}/environ` فرزند، همه‌ی logها، `repr`/`str` DTO و result، پیام استثناها | secret در هیچ‌کدام نیست؛ `redacted()` فقط `"<redacted>"` دارد |
| LP-160 | والد بلافاصله بعد از spawn و قبل از نصب `PR_SET_PDEATHSIG` فرزند کشته می‌شود | فرزند با `getppid()` ناهم‌خوان خارج می‌شود؛ صفر gate، صفر اتصال شبکه |
| LP-161 | فرزند بعد از فرستادن فراخوانی نویسنده و قبل از نوشتن result کشته می‌شود؛ و pipe وسط result قطع می‌شود | والد `killed_unknown` ثبت می‌کند؛ گام در `remote_calling` می‌ماند؛ نه موفق و نه ناموفق؛ recovery با `read` تعیین می‌کند |
| LP-162 | `schema_version` DTO با فرزند ناسازگار؛ result با `schema_version` یا `action_id` ناسازگار؛ `action_type` ثبت‌نشده؛ `outcome` خارج از فهرست | فرزند قبل از gate خارج می‌شود؛ والد result ناسازگار را `killed_unknown` حساب می‌کند؛ هیچ‌کدام موفق فرض نمی‌شود |
| LP-163 | تطبیق جدول ۱۰.۹ با کد: هر فراخوانی متد نویسنده در `backend/app`؛ هر `action_type` ثبت‌شده | هر نویسنده داخل یک action ثبت‌شده؛ هر action در جدول؛ سه متد بدون caller هیچ actionی ندارند و در `enforced` رد می‌شوند |
| LP-164 | بازرسی بایت‌های عبوری از pipe در هر دو جهت برای همه‌ی actionها | فقط JSON از نوع‌های ساده؛ بدون pickle؛ بدون ارجاع به شیء Session/ORM/callback |

### تغییر mode gate

| کد | سناریو | Invariant |
|---|---|---|
| LP-165 | نویسنده‌ای که در `off` شروع شده و بیش از `2 × hard_deadline` زنده می‌ماند؛ `off → shadow` و سپس `shadow → enforced` | تا خروج آن نویسنده هر تغییر mode با `gate_mode_change_timeout` رد می‌شود؛ گذشت زمان هیچ‌وقت آن را موفق نمی‌کند |
| LP-166 | نویسنده‌های در جریان در سه process مستقل؛ تغییر mode | تغییر تا خروج همه‌ی آن‌ها منتظر می‌ماند؛ سپس موفق |
| LP-167 | آخرین نویسنده‌ی `off` و اولین نویسنده‌ی `enforced` روی یک node، با زمان‌بندی تزریق‌شده | بازه‌های زمانی فراخوانی نویسنده هم‌پوشانی ندارند |
| LP-168 | deploy با `gate_mode = 'off'`: پاسخ، تعداد و ترتیب فراخوانی remote، و هم‌زمانی دو نویسنده روی یک node | برابر baseline بدون کد gate؛ دو نویسنده هم‌زمان اجرا می‌شوند (بدون serialization node)؛ فقط `LOCK_SH` گرفته شده |
| LP-169 | تغییر mode که به `activation_timeout` می‌رسد | ۴۰۹؛ `gate_mode`، `gate_mode_epoch` و `version` بدون تغییر؛ هیچ event؛ فهرست نویسنده‌های مشغول در پاسخ |

### آمادگی per-node

| کد | سناریو | Invariant |
|---|---|---|
| LP-170 | دو node با backend یکسان و `server_fingerprint` متفاوت | دو ردیف مستقل؛ هرکدام contract و matcher خودش |
| LP-171 | verify موفق node اول | node دوم `unverified` می‌ماند؛ T1 روی آن ۴۰۹ `node_contract_not_ready` |
| LP-172 | ثابت `adapter_version` کد عوض می‌شود | ردیف‌های آن backend در اولین بررسی `invalidated` با `adapter_version_changed`؛ fail-closed تا verify دوباره |
| LP-173 | تغییر هر فیلد `config_fingerprint` (base URL، port، hub، inbound tag، inbound/service id، panel mode، interface) هرکدام جدا؛ و تغییر رمز node | هر تغییر پیکربندی → `invalidated` با `config_changed` (یا `backend_changed`)؛ تغییر رمز بی‌اثر |
| LP-174 | ثبت matcher با: فقط `status: 404`؛ regex؛ wildcard؛ بازه‌ی status؛ `contains`؛ `json_path` خارج از allowlist؛ کلید ناشناخته؛ پنجمین matcher | همه ۴۲۲؛ ردیف بدون تغییر. خطای اتصال یا timeout با هیچ matcherی `not_exist` نمی‌شود |
| LP-175 | تغییر یک `operation_type` به `durable` با یک node فعال `unverified`، یک `invalidated`، و یک `ready` با `adapter_version` قدیمی | هر سه رد با فهرست nodeها؛ بعد از verify همه: پذیرفته. node غیرفعال ناآماده مانع نیست. (ورود به `enforced` با همین nodeها مجاز است: LP-185) |
| LP-176 | با حداقل یک نوع `durable`: `enabled = True` کردن node غیرفعال ناآماده؛ ساخت node تازه با `enabled = True`؛ و همان بعد از verify. و همان دو کار وقتی همه‌ی انواع `legacy`‌اند | با نوع `durable`: دو مورد اول ۴۰۹ `node_contract_not_ready`، سومی پذیرفته. با همه `legacy`: هر دو پذیرفته |

### shadow: contention در برابر instrumentation

| کد | سناریو | Invariant |
|---|---|---|
| LP-177 | `shadow`: دو نویسنده‌ی instrument‌شده هم‌زمان روی یک node | فقط `contention_count + 1`؛ `writer_not_instrumented_count` ثابت؛ هر دو کامل |
| LP-178 | `shadow`: نویسنده‌ای که مستقیم و بدون `node_gate` متد نویسنده را صدا می‌زند | `writer_not_instrumented_count + 1` و `last_not_instrumented_at`؛ `contention_count` ثابت؛ فراخوانی انجام می‌شود |
| LP-179 | `shadow → enforced` با `contention_count` بزرگ و `writer_not_instrumented_count = 0`؛ و با `writer_not_instrumented_count = 1`؛ و با `mode_lock_miss_count = 1` | اولی پذیرفته؛ دومی و سومی رد |
| LP-180 | `enforced`: نویسنده با `ShadowGateContext` (ساخته‌شده قبل از تغییر mode یا دستی) | رد قبل از هر بایت شبکه؛ فقط `GateToken` پذیرفته |

### lifecycle `installation_uuid`

| کد | سناریو | Invariant |
|---|---|---|
| LP-181 | restore دیتابیس و شاخه‌ی داده‌ی همان نصب؛ و restore فقط دیتابیس در `off` | UUID حفظ؛ نام قفل‌ها برابر قبل؛ در حالت دوم فایل از ستون بازسازی می‌شود |
| LP-182 | fork-reset روی clone: معتبر؛ بدون عبارت تأیید؛ روی host مالک اصلی | معتبر: UUID تازه، همه‌ی انواع `legacy`، `gate_mode='off'`، مالک `none`، قراردادهای node `invalidated`، event `installation_fork_reset`. دو مورد دیگر رد |
| LP-183 | rotate عادی با: عملیات غیرترمینال؛ مالک `active`؛ runner زنده؛ قفل node نگه‌داشته؛ `gate_mode` غیر-`off`؛ یک نوع `durable`. و fork-reset با عملیات غیرترمینال | همه ۴۰۹ با علت؛ UUID بدون تغییر |
| LP-184 | ویرایش دستی فایل `installation.id` در `enforced` (و در `off`)، سپس startup | زیرسیستم provisioning fail-closed: صفر gate، صفر runner، صفر mutation remote؛ وضعیت `installation_mismatch`؛ API، RADIUS و بات بالا می‌آیند |

### قرارداد node و ترتیب rollout

| کد | سناریو | Invariant |
|---|---|---|
| LP-185 | `shadow → enforced` با nodeهای فعال `unverified`، `invalidated` و بدون ردیف | پذیرفته؛ آمادگی قرارداد شرط این انتقال نیست |
| LP-186 | `enforced`: نویسنده‌ی legacy (provisioning، enable/disable، حذف) روی node `unverified`، هم‌زمان با نویسنده‌ی دوم روی همان node | هر دو در runner و سریال؛ نتیجه، مقدار بازگشتی و تحمل خطا برابر semantics امروز؛ action با `semantics='durable'` روی همان node ۴۰۹ `node_contract_not_ready` قبل از spawn |
| LP-187 | تغییر نوع به `durable` و T1 durable، قبل از آمادگی nodeهای فعال | هر دو رد؛ هیچ ردیف mode عوض نمی‌شود؛ هیچ reserve و هیچ remote |
| LP-188 | `contract_probe` کامل: read غایب، create، read تأیید، delete، read غایب با projection برابر؛ و probe در `off`/`shadow` | ردیف `ready` با matcher ساخته‌شده از همان projection، نسخه‌ی adapter و دو fingerprint؛ identity آزمایشی namespaced و بدون هیچ identity مشتری. در `off`/`shadow`: ۴۰۹ `gate_not_enforced` |
| LP-189 | probe با: timeout در read اول؛ HTML؛ پاسخ ناقص؛ projection قبل و بعد متفاوت؛ read تأیید ناموفق؛ delete مبهم | در همه: ردیف `unverified` می‌ماند؛ هیچ matcher؛ node برای durable بسته. در دو مورد آخر `probe_cleanup_unknown` با identity آزمایشی و بدون secret |
| LP-190 | INSERT/UPDATE مستقیم روی `provisioning_node_contracts` (SQLite و MariaDB): `unverified` با هر یک از هشت فیلد پر؛ `ready` با هر فیلد verification خالی یا با فیلد invalidation پر؛ `invalidated` بدون `invalidated_at` یا بدون snapshot verification | همه با CHECK واقعی DB رد؛ سه شکل معتبر جدول ۵.۱۰ پذیرفته |
| LP-191 | بعد از `ready` شدن همه‌ی nodeهای فعال | تغییر نوع به `durable` موفق؛ T1 durable روی آن nodeها پذیرفته |

---

## ۱۸. تصمیم‌های باز Product

| # | موضوع | پیش‌فرض این سند |
|---|---|---|
| ۱ | reseller در T1 رزرو شود (به‌جای شارژ بعد از تحویل در بات) | بله. اعتبار ناکافی یعنی رد قبل از تحویل؛ رسید مشتری در صف می‌ماند |
| ۲ | مهلت roll-forward (`forward_deadline`) | ۱۰ دقیقه |
| ۳ | بودجه‌ی زمانی درخواست sync | ۶۰ ثانیه |
| ۴ | all-or-nothing برای پکیج bundled | بله. جایگزین (تحویل جزئی با شارژ متناسب) طراحی نشده |
| ۵ | ساخت گروهی: هر User مستقل | بله |
| ۶ | آیا قطع سرویس Xray در هر add/remove (restart) قابل‌قبول می‌ماند | بله، رفتار امروز؛ فقط restartهای بی‌دلیل retry حذف می‌شوند |
| ۷ | effectهای optional approval (تخفیف، referral، ثبت پرداخت کارت) داخل T_final اعمال شوند و فراخوانی‌های جدای بات idempotent پاسخ بگیرند (۷.۶) | بله. جایگزین: approval بعد از T_final `mutating` بماند تا بات آن‌ها را جدا بزند و `finalize` کامل کند؛ در آن صورت «T_final و `completed` اتمیک» برقرار نیست |
| ۸ | شکست عملیات، approval را خودکار `failed` کند (گزارش‌دهنده اجراکننده‌ی عملیات است، نه بات؛ ۷.۶) | بله |
| ۹ | بعد از revoke کلید وسط اجرا، superadmin بتواند «ادامه» را انتخاب کند (تحویل سرویس با مجوز `approval_recovery`) | بله؛ هر دو گزینه‌ی ادامه و لغو |

| ۱۰ | در `enforced` همه‌ی نویسنده‌های remote، از جمله مسیرهای legacy و poller، در runner فرزند اجرا شوند (۱۰.۸) | بله. جایگزین (نگه‌داشت inline و بستن کل process در hang) بات و RADIUS را هم می‌بندد |
| ۱۱ | durable فقط روی یک host backend پشتیبانی شود (۱۰.۴) | بله |
| ۱۲ | آستانه‌های retention عملیات `cleanup_required` (۶.۳) | هشدار بعد از ۷ روز؛ بستن «ادامه» بعد از ۳۰ روز |
| ۱۳ | دوره‌ی reconciliation WireGuard (۱۰.۵) | هر ۱۰ دقیقه و در startup |

| ۱۴ | `hard_deadline` هر نگه‌داشت gate و `activation_timeout` تغییر mode | ۱۲۰ ثانیه؛ ۶۰ ثانیه |
| ۱۵ | توقف کوتاه provisioning (از جمله legacy) در مدت drain هنگام handoff host (۱۰.۴) | پذیرفته |
| ۱۶ | clone ماشین زنده و اجرای دو نصب روی یک node policy عملیاتی ممنوع باشد، نه سازوکار فنی (۱۰.۴، ۱۰.۱۰) | بله؛ reboot و clone از روی داده قابل تمایز نیستند و هر دو تأیید انسانی می‌خواهند |

| ۱۷ | تغییر هر `operation_type` به `durable` به آمادگی **همه‌ی** nodeهای فعال وابسته باشد (۵.۱۰، ۱۰.۶) | بله. یک node ناآماده آن تغییر را می‌بندد مگر غیرفعال شود؛ ورود gate به `enforced` به آن وابسته نیست |
| ۱۸ | reboot بدون shutdown تمیز نیازمند reclaim دستی superadmin باشد (۱۰.۴)؛ تا آن لحظه provisioning (در `enforced`) متوقف است | بله؛ جایگزین خودکار امنی وجود ندارد |

---

## ۱۹. خارج از Scope

- تأیید رفتار دستگاه‌های واقعی (بخش ۹.۶): کار staging در L3 است، نه طراحی.
- coordinator بیرونی و هر راه‌حل split-brain برای HA؛ و durable روی بیش از یک host backend.
- تحویل جزئی با شارژ متناسب.
- enforcement سرعت برای Xray و SoftEther.
- روشن‌کردن `PRAGMA foreign_keys` در SQLite.
- ادغام حساب‌ها؛ quota/RADIUS consistency.
- هر تغییری در سند Receipt Void.
