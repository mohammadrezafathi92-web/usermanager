# طراحی Canonical نسخه‌ی ۱۶.۱: ابطال کامل رسید اسکمیِ تأییدشده (۲۰۲۶-۱۰-۰۳)

**وضعیت سند:** v16.1 (freeze: بعد از این نسخه هیچ قابلیت یا جدول تازه‌ای به این سند اضافه نمی‌شود). برای فهم این سند خواندن هیچ نسخه‌ی قبلی لازم نیست.
**وضعیت آمادگی فازها (بخش ۲۱):**
- P0، P1، P2، P3 (correlation در mode shadow) و P5: طراحی کامل است.
- **ثبت اجباری approval (`required`) دیگر فاز مستقلی نیست.** فقط همراه cutover loyalty در یک activation مشترک (فاز P9a) روشن می‌شود، چون approval `required` با loyalty legacy اثر خارج از manifest می‌سازد (بخش ۵.۵). P9a به P9 نیاز دارد، پس پشت P6 مسدود است.
- P9a علاوه بر آن چهار تصمیم Product باز دارد (بخش ۲۳، موارد ۱۵، ۱۶، ۱۸، ۱۹). سقف تأیید خودکار (P9b) فقط بعد از P9a.
- P6 تا P11 مسدودند: به state machine durable provisioning وابسته‌اند که این سند فقط **قرارداد** لازم از آن را می‌گوید (بخش ۱۲.۳).
- یعنی آنچه امروز آماده‌ی شکستن به batchهای پیاده‌سازی است: P0، P1، P2، P3، P5.

**وضعیت کد:** هیچ کد production/test تغییر نکرده؛ هیچ commit/pushی انجام نشده. همه‌ی «تغییرهای production» که در این سند نام برده می‌شوند کار آینده‌اند.

---

## فهرست

0. Scope و ممنوعیت‌ها
1. معماری و policyهای ثابت
2. Requirement → Section
3. Inventory واقعی کد فعلی
4. قراردادهای Schema
5. Correlation: `receipt_approvals` / `receipt_approval_effects`
6. Remote Bot Reconciliation
7. `wallet_operations` / `wallet_operation_steps`
8. `resource_locks` و پروتکل fencing
9. `wallet_runtime_state` و پروتکل phase/epoch
10. Schema هویت: `customer_identities` / `wallet_accounts`
11. Schema پول و conservation
12. الگوریتم‌ها: lock plan، credit، debit، refund، void کیف‌پول
13. State machine ابطال برای `new` / `renew` / `topup`
14. Effect Matrix
15. PaymentCard: ابطال علّی پرداخت
16. Business View / Audit View
17. Authorization
18. API Contract
19. UI Flow
20. Migration، Cutover، HA
21. Rollout / Rollback / Drain
22. Test Matrix
23. تصمیم‌های باز Product
24. خارج از Scope

---

## ۰. Scope و ممنوعیت‌ها

- «رمز مادر» (vendor master recovery password) کاملاً خارج از scope است.
- هیچ commit موجودی reset/revert نمی‌شود.
- حذف فیزیکی `LedgerEntry`، `wallet_lots`، `wallet_debts`، `wallet_credit_sources` و هر ردیف event این سند ممنوع است؛ خنثی‌سازی فقط با ردیف/ستون معکوس.
- تشخیص معنای یک mutation از علامت مبلغ، یا از `balance_before/after`، یا با تطبیق تقریبی username+مبلغ+زمان ممنوع است.
- overwrite مطلق موجودی (نوشتن عدد نهایی) ممنوع است؛ فقط delta با `source_kind` صریح.
- ادعای atomicity بین دیتابیس اصلی و فایل sqlite محلی بات (`pending_purchases`) ممنوع است.
- هیچ مکانیزمی نباید فقط روی SQLite یا فقط روی MariaDB درست باشد.
- هیچ secret اتصال (password، private key، PSK، UUID/credential کلاینت، token) در هیچ snapshot، log، error یا response این قابلیت ذخیره یا برگردانده نمی‌شود.
- فرض «همه‌ی instanceها یک دیتابیس هم‌زمان می‌بینند» برای HA این پروژه غلط است (بخش ۲۰.۴).
- **خارج از threat model:** کسی که دسترسی نوشتن مستقیم به دیتابیس دارد (DB admin، یا مهاجمی با همان سطح). چنین کسی می‌تواند ردیف approval، manifest، effect و hashهای آن‌ها را با هم بازنویسی کند و هیچ سازوکار این سند آن را نمی‌بیند. CHECKها، مقایسه‌ی projection با manifest و `manifest_hash` برای کشف باگ، خرابی جزئی و دستکاری ناقص‌اند، نه دفاع در برابر مالک دیتابیس. به همین دلیل نبود MAC روی ردیف‌ها پذیرفته شده است.

---

## ۱. معماری و policyهای ثابت

### اجزا

| جزء | نقش |
|---|---|
| `receipt_approvals` + `receipt_approval_effects` | correlation مرکزی هر approval با هر اثر آن |
| `wallet_operations` + `wallet_operation_steps` | backbone عملیات چندمرحله‌ای (idempotency، retry، cleanup) |
| `resource_locks` | قفل lease‌دار با fencing برای عملیات چند-تراکنشی |
| `wallet_runtime_state` | phase/epoch سراسری cutover |
| `receipt_approval_expected_effects` | manifest immutable اثرهای مورد انتظار هر approval |
| `receipt_approval_authority_events` | تاریخچه‌ی append-only هر retry، تغییر approver و takeover |
| `receipt_approval_runtime_state` | mode ثبت approval، سقف خودکار و زمان شمارش loyalty؛ نسل کامل‌بودن effect؛ آمادگی هویت کلید |
| `receipt_approval_shadow_events` | log خطاهای correlation در mode shadow |
| `sale_payment_evidence` | مدرک backend-owned پرداخت هر فروش |
| `loyalty_purchase_events`، `loyalty_reward_events`، `loyalty_policy_epochs`، `loyalty_user_epoch_anchors`، `loyalty_user_baselines` | ledger append-only loyalty و epoch آستانه |
| `auto_approval_subjects` + `auto_approval_rate_events` | سقف یک تأیید خودکار در ۶۰ دقیقه |
| `wallet_writeoffs`، `quota_reversal_shortfalls` | ثبت نرمال‌شده‌ی آنچه برگشت‌پذیر نبود |
| `customer_identities` | هویت **شخص** (tenant + Telegram) - فقط بدهی و محدودیت تقلب |
| `wallet_accounts` | کیف‌پول **یک User** - موجودی، lot، debit |
| `wallet_credit_sources` → `wallet_credit_applications` → `wallet_lots` / `wallet_debts` | ورود پول و مقصد آن |
| `wallet_debit_events` → `wallet_allocations` | برداشت FIFO |
| reversalها و adjustmentها | برگشت immutable بدون DELETE |

### Policyهای ثابت (یک policy واحد، بدون استثنا)

1. موجودی هر `wallet_account` مستقل است؛ هیچ‌وقت بین دو User ادغام نمی‌شود، حتی با `telegram_id` یکسان.
2. بدهی و محدودیت تقلب در سطح `customer_identity` است. بدهی باز روی identity، **هر** خرید/تمدید را روی **همه‌ی** accountهای همان identity مسدود می‌کند. approval مشترک شرط این enforcement نیست.
3. خرید/تمدید و topup دو predicate جدا دارند (بخش ۱۲.۷). بدهی باز و fraud hold فقط خرید/تمدید را رد می‌کنند. topup تسویه‌کننده‌ی بدهی در هر دو حالت مجاز می‌ماند. فقط flag مستقل `wallet_accounts.topup_blocked` topup را متوقف می‌کند.
4. حساب جدید با همان Telegram موجودی حساب قبلی را به ارث نمی‌برد؛ فقط بدهی/مسدودیت identity روی آن اثر دارد.
5. مصرف کیف‌پول FIFO با ترتیب `(created_at, id)`.
6. ابطال topup: باقی‌مانده‌ی lot پس گرفته می‌شود؛ بخش خرج‌شده بدهی صریح می‌شود؛ فروش‌های پایین‌دستی و lotهای سالم دست نمی‌خورند.
7. پول داخلی هرگز mint نمی‌شود: `wallet_credit_sources` فقط برای ورود پول **خارجی** ساخته می‌شود؛ جابه‌جایی داخلی با reversal/application روی همان source انجام می‌شود.
8. هر شخص (`tenant_scope_key` + Telegram) در هر بازه‌ی شناور ۶۰ دقیقه‌ای حداکثر یک رسید با تأیید خودکار دارد؛ تصمیم فقط در backend مرکزی گرفته می‌شود (بخش ۵.۴). kindهای واجد تأیید خودکار فقط `new` و `renew` هستند؛ `topup` هرگز خودکار تأیید نمی‌شود.
9. برداشت فروش از کیف‌پول سه‌مرحله‌ای است: hold، سپس capture هم‌زمان با ثبت فروش، یا release (بخش ۱۲.۳).
10. cutover به wallet-lot (P9) فقط بعد از فعال‌بودن state machine durable provisioning (P6) انجام می‌شود. کیف‌پول هیچ مدل hold موقت یا legacy ندارد.
11. ابطال یک پرداخت کارت فقط مبلغ همان پرداخت و reset ناشی از همان پرداخت را برمی‌دارد؛ pointer فعلی و پرداخت‌ها و resetهای سالم بعدی دست نمی‌خورند (بخش ۱۵).

---

## ۲. Requirement → Section

| نیاز | بخش |
|---|---|
| الگوریتم refund/debt با partial settlement unwind | ۱۲.۵ |
| conservation بدون mint دوباره | ۱۱.۲ |
| lock plan چند-account/چند-identity | ۱۲.۱ |
| HA واقعاً fail-closed | ۲۰.۴ |
| phase/epoch بدون تناقض | ۹ |
| revalidation با expiry + acquire/renew/release کامل | ۸ |
| state machine کامل new/renew/topup | ۱۳ |
| ابطال علّی پرداخت کارت | ۱۵ |
| یک‌به‌یک بودن WalletAccount در DB | ۱۰ |
| schema دقیق | ۴ تا ۱۱، ۱۵ |
| test matrix | ۲۲ |
| predicate جدای خرید و topup | ۱۲.۷ |
| قرارداد provisioning و hold/capture/release | ۱۲.۳ |
| حذف DB-only بدون deprovision دوباره | ۱۳.۷ |
| نتیجه‌های تأیید remote | ۷، ۱۳.۴ |
| manifest اثرهای مورد انتظار | ۵.۳ |
| enforcement واقعی FK | ۴.۱ |
| write-off نرمال‌شده | ۱۱.۱، ۱۱.۲ |
| برگشت پاداش حجمی بدون clipping خاموش | ۱۴.۱ |
| سقف یک تأیید خودکار در ۶۰ دقیقه | ۵.۴ |

---

## ۳. Inventory واقعی کد فعلی

همه‌ی موارد زیر با grep مستقیم روی `backend/app` تأیید شده‌اند.

### ۳.۱ نویسنده‌های `User.balance`

| # | محل | رفتار |
|---|---|---|
| ۱ | `routers/bot.py:1381` `add_balance` | مثبت: `user.balance += amount` (خط ۱۴۱۴). منفی: UPDATE شرطی اتمیک `balance + amount >= 0` و `db.commit()` مستقل (۱۴۰۴-۱۴۰۹) |
| ۲ | `routers/users.py:684` `update_user` | `schemas.UserUpdate.balance` (`schemas.py:493`) از حلقه‌ی generic `setattr` عبور می‌کند: overwrite مطلق |
| ۳ | `services/user_ops.py:96` `_maybe_grant_loyalty_reward` | credit پاداش وفاداری |
| ۴ | `services/user_ops.py:190` `apply_referral_code` | credit پاداش به معرف (User دیگر) |
| ۵ | `services/user_ops.py:194` `apply_referral_code` | credit پاداش به کاربر جدید |

`AdminUser.balance` (`routers/admins.py:131`، `services/admin_billing.py:141,297`) اعتبار reseller است، سیستم جدایی است و در این سند فقط در «برگشت admin credit» (بخش ۱۴) لمس می‌شود.

### ۳.۲ callerهای `add_balance`

| Caller | محل | علامت | نیت واقعی |
|---|---|---|---|
| تأیید رسید topup | `telegram_bot/handlers/admin_pending.py:333` | + | `receipt_topup` |
| «اعتبار کیف پول» بات ادمین | `telegram_bot/handlers/admin_users.py:595` | ± | `manual_adjustment` |
| خرید مینی‌اپ | `routers/miniapp.py:598` | − | برداشت فروش؛ **قبل از** ساخت sale commit می‌شود |
| refund مینی‌اپ | `routers/miniapp.py:634` | + | برگشت همان برداشت؛ best-effort |
| خرید از اعتبار در بات | `telegram_bot/handlers/customer_purchase.py:530` | − | مثل مینی‌اپ |
| refund بات | `telegram_bot/handlers/customer_purchase.py:603` | + | مثل مینی‌اپ |
| integration بیرونی | `POST /api/bot/users/{username}/add-balance` (`routers/bot.py:1379`، `X-API-Key`، capability `WALLET_WRITE`) | ± | نامعلوم |

### ۳.۳ ساخت و حذف User، و تغییر هویت

| موضوع | محل |
|---|---|
| ساخت `models.User(...)` | `routers/users.py:453`، `routers/tg_tunnel.py:155`، `services/user_ops.py:127`، `:1869`، `:2236`، `:2339` |
| حذف User | فقط `services/user_ops.py:516` داخل `delete_user_cascade` (فراخوانی از `routers/users.py:857`، `routers/bot.py:1447`، `services/user_ops.py:566`) |
| تغییر `User.telegram_id` | `routers/bot.py:1082` (`link_telegram`)، و `update_user` از طریق `UserUpdate.telegram_id` |
| تغییر `User.owner_admin_id` | `routers/users.py:804` (`transfer_user`)، `services/user_ops.py:725,741`، و `update_user` |
| قفل خرید موجود | `User.purchases_blocked`؛ `_ensure_can_buy` (`routers/bot.py:190`) در ساخت User (۸۹۶)، خرید (۱۲۷۷)، تمدید (۱۳۱۹) **و topup** (۱۴۰۲) صدا زده می‌شود؛ `_ensure_telegram_can_buy` (۲۱۰-۲۲۵) همان قفل را روی همه‌ی Userهای هم‌Telegram اعمال می‌کند. یعنی امروز قفل خرید topup را هم رد می‌کند |
| حذف Purchase | `routers/users.py:1134`، `routers/bot.py:1184` |
| حذف Connection | `services/user_ops.py:1714` |
| حذف AdminUser | `routers/admins.py:938` (`delete_admin`) |
| حذف DiscountCode | `routers/discount_codes.py:119` |
| حذف LedgerEntry | هیچ caller ندارد؛ `delete_expense` (`routers/accounting.py:161`) ردیف معکوس می‌سازد |
| جایگزینی کل دیتابیس | `services/backup.py:645` (`restore_from_upload`)، `:749` (`ha_pull_and_apply`) |

**رفتار واقعی `delete_user_cascade` (`services/user_ops.py:488-517`):** برای هر Connection `deprovision_connection` را صدا می‌زند (فراخوانی remote)، ردیف‌های `RadiusActiveSession` و `UsageLog` را حذف و `RadiusLimitEventLog` را جدا می‌کند، `db.delete(user)` می‌زند و **خودش `db.commit()` می‌کند**. پس نه DB-only است و نه در تراکنش caller می‌نشیند.

**تأیید خودکار امروز (`services/auto_approve.py`):**

- `AUTO_APPROVABLE_KINDS = ("new", "renew")` (خط ۳۰). `topup` و `link` عمداً بیرون‌اند: topup پکیجی ندارد که ریسک را محدود کند.
- `decide` (۱۲۷-۱۵۸) شرط‌های روشن‌بودن، بازه‌ی ساعت، سقف مبلغ و «فقط مشتری قبلی» را از `BotSettings` یا override ادمین می‌خواند؛ هیچ سقف تعداد یا فاصله‌ی زمانی ندارد.
- `_is_returning` (۶۰-۸۴) سابقه را از `telegram_bot.storage.list_recent(limit=500)` می‌خواند، یعنی فایل sqlite **محلی همان نصب بات**. روی بات remote این داده در backend مرکزی وجود ندارد؛ پس شرط «فقط مشتری قبلی» امروز بین بات مشترک، اختصاصی و remote یکسان نیست.
- `try_auto_approve` (۱۶۱-۱۹۳) همان `perform_approval` را اجرا می‌کند. callerها: `telegram_bot/handlers/customer_purchase.py:727` و `routers/miniapp.py:744`.

**quota نامحدود:** `User.total_quota_bytes = 0` یعنی نامحدود (`models.py:792`) و `Purchase.quota_bytes = 0` یعنی نامحدود (`models.py:1042`). پاداش حجمی امروز بدون توجه به این، GB را روی مقدار فعلی جمع می‌کند (`services/user_ops.py:98`، `:192`، `:196`)؛ پس حساب نامحدود را به حساب محدود تبدیل می‌کند. پاداش‌ها فقط `User.total_quota_bytes` را می‌نویسند، نه `Purchase.quota_bytes`.

**referral:** `apply_referral_code` (`services/user_ops.py:156-198`) به **هر دو** طرف پاداش می‌دهد: اعتبار و حجم برای معرف (۱۹۰، ۱۹۲) و اعتبار و حجم برای کاربر جدید (۱۹۴، ۱۹۶)؛ مقدارها را زنده از `PanelSettings` می‌خواند.

**مقصد quota و ساخت Purchase در `new_user`:** در `create_user` بات، فقط `if payload.connections:` (`routers/bot.py:855-856`) تابع `absorb_legacy_pool_into_purchase` (`services/user_ops.py:1307`) اجرا می‌شود و یک Purchase واقعی می‌سازد که quota/مصرف/انقضای سطح User را ۱:۱ می‌گیرد (`quota_bytes = user.total_quota_bytes`). بدون اتصال، Purchase ساخته نمی‌شود. loyalty داخل `create_user_record` (`services/user_ops.py:105`، فراخوانی `_maybe_grant_loyalty_reward`) **قبل از** این Purchase اجرا می‌شود و روی User می‌نویسد؛ `apply_referral` (`routers/bot.py:926`) **بعد از** آن صدا زده می‌شود و باز `User.total_quota_bytes` را می‌نویسد، که آن موقع دیگر خوانده نمی‌شود.

**منطق reset کارت:** `advance_after_payment` (`services/payment_cards.py:100-144`) فقط وقتی شمارنده را صفر می‌کند که mode `threshold` باشد، threshold مقدار داشته باشد، **کارت همان لحظه active pointer باشد** و شمارنده به threshold رسیده باشد. rotation فقط وقتی pool بیش از یک کارت دارد، به کارت بعدی در ترتیب `_pool_query`؛ در pool تک‌کارت reset بدون rotation.

**کلید API:** `ApiKey` (`models.py:423`، ستون‌ها از ۴۴۵) `id = Integer primary_key` بدون AUTOINCREMENT و ستون `enabled` دارد. `DELETE /api-keys/{id}` (`routers/api_keys.py:188-193`) ردیف را واقعاً حذف می‌کند. پس در SQLite بعد از حذف بالاترین id، کلید بعدی می‌تواند همان id عددی را بگیرد؛ id عددی هویت پایدار یک کلید نیست.

**migration و قید یکتا:** `_auto_migrate_missing_columns` (`main.py:234`) ستون جدید را با `ALTER TABLE ADD COLUMN` اضافه می‌کند و سپس فقط `table.indexes` را با `index.create(checkfirst=True)` می‌سازد؛ شکست هر index فقط `logging.warning` می‌شود و startup ادامه می‌یابد. `UniqueConstraint` حاصل از `Column(unique=True)` روی جدول موجود ساخته **نمی‌شود**. پس یکتایی ستون جدید روی جدول موجود فقط با یک `Index(..., unique=True)` صریح به دست می‌آید، و حتی آن هم ممکن است بی‌صدا شکست بخورد.

**تمدید:** `renew_user` (`services/user_ops.py:325`) و `renew_purchase` (`:1498`) منطق یکسان دو‌شاخه دارند:

- اگر منبع حد دارد (`quota` یا `expire_at`) و هر دو هنوز جا دارند → تمدید **رزرو** می‌شود: `reserved_quota_bytes += add`، `reserved_duration_days += add_days`، و `reserved_package_id` اگر پکیج داده شده. ستون‌های اصلی دست نمی‌خورند. `renew_user` در این شاخه زود برمی‌گردد و `purchase_count` را زیاد **نمی‌کند**.
- وگرنه (quota تمام شده، یا منقضی شده، یا منبع هیچ حدی ندارد) → **reset کامل**: `used_bytes = 0`؛ اگر `add_gb` هست `quota = دقیقاً add` (نه `quota_before + add`)؛ اگر `add_days` هست `expire_at = now + add_days` (نه `expire_before + add_days`)؛ `package_id` اگر داده شده. `renew_user` در این شاخه `purchase_count += 1` می‌کند و loyalty را صدا می‌زند.
- `add_gb = 0` و `add_days = 0` با `reset_usage` فقط `used_bytes = 0` می‌کند و فروش نیست.

**زمان شمارش loyalty امروز (ناهمگون):**

| مسیر | `purchase_count += 1` و loyalty |
|---|---|
| ساخت User (`user_ops.py:146-150`) | همان لحظه |
| `renew_user`، reset فوری (`:398-399`) | همان لحظه |
| `renew_user`، رزرو | **بعداً**، در `_maybe_activate_reserved_renewal` (`:405`، خطوط ۴۴۶-۴۴۷)، وقتی poller یا RADIUS رزرو را فعال می‌کند؛ ممکن است ساعت‌ها یا روزها بعد باشد |
| `apply_package_as_purchase` (`:1369`، خطوط ۱۴۹۱-۱۴۹۲) و مسیر `:820-821` | همان لحظه |
| `renew_purchase` (`:1498`)، هر دو شاخه | **هرگز** |
| `_maybe_activate_reserved_purchase_renewal` (`:1553`) | هرگز |

`_maybe_grant_loyalty_reward` (`user_ops.py:66`) آستانه و مقدار پاداش را زنده از `PanelSettings` سراسری می‌خواند: `due = purchase_count // threshold`، و `due - loyalty_rewards_given` پاداش می‌دهد. `purchase_count` و `loyalty_rewards_given` دو شمارنده‌ی خام روی `User` هستند، بدون هیچ ردیف رویداد.

**مبلغ و روش پرداخت فروش از payload می‌آید:** `_record_bot_sale` (`routers/bot.py:278`) `paid_amount` (خط ۲۹۸) و `payment_method` (خط ۳۱۳) را از بدنه‌ی درخواست می‌گیرد؛ اگر مبلغ نباشد `accounting.sale_fallback_price` (`services/accounting.py:143`) قیمت پکیج را می‌گذارد (خط ۳۰۲). `/api/bot` برای integration بیرونی هم باز است، پس caller می‌تواند `payment_method='card'` یا `'wallet'` با مبلغ مثبت بفرستد بدون این‌که پولی دریافت یا کیف‌پولی capture شده باشد. `LedgerEntry` هیچ مدرک backend-owned از پرداخت ندارد.

پس یک تمدید رزروشده‌ی User امروز می‌تواند مدت‌ها بعد از بسته‌شدن approval پاداش loyalty بسازد، بدون هیچ ارتباطی با آن approval؛ و تمدید Purchase اصلاً شمرده نمی‌شود.

**ترتیب اتصال‌های پکیج:** `Package.connections` (`models.py:1545-1547`) هیچ `order_by` ندارد و `provision_package_connections` (`services/user_ops.py:1226`، حلقه در ۱۲۴۶) به همان ترتیب relationship می‌سازد. ترتیب ساخت اتصال‌ها تضمین‌شده نیست.

**Ledger قدیمی:** `payment_method` می‌تواند NULL باشد (ردیف‌های build قدیمی بات و تاریخچه‌ی backfill)؛ `services/accounting.py:411-419` آن را برای محاسبه‌ی نقد «کیف‌پول نیست» حساب می‌کند.

**ویرایش کارت:** `PaymentCardUpdate` (`schemas.py:1139-1144`) فقط `card_number`، `card_holder`، `is_active`، `sort_order`، `approval_telegram_id` را می‌پذیرد؛ `accumulated_amount` را نمی‌پذیرد.

**پکیج:** `Package` و `PackageConnection` (`models.py:1611-1615`: `node_id`، `protocol`، `flow`) هیچ ستون revision یا hash ندارند؛ توابع خرید همیشه ردیف‌های زنده را می‌خوانند.

### ۳.۴ نویسنده‌های PaymentCard

| محل | رفتار |
|---|---|
| `services/payment_cards.py:120-142` `advance_after_payment` | `accumulated_amount += amount`؛ در عبور از threshold: pointer به کارت بعدی و `accumulated_amount = 0` |
| `routers/panel_settings.py:329-390` | pool سراسری: create (۳۳۲)، activate خودکار اولین کارت (۳۴۲)، update (۳۵۰)، **حذف فیزیکی** (۳۶۸) و جابه‌جایی pointer (۳۷۰-۳۷۵)، activate دستی (۳۹۰) |
| `routers/panel_settings.py:546-641` | pool هر ادمین: mode/pointer/threshold (۵۵۵-۵۵۹)، create (۵۸۵-۵۸۹)، update (۶۰۳)، **حذف فیزیکی** (۶۲۶-۶۳۰)، activate (۶۴۱) |
| `routers/panel_settings.py:219` `update_settings` | payload از `PanelSettingsUpdate`؛ حلقه‌ی generic `for k, v in data.items(): setattr(row, k, v)` (۲۹۲-۲۹۳) فیلدهای سراسری `payment_card_mode`، `active_payment_card_id`، `payment_card_switch_threshold` (`models.py:1736-1738`) را می‌نویسد |

### ۳.۵ دو واقعیت زیرساختی

- **SQLite در این پروژه FK را enforce نمی‌کند.** `database.py:28-38` فقط `journal_mode=WAL` و `busy_timeout` را ست می‌کند و `PRAGMA foreign_keys=ON` وجود ندارد. پس هر قاعده‌ی ON DELETE این سند روی MariaDB قید واقعی DB است و روی SQLite باید توسط سرویس اجرا شود (بخش ۴).
- **الگوی قفل دو-دیالکتی از قبل در پروژه هست:** `BEGIN IMMEDIATE` روی SQLite و `SELECT ... FOR UPDATE` روی MySQL (`_lock_package_node_scope_settings`). این سند همان الگو را به کار می‌برد.

### ۳.۶ تصمیم‌های طراحی برای این inventory

1. همه‌ی نویسنده‌های ۳.۱ به `wallet_service` منتقل می‌شوند. هیچ کد دیگری `User.balance` را نمی‌نویسد.
2. `add_balance` بعد از مهاجرت، `source_kind` اجباری می‌گیرد **برای هر علامت**. درخواست بدون آن ۴۲۲ می‌گیرد؛ هیچ bucket حدسی وجود ندارد.
3. از طریق `add_balance` فقط دو معنا مجاز است: `receipt_topup` (مثبت، با `approval_uuid` ثبت‌شده) و `manual_adjustment` (هر علامت، با `reason` و `idempotency_token`؛ actor از principal).
4. برداشت فروش دیگر از `add_balance` عبور نمی‌کند؛ با hold و سپس capture در همان commit ثبت فروش انجام می‌شود (بخش ۱۲.۳).
5. refund دیگر از `add_balance` عبور نمی‌کند؛ برای hold ثبت‌نشده `release` و برای فروش ثبت‌شده `wallet_service.refund_debit(wallet_debit_event_id)` است (بخش‌های ۱۲.۳ و ۱۲.۴).
6. `UserUpdate.balance` حذف می‌شود؛ فرم پنل به endpoint manual-adjustment وصل می‌شود.
7. loyalty/referral با `origin_kind` خودشان و `source_key` deterministic از سرویس عبور می‌کنند.
8. هر شش محل ساخت User باید account بسازند، و `delete_user_cascade` باید account را tombstone کند (بخش ۱۰).
9. هر تغییر `telegram_id`/`owner_admin_id` باید از rebind identity عبور کند (بخش ۱۰.۳).
10. همه‌ی نویسنده‌های ۳.۴ به `payment_card_service` منتقل می‌شوند (بخش ۱۵)؛ `update_settings` سه فیلد کارت را از حلقه‌ی generic بیرون می‌کشد و به سرویس می‌دهد.
11. `_ensure_can_buy` به دو predicate `can_purchase` و `can_topup` شکسته می‌شود (بخش ۱۲.۷).
12. `delete_user_cascade` به «orchestration remote» و «finalizer فقط-DB» تفکیک می‌شود (بخش ۱۳.۷).
13. تصمیم تأیید خودکار و سقف ۶۰ دقیقه به backend مرکزی منتقل می‌شود (بخش ۵.۴)؛ `auto_approve.decide` و `_is_returning` فعلی مرجع تصمیم نیستند.
14. اجرای هر approval از مشخصات منجمد manifest انجام می‌شود، نه از پکیج زنده (بخش ۵.۳).
15. پاداش حجمی فقط روی یک مقصد یکتا و محدود اعمال می‌شود و همان مقصد ثبت و برگشت داده می‌شود (پیش‌فرض بخش ۱۴.۱).
16. بات برای `new`/`renew` همیشه endpoint مرکزی auto را صدا می‌زند؛ در mode `required` تصمیم محلی gate نیست (بخش ۵.۶).
17. بعد از cutover فاز P9a: loyalty در **زمان پرداخت** شمرده می‌شود و فقط برای فروشی که مدرک پرداخت backend-owned دارد؛ منبع حقیقت آن یک ledger append-only است و دو شمارنده‌ی `User` فقط cache‌اند؛ تغییر آستانه با epoch آینده‌نگر است (بخش ۱۴.۲). تا آن cutover loyalty مثل امروز می‌ماند.
18. تغییر تنظیمات loyalty فقط از `loyalty_service.change_policy` انجام می‌شود، نه از `setattr` generic تنظیمات.

---

## ۴. قراردادهای Schema

این قراردادها برای **همه‌ی** جدول‌های سند صادق‌اند و در DDLها تکرار نمی‌شوند.

| موضوع | قرارداد |
|---|---|
| `id` | `BIGINT NOT NULL PRIMARY KEY AUTOINCREMENT` (در SQLAlchemy: `BigInteger().with_variant(Integer, "sqlite")`، چون SQLite فقط `INTEGER PRIMARY KEY` را خودافزا می‌کند) |
| مبلغ | `BIGINT` تومان، بدون اعشار |
| زمان | `DATETIME(6)` UTC. `created_at` همیشه `NOT NULL` و توسط سرویس پر می‌شود. ستون زمانی nullable صریحاً `NULL` نوشته شده |
| UUID | `CHAR(36)` |
| enum | `VARCHAR` با `CHECK (col IN (...))` |
| JSON | `TEXT` حاوی JSON معتبر؛ `NOT NULL DEFAULT '{}'` مگر خلافش نوشته شود |
| `version` | `INT NOT NULL DEFAULT 0`؛ هر UPDATE آن را +۱ می‌کند با `WHERE version = :expected` |
| FK | روی MariaDB قید واقعی با قاعده‌ی نوشته‌شده. روی SQLite قید DB وجود ندارد (۳.۵)؛ enforcement هر parent در جدول ۴.۱ مشخص و قابل‌آزمون است |
| CHECK | هر دو دیالکت enforce می‌کنند (SQLite همیشه، MariaDB از 10.2.1) |
| UNIQUE و NULL | هیچ UNIQUEی در این سند برای درست کارکردن به ستون nullable تکیه ندارد، جز مواردی که صریحاً «NULL-exempt» نوشته شده و همان رفتار مطلوب است |
| migration | ابزار موجود (`main.py` `_auto_migrate_missing_columns` و `create_all`) فقط جدول/ستون/index **اضافه** می‌کند. این سند هیچ ستون NOT NULL به جدول موجود اضافه نمی‌کند |

### ۴.۱ Enforcement کلیدهای خارجی

روشن‌کردن `PRAGMA foreign_keys=ON` انتخاب نشده است: روی دیتابیس‌های موجود ردیف‌های dangling قدیمی را به خطای زمان اجرا تبدیل می‌کند و preflight و rollout جدا می‌خواهد (بخش ۲۴). به‌جای آن، enforcement برنامه‌ای با سه سازوکار قابل‌آزمون:

- **G-NODELETE:** برای مدل‌های جدید این سند هیچ مسیر حذف در کد وجود ندارد. تست AST (RV-100) هر `db.delete(...)`، `query(Model).delete(...)` و `delete(Model.__table__)` روی این مدل‌ها را در کل `backend/app` رد می‌کند.
- **G-FINALIZER:** حذف parentهای قدیمی که فرزند جدید دارند فقط از یک تابع مشخص عبور می‌کند که فرزندان را طبق policy به‌روز می‌کند؛ تست AST همان را ثابت می‌کند.
- **G-ORPHAN:** job تطبیق (بخش ۲۰.۵) برای هر FK جدید یک query «فرزند بدون parent» اجرا می‌کند و alert می‌دهد. روی SQLite این جایگزین قید DB است؛ روی MariaDB لایه‌ی دوم.

| Parent | فرزندهای جدید | callerهای حذف واقعی | Policy | Guard | تست |
|---|---|---|---|---|---|
| `users` | `wallet_accounts.user_id` | فقط `services/user_ops.py:516` (از `routers/users.py:857`، `routers/bot.py:1447`، `user_ops.py:566`) | RESTRICT تا tombstone؛ finalizer `user_id` را NULL می‌کند | G-FINALIZER: حذف فقط در `finalize_user_deletion_after_deprovision` (۱۳.۷) | RV-5، RV-62 |
| `ledger_entries` | `wallet_debit_events.ledger_entry_id`، `ledger_entries.reversal_of_id`، `loyalty_purchase_events.ledger_entry_id` | هیچ | RESTRICT: Ledger هرگز حذف نمی‌شود | G-NODELETE روی `LedgerEntry` | RV-100 |
| `payment_cards` | `payment_card_pool_events.card_id` | `routers/panel_settings.py:368`، `:626` | SET NULL؛ `card_id_snapshot` می‌ماند | G-FINALIZER: `payment_card_service.remove_card` در همان تراکنش `card_id` رویدادها را NULL می‌کند | RV-82، RV-64 |
| `admin_users` | هیچ FK؛ همه‌ی ارجاع‌ها snapshot عددی‌اند (`owner_admin_id_snapshot`، `actor_admin_id`، `approved_by_admin_id`) و `tenant_scope_key` رشته است | `routers/admins.py:938` | بدون قید؛ حذف ادمین مجاز | لازم نیست | RV-9، RV-93 |
| `purchases` | هیچ FK؛ `receipt_approval_effects.resource_id` بدون FK است | `routers/users.py:1134`، `routers/bot.py:1184` | بدون قید؛ effect با snapshot می‌ماند | preview و void effect بدون منبع را «از قبل حذف‌شده» گزارش می‌کنند | RV-104 |
| `connections` | هیچ FK | `services/user_ops.py:1714` و حذف داخل finalizer | مثل `purchases` | مثل بالا | RV-104 |
| `discount_codes` / `discount_code_redemptions` | هیچ FK جدید؛ فقط سه ستون nullable روی redemption | `routers/discount_codes.py:119` | بدون قید | اگر redemption دیگر وجود ندارد، گام discount با note `done` می‌شود | RV-59 |
| `receipt_approvals` | `receipt_approval_effects`، `receipt_approval_expected_effects`، `receipt_approval_authority_events`، `wallet_credit_sources.causation_approval_uuid` | هیچ (جدول جدید) | RESTRICT | G-NODELETE | RV-100 |
| `receipt_approval_effects` | `quota_reversal_shortfalls` | هیچ | RESTRICT | G-NODELETE | RV-100 |
| `api_keys` | هیچ FK. approval فقط `key_instance_uuid` را به‌صورت رشته نگه می‌دارد | `routers/api_keys.py:192` (حذف واقعی) | بدون قید؛ کلید حذف‌شده یعنی instance آن دیگر وجود ندارد → `execution_key_revoked`. id عددی دوباره‌استفاده‌شده هیچ اثری ندارد | بررسی binding با UUID (۶.۴) | RV-239، RV-251 |
| `wallet_operations` | `wallet_operation_steps` و همه‌ی ستون‌های `*_operation_id` | هیچ | RESTRICT | G-NODELETE | RV-100 |
| `customer_identities` | `wallet_accounts`، `wallet_debts`، `wallet_writeoffs`، rebindها | هیچ | RESTRICT | G-NODELETE | RV-100 |
| `wallet_accounts` | sourceها، lotها، debitها، بدهی‌ها، write-offها، repairها، rebindها | هیچ | RESTRICT | G-NODELETE | RV-100 |
| `wallet_credit_sources` | applicationها، `wallet_lots.source_credit_source_id` | هیچ | RESTRICT | G-NODELETE | RV-100 |
| `wallet_credit_applications` | reversalها | هیچ | RESTRICT | G-NODELETE | RV-100 |
| `wallet_lots` | applicationها، allocationها، بدهی‌ها، write-offها | هیچ | RESTRICT | G-NODELETE | RV-100 |
| `wallet_debts` | applicationها (settlement)، adjustmentها | هیچ | RESTRICT | G-NODELETE | RV-100 |
| `wallet_debit_events` | allocationها | هیچ | RESTRICT | G-NODELETE | RV-100 |
| `wallet_allocations` | allocation reversalها | هیچ | RESTRICT | G-NODELETE | RV-100 |
| `wallet_allocation_reversals` | adjustmentها | هیچ | RESTRICT | G-NODELETE | RV-100 |
| `payment_card_pool_states` | baseline و eventها | هیچ | RESTRICT | G-NODELETE | RV-100 |
| `loyalty_purchase_events` | `loyalty_reward_events` | هیچ | RESTRICT | G-NODELETE | RV-100 |
| `loyalty_reward_events` | `quota_reversal_shortfalls.loyalty_reward_event_id` | هیچ | RESTRICT | G-NODELETE | RV-100 |
| `wallet_debit_events` (مدرک) | `sale_payment_evidence.wallet_debit_event_id` | هیچ | RESTRICT | G-NODELETE | RV-100 |
| `loyalty_policy_epochs` | anchorها، `loyalty_reward_events` | هیچ | RESTRICT | G-NODELETE | RV-100 |
| `sale_payment_evidence` | `loyalty_purchase_events` | هیچ | RESTRICT | G-NODELETE | RV-100 |

`restore_from_upload` و `ha_pull_and_apply` کل دیتابیس را یک‌جا جایگزین می‌کنند؛ parent و فرزند با هم می‌آیند و guardهای بالا را دور نمی‌زنند. job G-ORPHAN بعد از هر restore اجرا می‌شود.

---

## ۵. Correlation

### ۵.۱ `receipt_approvals`

```
id                           PK
approval_uuid                CHAR(36)    NOT NULL  UNIQUE
pending_source_instance_id   VARCHAR(64) NOT NULL
pending_local_id             BIGINT      NOT NULL
registration_seq             INT         NOT NULL DEFAULT 0   -- فقط بعد از cancelled شدن ثبت قبلی همان pending زیاد می‌شود
immutable_intent_hash        CHAR(64)    NOT NULL  -- SHA-256 intent ثابت (پایین)؛ بین همه‌ی registration_seqهای یک pending یکسان
manifest_hash                CHAR(64)    NOT NULL  -- SHA-256 ردیف‌های receipt_approval_expected_effects ؛ بین seqها می‌تواند فرق کند
supersedes_approval_uuid     CHAR(36)    NULL      -- ثبت cancelled قبلی همین pending ؛ NULL فقط برای seq = 0
cancellation_reason          VARCHAR(40) NULL      -- فقط وقتی state = 'cancelled'
registered_under_mode        VARCHAR(10) NOT NULL  CHECK IN ('shadow','required')   -- mode مؤثر لحظه‌ی ثبت ؛ immutable
completeness_generation      INT         NOT NULL DEFAULT 0   -- نسل activation مشترک در لحظه‌ی ثبت (بخش ۵.۵) ؛ 0 = ثبت shadow ؛ immutable
registered_by_key_instance_uuid  CHAR(36) NULL     -- instance کلید ثبت‌کننده (NULL = بات in-process) ؛ immutable
execution_key_instance_uuid      CHAR(36) NULL     -- تنها instance کلیدی که اجازه‌ی اجرا دارد (NULL = فقط principal in-process) ؛ فقط takeover آن را عوض می‌کند
registered_by_api_key_id     INT         NULL      -- فقط snapshot نمایشی؛ در هیچ بررسی‌ای استفاده نمی‌شود
kind                         VARCHAR(8)  NOT NULL  CHECK IN ('new','renew','topup')
target_shape                 VARCHAR(16) NOT NULL  CHECK IN ('new_user','existing_user','renew_purchase','renew_user','topup')
approval_mode                VARCHAR(8)  NOT NULL  CHECK IN ('auto','manual')
auto_granted_at              DATETIME(6) NULL      -- زمان authoritative سهمیه‌ی خودکار (DB_NOW)؛ بعد از نوشتن هرگز پاک نمی‌شود
original_approval_mode       VARCHAR(8)  NOT NULL  CHECK IN ('auto','manual')   -- immutable: mode لحظه‌ی اولین ثبت
original_approved_by_telegram_id  BIGINT NULL      -- immutable: approver اولین ثبت دستی (NULL اگر اولین ثبت auto بود)
original_approver_evidence_kind   VARCHAR(20) NULL -- immutable
approved_by_admin_id         INT         NULL      (بدون FK؛ snapshot عددی) -- approver فعلی اجرا
approved_by_telegram_id      BIGINT      NULL      -- approver فعلی اجرا
approver_evidence_kind       VARCHAR(20) NULL      CHECK IN ('linked_admin','owner_seller','card_approver')
auto_approve_policy_snapshot TEXT/JSON   NOT NULL
owner_admin_id_snapshot      INT         NULL      (NULL = بات مشترک بدون مالک)
tenant_scope_key             VARCHAR(64) NOT NULL
telegram_id_snapshot         BIGINT      NULL      -- مشتق backend (پایین)؛ NULL فقط برای User بدون Telegram
telegram_id_source           VARCHAR(12) NOT NULL  CHECK IN ('user_row','bot_attested','none')
target_user_id_snapshot      INT         NULL      -- برای همه‌ی target_shapeها جز new_user پر است
target_username_snapshot     VARCHAR(128) NOT NULL
receipt_file_id_snapshot     VARCHAR(255) NULL
amount_snapshot              BIGINT      NOT NULL
payment_card_id_snapshot     INT         NULL
package_id_snapshot          INT         NULL
state                        VARCHAR(20) NOT NULL  CHECK IN ('registered','mutating','completed','failed','cancelled','voiding','voided','cleanup_required')
created_at                   NOT NULL
mutating_at, completed_at, failed_at, voided_at   NULL
void_operation_id            BIGINT      NULL  FK wallet_operations(id) ON DELETE RESTRICT
version

UNIQUE (pending_source_instance_id, pending_local_id, registration_seq)
INDEX  (tenant_scope_key, state)
INDEX  (tenant_scope_key, telegram_id_snapshot, auto_granted_at)
CHECK  ((approval_mode = 'manual') = (approved_by_telegram_id IS NOT NULL))
CHECK  ((approved_by_telegram_id IS NULL) = (approver_evidence_kind IS NULL))
CHECK  (approved_by_admin_id IS NULL OR approved_by_telegram_id IS NOT NULL)
CHECK  ((original_approval_mode = 'manual') = (original_approved_by_telegram_id IS NOT NULL))
CHECK  ((original_approved_by_telegram_id IS NULL) = (original_approver_evidence_kind IS NULL))
CHECK  (approval_mode <> 'auto' OR (auto_granted_at IS NOT NULL AND telegram_id_snapshot IS NOT NULL AND kind IN ('new','renew')))
CHECK  ((telegram_id_source = 'none') = (telegram_id_snapshot IS NULL))
CHECK  ((target_shape = 'new_user') = (target_user_id_snapshot IS NULL))
CHECK  ((target_shape = 'new_user') = (telegram_id_source = 'bot_attested'))
CHECK  ((registration_seq = 0) = (supersedes_approval_uuid IS NULL))
CHECK  ((registered_under_mode = 'required') = (completeness_generation >= 1))
CHECK  ((state = 'cancelled') = (cancellation_reason IS NOT NULL))
```

**دو hash جدا:**

| hash | ورودی | قاعده |
|---|---|---|
| `immutable_intent_hash` | `kind`، `target_shape`، `target_username`، `target_user_id`، `renew_purchase_id`، مبلغ نهایی، `payment_card_id`، `receipt_file_id`، `package_id`، کد تخفیف، کد دعوت، `telegram_id` و `owner_admin_id` مشتق‌شده | برای یک `(pending_source_instance_id, pending_local_id)` **هرگز** عوض نمی‌شود، حتی بعد از `cancelled` |
| `manifest_hash` | مشخصات قابل‌تغییر: اتصال‌های پکیج، quota/روز، وضعیت nodeها، معرف و مقدار پاداش‌ها، مقصد پاداش حجمی | هر `registration_seq` مقدار خودش را دارد |

mode و approver در هیچ‌کدام نیستند.

`version` این ردیف **execution token** است: پاسخ ثبت و takeover آن را برمی‌گرداند و هر production call باید همان را همراه `approval_uuid` بفرستد (بخش ۶.۵).

`cancelled` یعنی approval قبل از هر effect به‌خاطر شکست یک پیش‌شرط (بخش ۵.۳) بسته شد؛ `cancellation_reason` کد همان پیش‌شرط است. ثبت دوباره‌ی همان pending فقط وقتی مجاز است که آخرین ثبت آن `cancelled` باشد **و** `immutable_intent_hash` برابر باشد؛ ردیف جدید `registration_seq + 1` و `supersedes_approval_uuid` = ردیف قبلی می‌گیرد. `auto_granted_at` ردیف `cancelled` می‌ماند و سهمیه را آزاد نمی‌کند.

**هویت approval:**

| `target_shape` | `owner_admin_id` و `tenant_scope_key` | `telegram_id_snapshot` |
|---|---|---|
| `existing_user`، `renew_purchase`، `renew_user`، `topup` | backend User را با `target_username` در scope همان principal resolve می‌کند؛ مالک از `User.owner_admin_id` | از `User.telegram_id` (`telegram_id_source='user_row'`). اگر `NULL` است: `none`، و مسیر auto با `auto_approval_policy_denied` رد می‌شود |
| `new_user` | مالک از scope خود principal (`BotPrincipal` بات اختصاصی/remote به یک مالک bind است؛ بات مشترک `shared`): backend-derived | **managed-bot-attested** (`bot_attested`): مقدار `telegram_id` ردیف pending که نصب managed از `from_user.id` نوشته. backend update تلگرام را نمی‌بیند و این مقدار را مستقل تأیید نمی‌کند |

- برای targetهای موجود هویت **backend-derived** است: هر فیلد `telegram_id`، `owner_admin_id` یا `tenant_scope_key` که caller می‌فرستد فقط برای تطبیق است؛ اختلاف → ۴۲۲ `identity_mismatch` و approval ساخته نمی‌شود.
- برای `new_user` هویت Telegram یک **ادعای مورد اعتماد نصب managed** است، نه مقدار مشتق backend. فقط از principalهای bot-managed پذیرفته می‌شود (بات in-process و کلید نصب‌هایی که خود پنل ساخته)؛ integration عادی ۴۰۳ می‌گیرد. `pending_source_instance_id` و `registered_by_api_key_id` روی approval ثبت می‌شوند تا هر ادعا به نصب و کلیدش قابل ردیابی باشد (threat model در ۶.۴).
- binding در اجرا: `create_user` با `approval_uuid` فقط وقتی پذیرفته می‌شود که `telegram_id` و `username` بدنه با snapshot approval برابر باشد؛ `purchase_package`/`renew`/`add_balance` فقط روی `target_user_id_snapshot`. اختلاف → ۴۰۹ قبل از هر mutation.
- پس تغییر `telegram_id`، `username` یا `owner_admin_id` در payload نمی‌تواند subject سقف ۶۰ دقیقه را عوض کند.

`kind='link'` در این جدول ثبت نمی‌شود: پرداخت نیست و واجد ابطال نیست.

**ثبت idempotent:**

```
POST /api/accounting/receipt-approvals/auto      (capability RECEIPT_AUTO_APPROVAL_WRITE)
POST /api/accounting/receipt-approvals/manual    (capability RECEIPT_MANUAL_APPROVAL_WRITE)

  tx (MariaDB: SELECT آخرین ردیف همان pending ... FOR UPDATE ؛ SQLite: BEGIN IMMEDIATE):
  last = آخرین ردیف (بیشترین registration_seq) با همان (pending_source_instance_id, pending_local_id)

  last هست و last.immutable_intent_hash != hash   → 409 intent_changed   (همیشه؛ حتی اگر last 'cancelled' است)
  same_key = (instance کلید caller = last.execution_key_instance_uuid ؛ هر دو NULL هم برابرند)

  last هست و last.state IN ('registered','failed') و effect ندارد:
      درخواست=auto،  last=auto،  same_key،  registered   → 200، همان token
      درخواست=auto،  last=auto،  same_key،  failed       → retry کنترل‌شده، 200، token تازه
      درخواست=auto،  last=manual                          → 409
      درخواست=auto،  same_key نیست                        → 403 execution_principal_mismatch
      درخواست=manual، same_key، last=manual، همان approver، registered → 200، همان token
      درخواست=manual، same_key، last=manual، failed یا approver دیگر    → retry کنترل‌شده، 200، token تازه
      درخواست=manual، در هر حالت دیگر                     → takeover (پایین)، 200، token تازه
          (auto→manual ؛ manual→manual با کلید دیگر ؛ in-process→managed ؛ managed→in-process ؛ کلید قبلی revoke‌شده)
  last هست و (effect دارد یا last.state IN ('mutating','completed','voiding','voided','cleanup_required')):
      همان mode و same_key           → 200 با state فعلی، بدون هیچ تغییر
      وگرنه                          → 409 approval_has_effects یا 409 با state فعلی
  last نیست                         → seq = 0
  last.state = 'cancelled'          → seq = last.registration_seq + 1 ، supersedes = last.approval_uuid ، manifest تازه
  در دو حالت آخر: مسیر auto از بخش ۵.۴ عبور می‌کند (سقف ۶۰ دقیقه اعمال می‌شود) ؛ مسیر manual اعتبارسنجی approver (۶.۴) ؛ INSERT ؛ 201
```

رقابت دو ثبت یا دو ثبت دوباره‌ی هم‌زمان: هر دو همان `seq` را حساب می‌کنند؛ `UNIQUE(pending_source_instance_id, pending_local_id, registration_seq)` فقط یکی را می‌پذیرد. بازنده IntegrityError می‌گیرد، تراکنش را rollback می‌کند و **یک‌بار** از ابتدا اجرا می‌شود؛ این بار `last` ردیف برنده است و شاخه‌ی «همان mode → ۲۰۰» یا ۴۰۹ اجرا می‌شود. در MariaDB قفل `FOR UPDATE` روی آخرین ردیف همان رقابت را برای ثبت دوباره سریال هم می‌کند.

**بعد از `cancelled` (بدون loop):**

- پاسخ `manifest_precondition_failed` یعنی این approval بسته شد. بات pending محلی را به `pending` برمی‌گرداند و به **صف بررسی دستی** می‌فرستد؛ خودش ثبت خودکار دوباره نمی‌زند.
- سهمیه‌ی خودکار ثبت لغوشده مصرف‌شده می‌ماند. پس ثبت دوباره‌ی خودکار همان pending تا پایان ۶۰ دقیقه ۴۲۹ می‌گیرد؛ بعد از آن مثل هر درخواست دیگر پذیرفته می‌شود و سهمیه‌ی جدید مصرف می‌کند.
- ثبت دوباره‌ی دستی همیشه مجاز است. ادمین قبل از تأیید، manifest تازه و تفاوتش با ثبت لغوشده را می‌بیند: `POST .../receipt-approvals/manifest-preview` همان محاسبه‌ی manifest را بدون ساخت ردیف برمی‌گرداند.

**Retry کنترل‌شده.** `failed` ترمینال نیست. زیر قفل ردیف approval (بخش ۶.۵) و فقط اگر هیچ effectی وجود ندارد:

```
UPDATE receipt_approvals
   SET state = 'registered', failed_at = NULL, version = version + 1,
       approved_by_* = approver همین درخواست (فقط مسیر manual)
 WHERE approval_uuid = :u AND state IN ('registered','failed') AND version = :v
   AND NOT EXISTS (SELECT 1 FROM receipt_approval_effects WHERE approval_uuid = :u)
INSERT receipt_approval_authority_events(reason = 'retry' یا 'approver_change', ...)
```

- ردیف جدید، `registration_seq` جدید و grant خودکار جدید ساخته **نمی‌شود**. `auto_granted_at`، ردیف `auto_approval_subjects` و eventهای سقف دست نمی‌خورند؛ سقف ۶۰ دقیقه برای retry بررسی نمی‌شود، چون همان grant قبلی است.
- پاسخ execution token تازه (version جدید) می‌دهد؛ token قبلی دیگر پذیرفته نیست.
- manual → retry manual: مدرک approver دوباره اعتبارسنجی می‌شود (۶.۴). approver می‌تواند شخص دیگری باشد؛ در آن صورت `reason='approver_change'`.
- کلید caller باید همان `execution_key_instance_uuid` و هنوز enabled باشد (۶.۴). اگر نیست، مسیر takeover است، نه retry.
- `failed` که effect دارد هرگز reset نمی‌شود (۴۰۹ `approval_has_effects`)؛ مسیر آن `approval_recovery` است.

**Takeover دستی.** برای **هر** approval در `registered` یا `failed` که هیچ effectی ندارد، مستقل از mode قبلی. هدفش دقیقاً recovery است، از جمله وقتی کلید اجرای قبلی revoke یا حذف شده؛ پس فعال‌بودن کلید قبلی شرط نیست.

| انتقال | مجاز؟ |
|---|---|
| auto → manual | بله |
| manual → manual با کلید دیگر (کلید قبلی فعال یا revoke‌شده) | بله |
| in-process → نصب managed | بله |
| نصب managed → in-process (تأیید از بات داخل پنل) | بله |
| هر approval در `mutating` یا دارای effect | خیر، ۴۰۹ |
| manual → auto | خیر، ۴۰۹ |

شرط‌های principal تحویل‌گیرنده، همه لازم: bot-managed (in-process یا کلید نصب ساخته‌ی پنل)؛ کلید enabled با `key_instance_uuid`؛ `RECEIPT_MANUAL_APPROVAL_WRITE`؛ scope شامل tenant همان approval؛ و مدرک approver معتبر (۶.۴). هرکدام نبود → ۴۰۳ و هیچ تغییری.

```
UPDATE receipt_approvals
   SET approval_mode = 'manual', approved_by_* = approver جدید, state = 'registered', failed_at = NULL,
       execution_key_instance_uuid = instance کلید تحویل‌گیرنده (NULL برای in-process), version = version + 1
 WHERE approval_uuid = :u AND state IN ('registered','failed') AND version = :v
   AND NOT EXISTS (SELECT 1 FROM receipt_approval_effects WHERE approval_uuid = :u)
INSERT receipt_approval_authority_events(reason = 'revoked_key_takeover' اگر instance قبلی دیگر enabled نیست یا وجود ندارد ؛ وگرنه 'manual_takeover')
```

`auto_granted_at` می‌ماند، پس سهمیه‌ی مصرف‌شده آزاد نمی‌شود. `version + 1` token قبلی را باطل می‌کند. takeover، retry و شروع mutation روی قفل همان ردیف approval سریال می‌شوند (بخش ۶.۵).

**تاریخچه‌ی authority (append-only).** ستون‌های `original_*` در اولین INSERT نوشته می‌شوند و هیچ مسیری آن‌ها را UPDATE نمی‌کند. `approved_by_*` و `approval_mode` فقط «وضعیت فعلی اجرا» هستند. هر تغییر authority، از جمله retry با همان approver، یک ردیف می‌سازد:

```
receipt_approval_authority_events         -- immutable ؛ G-NODELETE و بدون مسیر UPDATE
id                              PK
approval_uuid                   CHAR(36)    NOT NULL  FK receipt_approvals(approval_uuid) ON DELETE RESTRICT
reason                          VARCHAR(24) NOT NULL  CHECK IN ('retry','approver_change','manual_takeover','revoked_key_takeover')
from_mode                       VARCHAR(8)  NOT NULL
to_mode                         VARCHAR(8)  NOT NULL
from_key_instance_uuid          CHAR(36)    NULL
to_key_instance_uuid            CHAR(36)    NULL
from_approver_telegram_id       BIGINT      NULL
to_approver_telegram_id         BIGINT      NULL
from_approver_evidence_kind     VARCHAR(20) NULL
to_approver_evidence_kind       VARCHAR(20) NULL
from_state                      VARCHAR(20) NOT NULL
version_before                  INT         NOT NULL
version_after                   INT         NOT NULL
created_at                      NOT NULL

UNIQUE (approval_uuid, version_after)
CHECK  (version_after = version_before + 1)
CHECK  (from_mode IN ('auto','manual') AND to_mode IN ('auto','manual'))
CHECK  ((from_mode = 'manual') = (from_approver_telegram_id IS NOT NULL))
CHECK  ((to_mode   = 'manual') = (to_approver_telegram_id   IS NOT NULL))
CHECK  ((from_approver_telegram_id IS NULL) = (from_approver_evidence_kind IS NULL))
CHECK  ((to_approver_telegram_id   IS NULL) = (to_approver_evidence_kind   IS NULL))
CHECK  (reason NOT IN ('manual_takeover','revoked_key_takeover','approver_change') OR to_mode = 'manual')
```

approver و evidence kind همیشه با هم‌اند و حضورشان دقیقاً با mode می‌خواند، هم روی approval (فعلی و `original_*`) و هم روی هر event.

زنجیره‌ی کامل approverها = `original_*` به‌علاوه‌ی ردیف‌های این جدول به ترتیب `version_after`. هیچ retry یا takeoverی تنها مدرک approver قبلی را پاک نمی‌کند.

### ۵.۲ `receipt_approval_effects`

```
id                 PK
approval_uuid      CHAR(36)     NOT NULL  FK receipt_approvals(approval_uuid) ON DELETE RESTRICT
effect_type        VARCHAR(40)  NOT NULL  CHECK IN (فهرست زیر)
effect_key         VARCHAR(160) NOT NULL
resource_type      VARCHAR(32)  NOT NULL
resource_id        BIGINT       NULL
actual_projection  TEXT/JSON    NOT NULL      -- همان کلیدهای schema expected، ساخته‌ی backend از منبع واقعی (بخش ۵.۳)
resource_snapshot  TEXT/JSON    NOT NULL      -- factهای زمان اجرا برای ابطال و نمایش (allowlist پایین)
delta_value        BIGINT       NULL
reversal_state     VARCHAR(24)  NULL  CHECK IN ('fully_reversed','reversed_with_debt','reversed_with_writeoff','partially_reversed',
                                                'unrecoverable','resource_deleted','not_applicable')
reversed_value     BIGINT       NULL
voided_at          NULL
void_operation_id  BIGINT       NULL  FK wallet_operations(id) ON DELETE RESTRICT
created_at         NOT NULL

UNIQUE (approval_uuid, effect_type, effect_key)
INDEX  (resource_type, resource_id)
CHECK  ((reversal_state IS NULL) = (voided_at IS NULL))
CHECK  ((reversal_state IS NULL) = (void_operation_id IS NULL))
CHECK  (reversal_state IS NOT NULL OR reversed_value IS NULL)
CHECK  (reversal_state NOT IN ('resource_deleted','not_applicable') OR reversed_value IS NULL)
CHECK  (reversal_state NOT IN ('fully_reversed','reversed_with_debt','reversed_with_writeoff','partially_reversed','unrecoverable')
        OR reversed_value IS NOT NULL)
```

چهار ستون برگشت فقط با `mark_effect_reversed` و همیشه با هم نوشته می‌شوند؛ معنا و transition هر `effect_type` در بخش ۱۴.۳ است.

`effect_key` قبل از ساخت منبع مشخص است و به `resource_id` وابسته نیست؛ retry همان کلید را می‌سازد و ردیف دوم ساخته نمی‌شود.

| `effect_type` | `resource_type` | `effect_key` | `resource_id` |
|---|---|---|---|
| `user_created` | `User` | `user` | الزامی |
| `purchase_created` | `Purchase` | `purchase` | الزامی |
| `purchase_renewed` | `Purchase` یا `User` | `renew` | الزامی |
| `connection_created` | `Connection` | `conn:package_connection:{package_connection_id}` برای اتصال bundled پکیج؛ `conn:request_slot:{index}` برای اتصال صریح درخواست | الزامی |
| `ledger_sale` | `LedgerEntry` | `sale` | الزامی |
| `ledger_topup` | `LedgerEntry` | `topup` | الزامی |
| `ledger_credit_spend` | `LedgerEntry` | `credit_spend` | الزامی |
| `wallet_credit_source_created` | `WalletCreditSource` | یکی از: `credit:receipt_topup`، `credit:referral_reward:referrer`، `credit:referral_reward:new_user`، `credit:loyalty:{epoch_id}:{slot}` | الزامی |
| `card_payment_recorded` | `PaymentCardPoolEvent` | `card_payment` | الزامی |
| `discount_redeemed` | `DiscountCodeRedemption` | `discount:{code_id}` | الزامی |
| `quota_reward_granted` | `User` یا `Purchase` | یکی از: `quota:referral:referrer`، `quota:referral:new_user`، `quota:loyalty:{epoch_id}:{slot}` | الزامی؛ `delta_value` = بایت؛ snapshot شامل `quota_before`، `quota_after`، `before_was_unlimited` |
| `loyalty_purchase_recorded` | `LoyaltyPurchaseEvent` | `loyalty_purchase` | الزامی |

همه‌ی `effect_key`ها ثابت و قبل از اجرا معلوم‌اند؛ هیچ کلیدی wildcard یا وابسته به شناسه‌ی زمان اجرا نیست. پاداش referral/loyalty دقیقاً با ردیف `wallet_credit_source_created` به `wallet_credit_sources` وصل است (نه با snapshot مبهم)، و همان source ستون `causation_approval_uuid` را هم دارد؛ پس از هر دو طرف قابل بازیابی است.

**تنها نویسنده‌ی effect** تابع `record_effect(db, approval, effect_type, effect_key, resource, evidence)` است. ورودی آن شیء منبع (ردیف ORM) و evidence تایپ‌شده‌ی داخلی است (بخش ۵.۳)، نه dict؛ `actual_projection` و `resource_snapshot` را خودش از این دو می‌سازد. هیچ endpointی projection، evidence یا snapshot را از caller نمی‌پذیرد.

**Allowlist `resource_snapshot`:** فقط کلیدهای `id`، `username`، `node_id`، `node_name`، `protocol`، `flow`، `package_id`، `package_name`، `amount`، `quota_bytes`، `quota_before`، `quota_after`، `before_was_unlimited`، `days`، `expire_at`، `counter_before`، `counter_after`، `ledger_kind`. نویسنده‌ی effect هر کلید دیگری را رد می‌کند (نه این‌که بی‌صدا حذف کند). هیچ فیلد credential اتصال هرگز به این تابع داده نمی‌شود.

### ۵.۳ `receipt_approval_expected_effects` (manifest)

```
id              PK
approval_uuid   CHAR(36)     NOT NULL  FK receipt_approvals(approval_uuid) ON DELETE RESTRICT
effect_type     VARCHAR(40)  NOT NULL
effect_key      VARCHAR(160) NOT NULL
requirement     VARCHAR(10)  NOT NULL  CHECK IN ('required','optional')
expected        TEXT/JSON    NOT NULL  -- schema دقیق همان (effect_type, effect_key) ؛ جدول پایین
created_at      NOT NULL

UNIQUE (approval_uuid, effect_type, effect_key)
```

manifest را **backend** در همان تراکنش ثبت approval از روی intent می‌سازد و بعد از آن هرگز تغییر نمی‌کند (G-NODELETE و بدون مسیر UPDATE). `manifest_hash` روی approval همان ردیف‌ها را قفل می‌کند؛ job تطبیق برابری را می‌سنجد. retry همان ثبت (همان `immutable_intent_hash`، approval غیر-`cancelled`) manifest ذخیره‌شده را برمی‌گرداند و دوباره نمی‌سازد، حتی اگر پکیج در این فاصله عوض شده باشد.

| `target_shape` | ردیف‌های `required` | ردیف‌های `optional` |
|---|---|---|
| `new_user` | `user_created:user`؛ `ledger_sale:sale`؛ برای هر slot اتصال یک `connection_created:conn:...`؛ **و اگر حداقل یک اتصال مورد انتظار هست، `purchase_created:purchase`** | پایین |
| `existing_user` | `purchase_created:purchase`؛ `ledger_sale:sale`؛ **همان ردیف‌های `connection_created` برای هر اتصال bundled پکیج** | پایین |
| `renew_purchase` / `renew_user` | `purchase_renewed:renew`؛ `ledger_sale:sale` | پایین |
| `topup` | `wallet_credit_source_created:credit:receipt_topup`؛ `ledger_sale` وجود ندارد، به‌جایش `ledger_topup:topup` | — |

`new_user` با حداقل یک اتصال یک Purchase واقعی می‌سازد (۳.۳: `absorb_legacy_pool_into_purchase` زیر `if payload.connections`)؛ `new_user` بدون اتصال Purchase نمی‌سازد. این تفاوت در خود manifest ثبت است (بود یا نبود ردیف `purchase_created`)، نه حدس زمان اجرا: اگر ردیف نیست و Purchaseی ساخته شود، نوشتن effect آن رد می‌شود؛ اگر هست و ساخته نشود، `finalize` کامل نمی‌کند. ابطال، audit و recovery Purchase را فقط از همین effect پیدا می‌کنند.

**Slot اتصال (هویت پایدار، مستقل از ترتیب اجرا).** هیچ کلیدی از ترتیب ساخت مشتق نمی‌شود:

| منبع اتصال | `effect_key` | از کجا |
|---|---|---|
| اتصال bundled پکیج | `conn:package_connection:{package_connection_id}` | `id` همان ردیف `PackageConnection` در لحظه‌ی ثبت |
| اتصال صریح درخواست (pending با `node_id`، یا لیست `connections` بدنه) | `conn:request_slot:{index}` | index در لیست canonical اتصال‌های intent (از صفر)؛ این لیست داخل `immutable_intent_hash` است |

- دو spec کاملاً یکسان دو slot مستقل دارند (دو `package_connection_id` متفاوت، یا دو index متفاوت).
- slot از ثبت تا provisioning و تا `record_effect` **صریحاً پاس داده می‌شود**: در مسیر approval، تابع provisioning لیست `(slot_key, node_id, protocol, flow)` را از manifest می‌گیرد و هر Connection ساخته‌شده را با `slot_key` خودش برمی‌گرداند. `Package.connections` بدون `order_by` (۳.۳) دیگر روی صحت اثر ندارد.
- شکست یک slot هویت بقیه را عوض نمی‌کند: slot شکست‌خورده در `missing_effects` با همان کلید خودش می‌آید.
- پیش‌شرط اجرا برای اتصال bundled: همان مجموعه‌ی `package_connection_id` با همان `(node_id, protocol, flow)`.

ردیف‌های شرطی (همه با کلید ثابت):

| ردیف | در manifest و `required` وقتی | در manifest و `optional` وقتی |
|---|---|---|
| `ledger_credit_spend:credit_spend` | مالک approval ادمین پیش‌پرداخت غیر-superadmin است (همان predicate که `admin_billing` برای کسر اعتبار به کار می‌برد) | — |
| `card_payment_recorded:card_payment` | `payment_card_id_snapshot` پر، مبلغ مثبت، pool در `event_logged` | — |
| `discount_redeemed:discount:{code_id}` | — | pending کد تخفیف دارد (redeem امروز best-effort است) |
| `wallet_credit_source_created:credit:referral_reward:referrer` | referral معتبر و مقدار منجمد اعتبار معرف > 0 | — |
| `wallet_credit_source_created:credit:referral_reward:new_user` | referral معتبر و مقدار منجمد اعتبار کاربر جدید > 0 | — |
| `quota_reward_granted:quota:referral:referrer` | referral معتبر، مقدار منجمد حجم معرف > 0، و `resolve_quota_reward_target(معرف)` یک مقصد یکتا و محدود می‌دهد (بخش ۱۴.۱) | — |
| `quota_reward_granted:quota:referral:new_user` | referral معتبر، مقدار منجمد حجم کاربر جدید > 0، و quota پکیج محدود است | — |
| `loyalty_purchase_recorded:loyalty_purchase` | فقط وقتی `timing_mode='payment_event'` (بعد از P9a)، `kind` یکی از `new`/`renew`، و مبلغ نهایی > 0 (پس مدرک `receipt_approval` ساخته خواهد شد؛ بخش ۱۴.۲). مبلغ صفر (مثلاً تخفیف ۱۰۰٪) → ردیف در manifest نیست | — |
| `wallet_credit_source_created:credit:loyalty:{epoch_id}:{slot}` | فقط بعد از P9a، و فقط اگر پیش‌بینی ثبت می‌گوید همین خرید slot `s` از epoch باز را می‌سازد و مقدار اعتبار epoch > 0 (بخش ۱۴.۲) | — |
| `quota_reward_granted:quota:loyalty:{epoch_id}:{slot}` | همان شرط، مقدار حجم epoch > 0، و resolver مقصد (۱۴.۱) یک مقصد یکتا و محدود می‌دهد | — |

`effect_type` `ledger_topup` (منبع `LedgerEntry`، کلید `topup`) در فهرست بخش ۵.۲ هست.

**Schema `expected` (برای هر ردیف دقیق؛ نه یک allowlist عمومی).** validator مجموعه‌ی کلیدها را **دقیقاً** برابر جدول می‌خواهد (کلید اضافه یا کم → رد) و نوع هر مقدار را می‌سنجد. `manifest_hash` از JSON canonical همین ردیف‌ها ساخته می‌شود: کلیدها مرتب، بدون فاصله، اعداد صحیح؛ ردیف‌ها به ترتیب `(effect_type, effect_key)`. پس ترتیب کلید در ورودی hash را عوض نمی‌کند. هیچ credential در هیچ schema نیست.

| `effect_type : effect_key` | کلیدهای `expected` |
|---|---|
| `user_created:user` | `username`:str، `telegram_id`:int، `owner_admin_id`:int\|null، `package_id`:int، `quota_bytes`:int، `days`:int\|null |
| `purchase_created:purchase` | `user`:**binding**، `package_id`:int، `quota_bytes`:int، `days`:int\|null |
| `purchase_renewed:renew` | `user_id`:int، `purchase_id`:int\|null، `package_id`:int، `add_quota_bytes`:int، `add_days`:int |
| `connection_created:conn:...` | `user`:**binding**، `node_id`:int، `protocol`:str، `flow`:str |
| `ledger_sale:sale` | `user`:**binding**، `ledger_kind`:str، `amount`:int، `payment_card_id`:int\|null |
| `ledger_topup:topup` | `user_id`:int، `amount`:int، `payment_card_id`:int\|null |
| `ledger_credit_spend:credit_spend` | `admin_id`:int، `amount`:int |
| `card_payment_recorded:card_payment` | `card_id`:int، `amount`:int |
| `discount_redeemed:discount:{code_id}` | `user`:**binding**، `code_id`:int، `discount_amount`:int |
| `wallet_credit_source_created:credit:receipt_topup` | `user_id`:int، `amount`:int |
| `wallet_credit_source_created:credit:referral_reward:referrer` | `referrer_user_id`:int، `referrer_username`:str، `amount`:int |
| `wallet_credit_source_created:credit:referral_reward:new_user` | `user`:**binding**، `amount`:int |
| `quota_reward_granted:quota:referral:referrer` | `target`:**binding**، `bytes`:int |
| `quota_reward_granted:quota:referral:new_user` | `target`:**binding**، `bytes`:int |
| `wallet_credit_source_created:credit:loyalty:{epoch_id}:{slot}` | `user`:**binding**، `epoch_id`:int، `slot`:int، `amount`:int |
| `quota_reward_granted:quota:loyalty:{epoch_id}:{slot}` | `target`:**binding**، `epoch_id`:int، `slot`:int، `bytes`:int |
| `loyalty_purchase_recorded:loyalty_purchase` | `user`:**binding**، `eligibility_reason`:str |

**Binding.** هر مقدار binding دقیقاً یکی از این دو شکل است؛ مقدار ساختگی، صفر یا `null` مجاز نیست:

| شکل | معنا | کی |
|---|---|---|
| `{"type": "User"\|"Purchase", "id": int}` | منبع موجود در لحظه‌ی ثبت | target موجود (`existing_user`، renew، معرف) |
| `{"ref": "effect:user_created:user"}` یا `{"ref": "effect:purchase_created:purchase"}` | منبعی که همین approval می‌سازد | `new_user`، و Purchase تازه‌ی `existing_user` |

`ref` در لحظه‌ی نوشتن effect وابسته، به `(resource_type, resource_id)` همان effect مرجع در همین approval resolve می‌شود. اگر effect مرجع هنوز وجود ندارد، نوشتن رد می‌شود (`effect_manifest_mismatch`)؛ پس ترتیب اجرا هم enforce می‌شود: `user_created` قبل از `purchase_created` و قبل از هر پاداش.

**Enforcement مقادیر (نه فقط کلیدها).** برای هر effect، backend یک `actual_projection` با **همان کلیدهای** ردیف `expected` می‌سازد. ورودی آن دو چیز است: شیء منبع، و یک **evidence تایپ‌شده** که سرویس مالک mutation ساخته. ردیف نهایی به‌تنهایی کافی نیست: مقدار قبل از mutation (quota، شمارنده، موجودی) و delta اعمال‌شده از آن قابل بازسازی نیست، و حدس یا تقسیم مقدار نهایی مجاز نیست.

**قرارداد evidence:**

```
before   = سرویس مالک، قبل از mutation، مقادیر لازم را از منبع می‌خواند (زیر همان قفل/تراکنش)
mutation ؛ flush
evidence = EvidenceX(before..., after...)        -- frozen dataclass ؛ after از همان شیء بعد از flush
record_effect(db, approval, effect_type, effect_key, resource, evidence)
```

- هر سه قدم در **همان تراکنش** هستند.
- evidence فقط داخل ماژول سرویس مالک همان mutation ساخته می‌شود. router، bridge و بدنه‌ی درخواست هیچ راهی برای فرستادن evidence، projection یا snapshot ندارند؛ تست AST (RV-265) ساخت کلاس‌های evidence را بیرون از ماژول‌های مجاز رد می‌کند.
- validator هر evidence سازگاری درونی آن را می‌سنجد و در ناسازگاری تراکنش را rollback می‌کند.

| effect | Evidence و فیلدها | validator | projection از |
|---|---|---|---|
| `user_created` | `UserCreatedEvidence(package_id, quota_bytes, days)` | مقادیر برابر آنچه روی ردیف `User` نوشته شد | `User` + evidence |
| `purchase_created` | `PurchaseCreatedEvidence(days)` | `Purchase.expire_at` با `days` سازگار | `Purchase` (`user_id`، `package_id`، `quota_bytes`) + evidence |
| `purchase_renewed` | `PurchaseRenewedEvidence` (جدول «تمدید» پایین) | state table همان جدول | `add_quota_bytes`، `add_days`، `package_id` از evidence معتبر؛ `user_id`/`purchase_id` از منبع |
| `connection_created` | `ConnectionCreatedEvidence(slot_key)` | `slot_key` برابر `effect_key` | `Connection` (`user_id`، `node_id`، `type`، `flow`) |
| `ledger_sale`، `ledger_topup`، `ledger_credit_spend` | بدون evidence (ردیف Ledger immutable و کامل است) | — | `LedgerEntry` |
| `card_payment_recorded` | بدون evidence | — | `payment_card_pool_events` |
| `discount_redeemed` | `DiscountEvidence(used_count_before, used_count_after)` | `after = before + 1` | `DiscountCodeRedemption` |
| `wallet_credit_source_created` (قبل از cutover) | `WalletCreditEvidence(balance_before, balance_after, rewards_count)` | `balance_after > balance_before`؛ `rewards_count >= 1`؛ `(after - before) % rewards_count = 0` | کاربر هدف + `amount = after - before` (برای loyalty `/ rewards_count`) |
| `wallet_credit_source_created` (بعد از cutover) | `WalletCreditEvidence(rewards_count)` | `gross_amount % rewards_count = 0` | `wallet_credit_sources` |
| `quota_reward_granted` | `QuotaRewardEvidence(target_type, target_id, quota_before, quota_after, before_was_unlimited, rewards_count)` | `quota_after > quota_before`؛ `before_was_unlimited = (quota_before = 0)`؛ تقسیم‌پذیری بر `rewards_count`؛ `(target_type, target_id)` برابر منبع داده‌شده | منبع + `bytes = quota_after - quota_before` |
| `loyalty_purchase_recorded` | `LoyaltyPurchaseEvidence(active_count_before, active_count_after, rewards_live_before, rewards_live_after)`، خوانده‌شده زیر قفل ردیف User | `active_count_after = before + 1`؛ `rewards_live_after >= before` | ردیف `loyalty_purchase_events` (`user_id`، `eligibility_reason`) |

**تمدید (`purchase_renewed`)، برای User و Purchase یکسان.** ستون‌ها: `q` = `total_quota_bytes`/`quota_bytes`، `u` = `used_bytes`، `e` = `expire_at`، `rq`/`rd`/`rp` = `reserved_quota_bytes`/`reserved_duration_days`/`reserved_package_id`، `pk` = `package_id`.

```
PurchaseRenewedEvidence(
  renewal_mode,            -- 'reserved' | 'immediate_reset'
  effective_now,           -- همان timestamp که سرویس برای تصمیم و برای expire_after به کار برد
  add_quota_bytes, add_days, package_id_requested,     -- ورودی واقعی تابع تمدید
  q_before, u_before, e_before, rq_before, rd_before, rp_before, pk_before,
  q_after,  u_after,  e_after,  rq_after,  rd_after,  rp_after,  pk_after)
```

validator اول mode را از مقادیر before دوباره حساب می‌کند و باید با `renewal_mode` برابر باشد:

```
has_limits = (q_before > 0) OR (e_before IS NOT NULL)
quota_ok   = (q_before = 0) OR (u_before < q_before)
expiry_ok  = (e_before IS NULL) OR (e_before > effective_now)
mode       = 'reserved' اگر has_limits AND quota_ok AND expiry_ok ؛ وگرنه 'immediate_reset'
```

| ستون | `reserved` | `immediate_reset` |
|---|---|---|
| `q_after` | `= q_before` | `= add_quota_bytes` اگر `add_quota_bytes > 0`؛ وگرنه `= q_before` |
| `u_after` | `= u_before` | `= 0` |
| `e_after` | `= e_before` | `= effective_now + add_days` اگر `add_days > 0`؛ وگرنه `= e_before` |
| `rq_after` | `= coalesce(rq_before, 0) + add_quota_bytes` | `= rq_before` |
| `rd_after` | `= coalesce(rd_before, 0) + add_days` | `= rd_before` |
| `rp_after` | `= package_id_requested` اگر داده شده؛ وگرنه `= rp_before` | `= rp_before` |
| `pk_after` | `= pk_before` | `= package_id_requested` اگر داده شده؛ وگرنه `= pk_before` |

- `add_quota_bytes = 0` و `add_days = 0` تمدید فروش نیست (reset مصرف): ثبت approval با چنین intent ۴۲۲ می‌گیرد و validator هم آن را رد می‌کند.
- فقط حجم (`add_days = 0`) و فقط روز (`add_quota_bytes = 0`) هر دو در هر دو mode معتبرند؛ ستون بُعد صفر طبق جدول بدون تغییر می‌ماند.
- منبع بدون هیچ حد (`q_before = 0` و `e_before IS NULL`) همیشه `immediate_reset` است؛ با `add_quota_bytes > 0` از نامحدود به محدود می‌رود. این رفتار امروز کد است و همان‌طور ثبت می‌شود.
- quota نامحدود با انقضای معتبر، و انقضای NULL با quota باقی‌مانده، هر دو `reserved`اند.
- مثال: `q_before = 100`، مصرف‌شده، `add_quota_bytes = 50` → `immediate_reset`، `q_after = 50`، `u_after = 0`. اختلاف خام ستون ۵۰- است؛ projection مقدار intent را از `add_quota_bytes` evidence می‌گیرد، نه از اختلاف ستون‌ها.
- projection: `add_quota_bytes`، `add_days` و `package_id` از evidence (بعد از قبول‌شدن validator) و `user_id`/`purchase_id` از منبع.
- هر سطر ناسازگار با mode، یا mode نادرست → `effect_evidence_invalid` و rollback همان تراکنش.
- ابطال یک تمدید از `resource_snapshot` همین evidence می‌خواند که کدام شاخه اجرا شده بود.
- loyalty در هیچ‌کدام از این ستون‌ها نیست؛ در هر دو mode، اگر فروش واجد است، با effect جدای `loyalty_purchase_recorded` در همان تراکنش ثبت می‌شود (بخش ۱۴.۲).

`rewards_count` مقدار واقعی است که سرویس loyalty همان لحظه محاسبه کرده (`due - already`)، نه حدس. فیلدهای before/after هر evidence در `resource_snapshot` effect ذخیره می‌شوند تا ابطال از آن‌ها بخواند.

```
record_effect(db, approval, effect_type, effect_key, resource, evidence):
  row = ردیف manifest با همان (effect_type, effect_key) ؛ نبود → rollback ، effect_not_in_manifest
  evidence از نوع همان effect نیست، یا validator آن رد کرد → rollback ، effect_evidence_invalid
  expected = row.expected با resolve هر binding
  actual   = projection ساخته از (resource, evidence)
  canonical(actual) != canonical(expected) → rollback همان تراکنش mutation ، خطای ثابت effect_manifest_mismatch
  INSERT effect (actual_projection = actual ، resource_snapshot = factهای evidence)
```

- rollback یعنی mutation محلی همان تراکنش هم برمی‌گردد؛ اثر remote قبلی (اگر بود) مسئله‌ی provisioning است (P6).
- ردیف `optional` اگر effect ساخته شود دقیقاً همین مسیر را می‌گذراند.
- برگشت‌های ابطال (بخش‌های ۱۲ و ۱۴) effect approval نیستند و از `record_effect` عبور نمی‌کنند؛ مقدار قبل و بعدشان در ردیف‌های نرمال‌شده‌ی خودشان است (`wallet_*`، `quota_reversal_shortfalls`، `reversed_value`).

**مرجع صحت و نقش `finalize`.** مرجع اصلی صحت همان مقایسه‌ی لحظه‌ی INSERT است، که زیر تراکنش mutation انجام شده. `finalize` بعد از چند commit و بدون قفل آن تراکنش‌ها اجرا می‌شود، پس منبع mutable را دوباره با snapshot اولیه **مقایسه نمی‌کند**:

| گروه منبع | مثال | بررسی در `finalize` و در job تطبیق |
|---|---|---|
| ۱. immutable / قابل بازبینی | `LedgerEntry` (`kind`، `amount`، `payment_card_id`)، `payment_card_pool_events` (`card_id_snapshot`، `amount`)، `wallet_credit_sources` (`gross_amount`، `origin_kind`)، `DiscountCodeRedemption` (`code_id`، `discount_amount`) | فقط همین فیلدهای immutable از منبع زنده با `actual_projection` مقایسه می‌شوند |
| ۲. mutable / فقط snapshot audit | `User`، `Purchase`، `Connection`، quota، شمارنده‌ها، موجودی | هیچ مقایسه‌ای با وضعیت زنده. مصرف quota، تمدید، rename، transfer یا تغییر موجودی بعدی approval را خراب نمی‌کند |
| ۳. حذف‌شده / tombstone | منبعی که دیگر وجود ندارد | هیچ مقایسه‌ای |

`finalize` این‌ها را می‌سنجد: (۱) مجموعه‌ی `(effect_type, effect_key)` در برابر manifest؛ (۲) `actual_projection` **ذخیره‌شده** در برابر `expected` با binding resolve‌شده؛ (۳) فقط برای گروه ۱، فیلدهای immutable زنده. دستکاری `actual_projection` ذخیره‌شده در (۲) دیده می‌شود، چون با manifest نمی‌خواند؛ دستکاری خود manifest با `manifest_hash` روی approval دیده می‌شود. MAC با secret سرور استفاده نمی‌شود، چون نصب بدون `SECRET_KEY` تنظیم‌شده آن را در هر restart عوض می‌کند و همه‌ی ردیف‌های قبلی را نامعتبر نشان می‌داد. job تطبیق همین سه بررسی را دارد و برابری با وضعیت زنده‌ی منبع mutable را invariant نمی‌داند.

**Referral و loyalty در ثبت:** referral فقط برای `target_shape='new_user'`. backend کد دعوت را همان لحظه resolve می‌کند و معرف، مقدار هر پاداش از `PanelSettings` همان لحظه، و مقصد پاداش حجمی هر طرف را منجمد می‌کند. مقصد پاداش حجمی loyalty هم با همان resolver و همان قرارداد binding منجمد می‌شود (بخش ۱۴.۱). کد نامعتبر یا متعلق به خود مشتری: هیچ ردیف referral در manifest نیست و approval بدون referral ادامه می‌یابد (معادل رفتار best-effort امروز). اجرای `apply_referral` همه‌ی این‌ها را از manifest می‌خواند، نه از تنظیمات زنده.

**پیش‌شرط اجرا (قبل از هر mutation محلی و هر فراخوانی remote).** اولین production call هر approval در یک تراکنش، زیر قفل ردیف approval (بخش ۶.۵):

```
1. execution token برابر version فعلی approval ؛ وگرنه 409 approval_superseded
2. پیش‌شرط target (جدول پایین) ؛ هر شکست → قدم ۴ با cancellation_reason همان ردیف
3. مشخصات زنده را با همان تابعی که manifest را ساخت دوباره می‌سازد و با ردیف‌های manifest مقایسه می‌کند:
     پکیج موجود و همان package_id ؛ همان مجموعه‌ی slotها با همان (node_id, protocol, flow) ؛ هر node موجود و فعال ؛
     همان quota/days/مبلغ ؛ معرف منجمد هنوز موجود، مشتری هنوز referred نشده، مقصد پاداش حجمی هر طرف همان ؛
     بعد از P9a: پیش‌بینی loyalty (epoch باز و slot) همان است ؛ وگرنه loyalty_projection_changed
4. هر اختلاف → approval.state = 'cancelled' ، cancellation_reason ، COMMIT ، 409 manifest_precondition_failed با فهرست اختلاف‌ها
5. بدون اختلاف → approval.state = 'mutating' ، COMMIT
```

| `target_shape` | پیش‌شرط target (همه قبل از `state='mutating'`) | `cancellation_reason` |
|---|---|---|
| `new_user` | هیچ User با `target_username` وجود ندارد | `username_taken` |
| `existing_user` | User با `target_user_id_snapshot` وجود دارد؛ `username`، `owner_admin_id`، `tenant_scope_key` و `telegram_id` آن برابر snapshot | `target_user_changed` |
| `renew_purchase` | مثل `existing_user`؛ و Purchase با `renew_purchase_id` وجود دارد، متعلق به همان User است و `package_id` آن همان است که manifest انتظار دارد | `target_purchase_changed` |
| `renew_user` | مثل `existing_user`؛ و User هنوز همان شکل legacy را دارد که ثبت دید (تعداد Purchaseهایش همان است، پس تمدید سطح User هنوز همان هدف را می‌زند) | `target_shape_changed` |
| `topup` | مثل `existing_user`؛ و بعد از cutover: `wallet_account` زنده (غیر-tombstone) همان User وجود دارد | `target_user_changed` |
| همه | `tenant_scope_key` فعلی target برابر `tenant_scope_key` approval | `target_tenant_changed` |

پیش‌شرط پنجره‌ی رقابت را کوچک می‌کند ولی نمی‌بندد: بین قدم ۵ و اجرای واقعی هنوز ممکن است target عوض شود (مثلاً username هم‌زمان گرفته شود). بستن آن وظیفه‌ی state machine provisioning (P6) است: شکست UNIQUE یا قفل ردیف در آن‌جا باید قبل از هر فراخوانی remote رخ دهد یا compensation بدون orphan داشته باشد. تا P6، این شکست‌ها همان رفتار امروز توابع خرید را دارند.

بعد از قدم ۵، provisioning **فقط** از مشخصات manifest اجرا می‌شود؛ توابع خرید در حضور `approval_uuid` ردیف‌های زنده‌ی `PackageConnection` را نمی‌خوانند. پس تغییر پکیج بعد از قدم ۵ روی این اجرا اثری ندارد و effectها همیشه با manifest می‌خوانند. بعد از `cancelled`، pending به صف دستی برمی‌گردد و ثبت دوباره طبق بخش ۵.۱ انجام می‌شود (`registration_seq + 1`، manifest تازه، همان intent).

### ۵.۴ سقف یک تأیید خودکار در ۶۰ دقیقه

**Policy:**

- subject = `(tenant_scope_key, telegram_id_snapshot)`. دو User با Telegram یکسان در یک tenant یک subject‌اند؛ همان Telegram در دو tenant دو subject جدا.
- kindهای واجد تأیید خودکار فقط `new` و `renew` هستند و سقف بین این دو مشترک است. `topup` واجد نیست: مسیر auto آن را با `auto_approval_policy_denied` (دلیل `kind_not_auto_eligible`) رد می‌کند، هیچ سهمیه‌ای مصرف نمی‌شود و رسید به صف دستی می‌رود. فعال‌شدن تأیید خودکار topup یک policy جدای مبلغ/کارت/ریسک می‌خواهد و در این سند نیست.
- برای بات مشترک، اختصاصی و remote یکسان است، چون فقط backend مرکزی تصمیم می‌گیرد.
- تأیید دستی محدود نمی‌شود و سهمیه مصرف نمی‌کند.
- retry همان pending سهمیه‌ی جدید مصرف نمی‌کند.
- void، refund، شکست بعد از effect، و شکست قبل از هر mutation هیچ‌کدام سهمیه را آزاد نمی‌کنند؛ درخواست بعدی فقط می‌تواند دستی بررسی شود.

```
auto_approval_subjects
tenant_scope_key     VARCHAR(64) NOT NULL
telegram_id          BIGINT      NOT NULL
last_granted_at      DATETIME(6) NOT NULL
last_approval_uuid   CHAR(36)    NOT NULL
version              INT         NOT NULL DEFAULT 0
PRIMARY KEY (tenant_scope_key, telegram_id)

auto_approval_rate_events                 -- log immutable؛ بدون username و بدون مبلغ
id                 PK
tenant_scope_key   VARCHAR(64) NOT NULL
telegram_id        BIGINT      NOT NULL
outcome            VARCHAR(20) NOT NULL  CHECK IN ('granted','rate_limited','shadow_would_limit','policy_denied')
reason_code        VARCHAR(32) NULL      -- reason evaluator مرکزی
local_decision     VARCHAR(16) NULL      CHECK IN ('allowed','denied')   -- diagnostic بات؛ بدون اثر
approval_uuid      CHAR(36)    NULL
pending_source_instance_id  VARCHAR(64) NOT NULL
pending_local_id   BIGINT      NOT NULL
retry_after_seconds INT        NULL
created_at         NOT NULL                -- DB_NOW

INDEX (tenant_scope_key, telegram_id, created_at)
```

**الگوریتم `POST .../receipt-approvals/auto`:**

```
1. (بدون قفل) idempotency همان pending طبق ۵.۱. اگر ردیف هست، همان‌جا پاسخ داده می‌شود و هیچ grantی مصرف نمی‌شود.
2. backend evaluator مرکزی (پایین) را روی intent اجرا می‌کند. رد → 403 `auto_approval_policy_denied` با reason code
   + ردیف event `policy_denied`. هیچ ردیف approval ساخته نمی‌شود و هیچ سهمیه‌ای مصرف نمی‌شود.
3. tx:
     MariaDB:  SELECT ... FROM auto_approval_subjects WHERE (tenant_scope_key, telegram_id) = (:t, :g) FOR UPDATE
     SQLite:   BEGIN IMMEDIATE ؛ سپس همان SELECT
     now = DB_NOW
     اگر ردیف هست و last_granted_at > now - 60 minutes:
         retry_after = ceil(last_granted_at + 3600s - now)
         اگر enforcement روشن: INSERT event(rate_limited) ؛ COMMIT ؛ 429
         اگر shadow:          INSERT event(shadow_would_limit) ؛ ادامه
     idempotency قدم ۱ دوباره داخل تراکنش بررسی می‌شود
     INSERT receipt_approvals(approval_mode='auto', auto_granted_at = now, ...) + manifest
     اگر ردیف subject نبود: INSERT(last_granted_at = now, last_approval_uuid)
         IntegrityError (رقیب هم‌زمان ساخت) → ROLLBACK و اجرای دوباره‌ی قدم ۳
     وگرنه: UPDATE last_granted_at = now, last_approval_uuid, version + 1
     INSERT event(granted)
   COMMIT → 201
```

- زمان فقط `DB_NOW` است. مرز: `last_granted_at > now - 60min` محدود می‌کند؛ دقیقاً در ۶۰ دقیقه مجاز است.
- ثبت approval و به‌روزرسانی subject یک commit‌اند، پس دو درخواست هم‌زمان یک subject هر دو موفق نمی‌شوند: دومی پشت قفل ردیف می‌ماند و سپس `last_granted_at` تازه را می‌بیند.
- قفل فقط ردیف همان subject است. در MariaDB subjectهای مختلف هم‌زمان پیش می‌روند؛ در SQLite نوشتن‌ها ذاتاً سریال‌اند.
- پاسخ ۴۲۹: `{"error": "auto_approval_rate_limited", "retry_after_seconds": N}` و header `Retry-After`. هیچ اطلاعی از approval قبلی، username یا tenant دیگر در پاسخ نیست.
- تصمیم در هیچ sqlite محلی بات یا حافظه‌ی process نگه داشته نمی‌شود.

**Evaluator مرکزی.** یک تابع pure: `evaluate(intent, settings, history, now) → (allowed, reason_code)`. هیچ I/O ندارد و `auto_approve.decide` یا `_is_returning` فعلی را صدا نمی‌زند. ورودی‌هایش را endpoint از دیتابیس **مرکزی** می‌خواند:

| شرط | منبع | رد با |
|---|---|---|
| kind واجد | `intent.kind IN ('new','renew')` | `kind_not_auto_eligible` |
| روشن‌بودن، بازه‌ی ساعت، سقف مبلغ | `BotSettings` یا override مالک (`AdminUser.own_auto_approve_*`)، همان قاعده‌ی انتخاب امروز؛ ساعت از `DB_NOW` | `disabled` / `outside_window` / `over_cap` |
| هویت | `telegram_id_snapshot` مشتق‌شده غیر-NULL | `no_telegram_identity` |
| «فقط مشتری قبلی» | `history.returning` (پایین) | `not_returning` / `returning_unknown` |

`history.returning` برای subject `(tenant_scope_key, telegram_id)`:

```
returning = True  اگر یکی برقرار باشد:
  (الف) یک receipt_approvals دیگر با همان subject، kind IN ('new','renew')، state = 'completed'
  (ب)   یک LedgerEntry با kind IN ('sale_new','sale_renew')، voided_at IS NULL،
        و مدرک کارت: payment_method = 'card'  یا  payment_card_id IS NOT NULL،
        که user_id آن به Userی با همان telegram_id و همان tenant_scope_key اشاره می‌کند
returning = unknown اگر (الف) و (ب) برقرار نیستند ولی برای آن Telegram یکی از این‌ها پیدا شود:
  فروشی با payment_method = NULL و payment_card_id = NULL (مدرک کارت ندارد)،
  یا فروش کارتی با user_id = NULL (User حذف‌شده) یا با tenant نامعلوم
returning = False در غیر این صورت
```

- approval `voided` سابقه حساب نمی‌شود.
- `unknown` رد می‌شود (`returning_unknown`) و رسید دستی بررسی می‌شود. caller هیچ boolean «مشتری قبلی» نمی‌فرستد؛ اگر بفرستد نادیده گرفته می‌شود.
- `payment_method = NULL` در Ledger قدیمی وجود دارد (`services/accounting.py:411-419` آن را برای محاسبه‌ی نقد «کیف‌پول نیست» حساب می‌کند). این‌جا بدون مدرک کارت، خرید کارتی فرض **نمی‌شود**.
- **مهاجرت:** شاخه‌ی (ب) بخشی از مشتریان قدیمی را می‌پوشاند، نه همه را. مشتریانی که تنها سابقه‌شان فروش بدون مدرک کارت، یا فقط در sqlite محلی یک بات است، بعد از deploy `returning_unknown` یا `not_returning` می‌شوند و دستی بررسی می‌شوند. تاریخچه‌ی sqlite محلی بات‌ها import نمی‌شود.
- **Shadow (P3):** برای هر درخواست، تصمیم محلی بات و نتیجه‌ی evaluator مرکزی هر دو در `auto_approval_rate_events` ثبت می‌شوند. گزارش shadow تعداد این‌ها را می‌دهد: «محلی returning، مرکزی unknown» و «محلی returning، مرکزی not_returning» (false-negativeهای مهاجرت)، و برعکس. enforcement (P9a) فقط بعد از دیدن این اعداد روشن می‌شود.

**بات فقط پرسنده است.** برای هر pending با kind `new` یا `renew`، بات **همیشه** `.../receipt-approvals/auto` را صدا می‌زند. در mode مؤثر `required`، `auto_approve.decide` و `_is_returning` محلی gate نیستند و فقط backend پاسخ allowed/denied می‌دهد. در `off` و `shadow` تصمیم محلی مثل امروز مرجع می‌ماند و نتیجه‌ی مرکزی فقط log می‌شود (بخش ۵.۶)، چون shadow نباید چیزی را gate کند. تصمیم محلی حداکثر به‌عنوان فیلد diagnostic همراه درخواست فرستاده و log می‌شود؛ اختلافش با backend هیچ اثری بر نتیجه ندارد. پس بات remote با sqlite محلی خالی، برای مشتری‌ای که سابقه‌ی مرکزی دارد، تأیید خودکار می‌گیرد.

**دورزدن با `manual` بسته است:** mode از endpoint و capability می‌آید، نه از یک فیلد بدنه. مسیر manual فقط با مدرک approver معتبر پذیرفته می‌شود (بخش ۶.۴).

**رفتار بات:** با ۴۲۹ (یا ۴۰۳ policy) pending محلی `pending` می‌ماند و وارد صف بررسی دستی می‌شود؛ رسید حذف یا reject نمی‌شود. مشتری پیام عمومی «درخواست برای بررسی دستی ثبت شد» می‌گیرد. اعلان ادمین `retry_after_seconds` را به‌صورت زمان باقی‌مانده نشان می‌دهد؛ شمارش معکوس فقط نمایشی است.

**Seed هنگام روشن‌شدن enforcement:** یک تراکنش، برای هر subject: `last_granted_at = MAX(receipt_approvals.auto_granted_at)` در ۶۰ دقیقه‌ی اخیر. idempotent است. خاموش‌کردن flag فقط شاخه‌ی ۴۲۹ را غیرفعال می‌کند؛ `auto_granted_at`، subjectها و eventها نوشته می‌شوند و می‌مانند.

### ۵.۵ `receipt_approval_runtime_state` و آمادگی

```
receipt_approval_runtime_state
id                       INT         NOT NULL PRIMARY KEY   CHECK (id = 1)
registration_mode        VARCHAR(10) NOT NULL DEFAULT 'off'  CHECK IN ('off','shadow','required')   -- mode درخواست‌شده‌ی اپراتور
auto_rate_limit_mode     VARCHAR(10) NOT NULL DEFAULT 'off'  CHECK IN ('off','shadow','enforced')   -- mode درخواست‌شده‌ی اپراتور
loyalty_timing_mode      VARCHAR(20) NOT NULL DEFAULT 'legacy_activation'  CHECK IN ('legacy_activation','payment_event')
completeness_generation  INT         NOT NULL DEFAULT 0     -- با هر activation مشترک +۱ ؛ هرگز کم نمی‌شود
activated_at             DATETIME(6) NULL
key_identity_ready       BOOLEAN     NOT NULL DEFAULT 0
key_identity_checked_at  DATETIME(6) NULL
key_identity_failure     VARCHAR(40) NULL
key_identity_failed_at   DATETIME(6) NULL
updated_at               NOT NULL
version

CHECK (auto_rate_limit_mode <> 'enforced' OR registration_mode = 'required')
CHECK ((key_identity_ready = 1) = (key_identity_failure IS NULL))
CHECK ((registration_mode = 'required') = (loyalty_timing_mode = 'payment_event'))
CHECK (registration_mode <> 'required' OR completeness_generation >= 1)
```

**`required` و loyalty `payment_event` یک سوئیچ‌اند.** هر دو در همین یک ردیف نگه داشته می‌شوند و CHECK سوم اجازه نمی‌دهد یکی بدون دیگری باشد. دلیل: کد loyalty امروز (۳.۳) در ساخت User، تمدید فوری، activation رزرو و `apply_package_as_purchase` موجودی، quota و شمارنده‌ها را عوض می‌کند، بدون source، رویداد یا effect. اگر approval `required` با آن کد اجرا شود، اثر خارج از manifest می‌سازد و بعداً همان approval قابل ابطال می‌شود درحالی‌که پاداشش داخل baseline رفته و قابل پس‌گرفتن نیست. پس هیچ approval `required` هرگز با loyalty legacy اجرا نمی‌شود.

**Activation مشترک (فاز P9a)**، `POST .../runtime-mode/activate` (superadmin + رمز):

```
پیش‌شرط‌ها، همه داخل همان تراکنش دوباره بررسی می‌شوند:
  1. wallet_runtime_state.phase = 'enforced'                       (P9 انجام شده)
  2. key_identity_ready = 1 ، با محاسبه‌ی تازه
  3. ha_enabled = False یا external_fencing = 'coordinator'        (بخش ۲۰.۴)
  4. هیچ User و هیچ Purchase رزرو فعال ندارد                        (بخش ۱۴.۲)
  5. هر کلید managed فعال، approval_protocol_version >= نسخه‌ی لازم را گزارش کرده (پایین)
  هر شکست → ROLLBACK ، 409 با کد همان پیش‌شرط و فهرست موارد ؛ هیچ تغییری

ترتیب قفل (همه‌ی writerها هم به همین ترتیب می‌خوانند):
  wallet_runtime_state (locking read مشترک) → receipt_approval_runtime_state (انحصاری)

tx واحد:
  generation = completeness_generation + 1
  INSERT loyalty_policy_epochs (epoch اول این generation از تنظیمات فعلی)
  برای هر User با شمارنده‌ی غیرصفر: INSERT loyalty_user_baselines(generation) و anchor epoch اول (بخش ۱۴.۲)
  UPDATE receipt_approval_runtime_state
     SET registration_mode = 'required', loyalty_timing_mode = 'payment_event',
         completeness_generation = generation, activated_at = now, version = version + 1
COMMIT
```

- هر دو mode در یک UPDATE روی یک ردیف عوض می‌شوند؛ حالت «یکی روشن، دیگری نه» وجود ندارد و crash وسط کار یعنی rollback کامل همان تراکنش.
- **retry:** اگر ردیف از قبل `required`/`payment_event` است → ۲۰۰ بدون تغییر؛ generation دوباره زیاد نمی‌شود.
- **approvalهای shadow در جریان** در اولین mutation بعدی ۴۰۹ `approval_mode_changed` می‌گیرند و لغو می‌شوند (بخش ۵.۶).
- **نسخه‌ی پروتکل بات:** `api_keys` یک ستون nullable دیگر می‌گیرد، `approval_protocol_version INT NULL`، که هر فراخوانی ثبت (از جمله در shadow) آن را به‌روز می‌کند. کلید managed فعالی که نسخه‌ی کافی گزارش نکرده activation را متوقف می‌کند؛ اپراتور یا آن نصب را به‌روز می‌کند یا کلیدش را غیرفعال.

شمارش دوم هر خرید واجدی را می‌بیند، چه پاداش ساخته باشد چه نه، و چه approval داشته باشد چه نه (خرید کیف‌پولی). `loyalty_reward_events.reearn_generation` معنای دیگری دارد (شماره‌ی بازکسب یک slot) و در این شرط هیچ نقشی ندارد.

**epoch و نسل:** هر `loyalty_policy_epochs` ستون `generation` همان activation را دارد. deactivate epoch باز را می‌بندد؛ activation بعدی epoch اول نسل تازه را می‌سازد. invariant که job تطبیق می‌سنجد: اگر `loyalty_timing_mode='payment_event'`، دقیقاً یک epoch با `closed_at IS NULL` وجود دارد و `generation` آن برابر `completeness_generation` جاری است؛ اگر `legacy_activation`، هیچ epoch بازی وجود ندارد.

**نسل کامل‌بودن effect.** هر approval `completeness_generation` لحظه‌ی ثبتش را نگه می‌دارد: ثبت shadow صفر، ثبت `required` نسل جاری. ابطال رسید (P11) فقط approval با `completeness_generation >= 1` را می‌پذیرد؛ برای بقیه، preview و execute ۴۰۹ `approval_not_voidable_generation` می‌دهند و دکمه‌ی UI غیرفعال است. فقط برای این approvalها تضمین شده که هر تغییر موجودی و quota همان mutation داخل manifest است.

**Rollback activation:**

| وضعیت | رفتار |
|---|---|
| `COUNT(receipt_approvals WHERE completeness_generation = جاری) = 0` **و** `COUNT(loyalty_purchase_events WHERE completeness_generation = جاری) = 0` (هر دو query در همان تراکنش، زیر قفل انحصاری runtime state) | یک تراکنش: `registration_mode='shadow'`، `loyalty_timing_mode='legacy_activation'`، `auto_rate_limit_mode` حداکثر `shadow`؛ epoch باز نسل جاری `closed_at = now` می‌گیرد. `completeness_generation` کم نمی‌شود؛ activation بعدی نسل بعدی با baseline و epoch تازه می‌سازد |
| حتی یکی از آن دو شمارش غیرصفر است | ۴۰۹ `activation_irreversible`. از آن پس فقط `blocked` (به‌خاطر آمادگی کلید) ممکن است، نه برگشت به shadow |

**هیچ CHECKی mode را به آمادگی وابسته نمی‌کند.** چنین قیدی نوشتن `key_identity_ready = 0` را وقتی mode `required` است رد می‌کرد و شکست آمادگی دیگر قابل ثبت نبود. به‌جای آن، mode **مؤثر** مشتق می‌شود:

| `registration_mode` (درخواست‌شده) | `key_identity_ready` | mode مؤثر |
|---|---|---|
| `off` | هر مقدار | `off` |
| `shadow` | 1 | `shadow` |
| `shadow` | 0 | `shadow` با خطای shadow (بخش ۵.۶) |
| `required` | 1 | `required` |
| `required` | 0 | **`blocked`** |

- `required` هرگز خودکار به `shadow` یا `off` پایین نمی‌آید؛ آن fail-open می‌بود. فقط اپراتور mode درخواست‌شده را عوض می‌کند.
- در `blocked`: هر endpoint ثبت، اجرا، `finalize`، و هر mutation که approval می‌خواهد ۵۰۳ `receipt_approval_blocked` می‌دهد؛ هیچ مسیر legacy اجرا نمی‌شود و مسیر auto هیچ grantی نمی‌دهد (`auto_granted_at` نوشته نمی‌شود).
- سقف خودکار مؤثر: `enforced` فقط وقتی mode مؤثر ثبت `required` است؛ در `blocked` هیچ grantی وجود ندارد که محدود شود.
- guard هر درخواست وضعیت را از همین ردیف durable می‌خواند (locking read، مثل بخش ۹.۲)، نه از حافظه‌ی process.

**محاسبه‌ی آمادگی** در هر startup (بعد از migration)، درست قبل از هر تغییر mode، و با درخواست اپراتور (`POST .../runtime-mode/recheck`):

```
tx الف (فقط خواندن و backfill):
  1. inspector: index با نام uq_api_keys_key_instance_uuid روی api_keys وجود دارد و unique است ؛ وگرنه failure = 'unique_index_missing'
  2. backfill: برای هر ردیف با key_instance_uuid IS NULL یک UUID تازه
  3. SELECT COUNT(*) WHERE key_instance_uuid IS NULL = 0 ؛ وگرنه failure = 'null_uuid_remaining'
  4. SELECT ... GROUP BY key_instance_uuid HAVING COUNT(*) > 1 خالی است ؛ وگرنه failure = 'duplicate_uuid'
  خطای غیرمنتظره در هر قدم → failure = 'check_error'
tx ب (جدا، فقط همین یک UPDATE، بدون وابستگی به mode):
  UPDATE receipt_approval_runtime_state
     SET key_identity_ready = (failure IS NULL), key_identity_failure = failure,
         key_identity_failed_at = (now اگر failure), key_identity_checked_at = now, version = version + 1
```

- tx ب هیچ قیدی ندارد که به mode بستگی داشته باشد، پس نتیجه‌ی شکست همیشه durable می‌شود، حتی وقتی mode `required` است. اگر خود tx ب شکست بخورد، process آن را با `key_identity_ready = 0` در حافظه **و** تلاش دوباره‌ی نوشتن در درخواست بعدی جبران می‌کند؛ guard وقتی ردیف قابل خواندن نیست هم ۵۰۳ می‌دهد.
- بازگشت `blocked → required` فقط وقتی است که یک محاسبه‌ی آمادگی کامل موفق شود و tx ب آن commit شود (در startup بعدی یا با `recheck` اپراتور). mode درخواست‌شده در این مدت همان `required` مانده، پس چیزی برای «روشن‌کردن دوباره» لازم نیست و هیچ پنجره‌ی shadow وجود ندارد.
- warning migration به‌تنهایی هیچ‌چیز را فعال نمی‌کند؛ فقط نتیجه‌ی این چهار قدم.
- هر درخواست علاوه بر این، UUID کلید خودش را می‌خواند؛ NULL → ۴۰۳.
- UI سه چیز را جدا نشان می‌دهد: mode درخواست‌شده، mode مؤثر، و `key_identity_failure` با زمانش.

**تغییر mode درخواست‌شده** فقط از endpointهای superadmin (بخش ۱۸):

- `off ↔ shadow` و `auto_rate_limit_mode` در محدوده‌ی `off`/`shadow`: `PUT runtime-mode`.
- رفتن به `required`: **فقط** با activation مشترک بالا. `PUT runtime-mode` با `registration_mode='required'` همیشه ۴۰۹ `use_joint_activation` می‌دهد.
- `auto_rate_limit_mode='enforced'` (P9b): فقط بعد از activation، با پیش‌شرط HA.

### ۵.۶ قرارداد off / shadow / required برای bridge و handler

فقط backend mode مؤثر را تعیین می‌کند. بات همیشه endpoint ثبت را صدا می‌زند و از پاسخ ساخت‌یافته می‌فهمد چه کند؛ هیچ فیلدی در درخواست نمی‌تواند mode را ادعا کند.

| mode مؤثر | پاسخ ثبت | مرجع تصمیم تأیید خودکار | production call |
|---|---|---|---|
| `off` | 200 `{mode:"off", approval_uuid:null, proceed_legacy:true}`؛ چیزی ثبت نمی‌شود | `auto_approve.decide` محلی، مثل امروز | بدون `approval_uuid`؛ رفتار امروز |
| `shadow`، ثبت موفق | 201/200 `{mode:"shadow", approval_uuid, execution_token, proceed_legacy:false, central_decision:{...}}` | محلی، مثل امروز؛ نتیجه‌ی evaluator مرکزی و سقف فقط log می‌شوند | با `approval_uuid`؛ effectها best-effort (پایین) |
| `shadow`، ثبت ناموفق به هر دلیل (آماده‌نبودن هویت کلید، خطای ثبت، `identity_mismatch`، ...) | 200 `{mode:"shadow", approval_uuid:null, shadow_error:"<code>", proceed_legacy:true}` | محلی | بدون `approval_uuid`؛ رفتار امروز |
| `required` | 201/200 با `approval_uuid` و `execution_token`؛ یا خطای قطعی (۴۰۳، ۴۰۹، ۴۲۲، ۴۲۹). `proceed_legacy` هرگز `true` نیست | **فقط backend** | فقط با `approval_uuid` و token |
| `blocked` | 503 `receipt_approval_blocked`؛ بدون `proceed_legacy` | — | هیچ |

- **یک پیاده‌سازی برای هر سه مسیر:** `PanelBridge` و `RemoteBridge` هر دو همان متد `begin_approval(intent)` را دارند که یک `ApprovalSession(mode, approval_uuid, execution_token, proceed_legacy, shadow_error)` برمی‌گرداند. `perform_approval` و `try_auto_approve` فقط با این شیء کار می‌کنند: اگر `proceed_legacy` است، مسیر امروز را بدون `approval_uuid` اجرا می‌کنند؛ وگرنه مسیر correlated را. هر خطای HTTP (شامل ۵۰۳) یعنی توقف، نه ادامه‌ی legacy. پس یک `except` عمومی نمی‌تواند approval را بی‌صدا به legacy ببرد.
- **خطای shadow ثبت می‌شود:** هر پاسخ با `shadow_error` یک ردیف در `receipt_approval_shadow_events` می‌نویسد.

```
receipt_approval_shadow_events            -- log immutable ؛ بدون username و بدون مبلغ
id                           PK
pending_source_instance_id   VARCHAR(64) NOT NULL
pending_local_id             BIGINT      NOT NULL
stage                        VARCHAR(16) NOT NULL  CHECK IN ('registration','effect','finalize')
error_code                   VARCHAR(48) NOT NULL
approval_uuid                CHAR(36)    NULL
created_at                   NOT NULL

INDEX (created_at)
```

- **effect در shadow:** `record_effect` داخل یک savepoint اجرا می‌شود؛ هر شکست آن (نبود در manifest، ناهم‌خوانی projection، evidence نامعتبر) savepoint را برمی‌گرداند، یک ردیف `receipt_approval_shadow_events` می‌نویسد و mutation واقعی commit می‌شود. در `required` همان شکست کل تراکنش mutation را rollback می‌کند.
- **جعل `proceed_legacy` ممکن نیست:** endpointهای mutation mode مؤثر را خودشان در همان تراکنش می‌خوانند. در `required`، فروش کارتی یا topup رسیدی از مسیر بات بدون `approval_uuid` معتبر رد می‌شود (۴۰۹ `approval_required`)، صرف‌نظر از هر فیلدی که caller بفرستد.
- **تغییر mode بین ثبت و mutation** (approval ستون `registered_under_mode` دارد، بخش ۵.۱):

| approval ثبت‌شده زیر | mode مؤثر در لحظه‌ی mutation | رفتار |
|---|---|---|
| `shadow` | `shadow` | best-effort |
| `shadow` | `required` | ۴۰۹ `approval_mode_changed`؛ approval `cancelled`؛ ثبت دوباره زیر `required` |
| `required` | `required` | enforcement کامل |
| `required` | `shadow` یا `off` (اپراتور پایین آورده) | همان enforcement کامل `required` برای این approval؛ سخت‌گیری approval با پایین‌آمدن mode شل نمی‌شود |
| هرکدام | `blocked` | ۵۰۳؛ هیچ mutation |
| بدون `approval_uuid` (legacy) | `required` | ۴۰۹ `approval_required` |

---

## ۶. Remote Bot Reconciliation

### ۶.۱ دنباله

1. قبل از هر production call، approval ثبت می‌شود: مسیر خودکار (`try_auto_approve`) `.../receipt-approvals/auto` و دکمه‌ی تأیید ادمین (`cb_approval`) `.../receipt-approvals/manual` را صدا می‌زند؛ هر دو از طریق `PanelBridge` یا `RemoteBridge`. `perform_approval` با `approval_uuid` حاصل اجرا می‌شود. برای kind `new`/`renew` بات همیشه مسیر auto را صدا می‌زند؛ `auto_approve.decide` محلی دیگر تصمیم نمی‌گیرد (بخش ۵.۴).
2. هر production call (`create_user`، `purchase_package`، `renew_service`، `renew`، `add_balance`، `apply_referral`، `redeem_discount`، `record_card_payment`) فیلد `approval_uuid` را می‌برد.
3. **خودِ endpoint مرکزی**، در همان تراکنشی که اثر را می‌سازد، ردیف `receipt_approval_effects` را می‌نویسد و اگر approval هنوز `registered` است آن را `mutating` می‌کند.
4. در پایان بات `POST /api/accounting/receipt-approvals/{uuid}/finalize` می‌زند. این فقط **درخواست بررسی** است.

### ۶.۲ مالکیت state

بات هیچ endpointی برای نوشتن `state` ندارد. `finalize` effectهای واقعی را با manifest (بخش ۵.۳) روی `(effect_type, effect_key)` مقایسه می‌کند:

| نتیجه‌ی مقایسه | رفتار |
|---|---|
| همه‌ی `required` موجود؛ هیچ effect خارج از manifest؛ `actual_projection` ذخیره‌شده برابر `expected`؛ فیلدهای immutable منابع گروه ۱ برابر؛ برای topup source در `applied` | `completed` |
| projection ناهم‌خوان | state تغییر نمی‌کند؛ پاسخ `projection_mismatch`؛ یک `approval_recovery` در `cleanup_required` |
| یک `required` گم‌شده | state تغییر نمی‌کند؛ پاسخ `missing_effects` |
| effectی که در manifest نیست | state تغییر نمی‌کند؛ پاسخ `unexpected_effects`؛ یک `approval_recovery` در `cleanup_required` ساخته می‌شود |
| تکراری | ممکن نیست (UNIQUE بخش ۵.۲) |

نوشتن effect خارج از manifest، یا با مقدار ناهم‌خوان با `expected`، در همان لحظه‌ی نوشتن رد می‌شود (`record_effect`، بخش ۵.۳) و تراکنش mutation rollback می‌شود. پس `unexpected_effects` و `projection_mismatch` در `finalize` فقط نشانه‌ی باگ یا دستکاری ردیف‌های ذخیره‌شده است؛ تغییر معتبر بعدی یک منبع mutable هیچ‌کدام را نمی‌سازد.

`finalize` در هر حال ۲۰۰ با state واقعی برمی‌گرداند. `failed` فقط وقتی نوشته می‌شود که هیچ effectی وجود نداشته باشد و بات صریحاً شکست را گزارش کند؛ اگر حتی یک effect هست، approval `mutating` می‌ماند و یک `wallet_operations` از نوع recovery برای تکمیل یا ابطال آن ساخته می‌شود.

### ۶.۳ رفتار بات هنگام خطا

```
on exception در perform_approval:
    s = finalize(approval_uuid)          # state واقعی از backend
    اگر s.effect_count == 0  → storage.release_pending (امن است)
    وگرنه                    → pending محلی 'processing' می‌ماند و به ادمین پیام «نیمه‌کاره؛ از پنل پیگیری شود» داده می‌شود
```

worker مرکزی هرگز فایل sqlite محلی هیچ باتی را نمی‌خواند. `pending_source_instance_id`/`pending_local_id` فقط ارجاع پشتیبانی‌اند.

### ۶.۴ Auth

همه‌ی endpointهای این بخش `X-API-Key` → `BotPrincipal` می‌گیرند، با `bot_route_policy`:

| Endpoint | Capability |
|---|---|
| `.../receipt-approvals/auto` | `RECEIPT_AUTO_APPROVAL_WRITE` |
| `.../receipt-approvals/manual` | `RECEIPT_MANUAL_APPROVAL_WRITE` |
| `.../finalize` | هرکدام از دو capability بالا |

- **Tenant binding:** `owner_admin_id` درخواست باید داخل scope همان principal باشد (همان قاعده‌ی `bot_resources._get_*_or_403`). principalی که scope یک approval را نمی‌پوشاند نمی‌تواند آن را `finalize` کند.
- **`RECEIPT_MANUAL_APPROVAL_WRITE`** فقط به principal داخلی (بات in-process) و کلید نصب‌هایی داده می‌شود که خود پنل با `services/remote_deploy.py` ساخته. از صفحه‌ی عمومی ساخت API key قابل اعطا نیست، پس integration شخص ثالث نمی‌تواند تأیید دستی اعلام کند.
- **مدرک approver:** درخواست manual باید `approved_by_telegram_id` داشته باشد و backend خودش آن را resolve می‌کند؛ دقیقاً یکی باید برقرار باشد:

| `approver_evidence_kind` | شرط سمت backend |
|---|---|
| `linked_admin` | این Telegram به یک `AdminUser` وصل است و `owner_admin_id` approval در `visible_admin_ids` آن ادمین است |
| `owner_seller` | این Telegram به همان `AdminUser` مالک approval وصل است |
| `card_approver` | `PaymentCard.approval_telegram_id` کارت همین approval برابر این Telegram است |

  هیچ‌کدام برقرار نبود → ۴۰۳ `manual_approver_not_authorized`؛ approval ساخته نمی‌شود. این همان سه راهی است که `_approval_actor` امروز در بات می‌پذیرد، ولی حالا backend آن را enforce می‌کند.
- **`RECEIPT_AUTO_APPROVAL_WRITE`** به‌تنهایی خطری ندارد: backend policy و سقف را خودش اجرا می‌کند، پس caller نمی‌تواند تأیید خودکاری بگیرد که policy نمی‌دهد.

**Threat model مدرک دستی.** backend فقط تطبیق می‌دهد که `approved_by_telegram_id` متعلق به یک approver مجاز است؛ اثباتی از کلیک واقعی در Telegram ندارد. پس:

- کلید یک نصب managed (in-process یا remote ساخته‌ی پنل) در این مدل **proxy مورد اعتماد approver** است. نشت آن کلید یعنی امکان جعل تأیید دستی برای tenant همان نصب، و ثبت `new_user` با Telegram دلخواه.
- این capability هرگز به کلید integration عادی (tenant یا global) داده نمی‌شود و از UI ساخت کلید قابل انتخاب نیست.
- دامنه‌ی خسارت با tenant binding محدود است: کلید نصب یک ادمین فقط approvalهای scope همان ادمین را ثبت می‌کند.
- هر approval `pending_source_instance_id` (شناسه‌ی نصب) و شناسه‌ی کلید را ثبت می‌کند؛ audit می‌تواند همه‌ی approvalهای یک نصب را فهرست کند.
- **هویت کلید.** `api_keys` یک ستون جدید می‌گیرد: `key_instance_uuid CHAR(36) NULL`، UUID تصادفی مستقل از مقدار کلید (plaintext یا hash کلید هرگز به‌عنوان هویت استفاده نمی‌شود).
  - یکتایی با یک index صریح است، نه `Column(unique=True)`: `Index("uq_api_keys_key_instance_uuid", ApiKey.key_instance_uuid, unique=True)`. فقط این شکل داخل `table.indexes` می‌نشیند و ابزار migration موجود روی جدول موجود آن را می‌سازد (۳.۳). ستون nullable می‌ماند و چند NULL مجاز است.
  - backfill idempotent برای هر ردیف NULL یک UUID می‌نویسد؛ هر کلید جدید در لحظه‌ی ساخت UUID می‌گیرد.
  - چون شکست ساخت index در migration فقط warning است، آمادگی صریحاً سنجیده می‌شود (بخش ۵.۵) و تا آماده نباشد قابلیت approval fail-closed می‌ماند.
  - کلیدی که UUID ندارد، یا UUIDش تکراری است، نمی‌تواند approval ثبت یا اجرا کند (۴۰۳).
  - UUID هرگز دوباره استفاده نمی‌شود: حذف ردیف آن را از بین می‌برد و کلید جدید، حتی با همان id عددی، UUID تازه دارد.
  - rotation مقدار کلید روی همان ردیف هم UUID تازه می‌نویسد؛ پس approvalهای instance قبلی با کلید چرخانده‌شده اجرا نمی‌شوند.
- revoke، حذف یا rotation کلید فوری اثر می‌کند، با قاعده‌ی binding اجرا:

| وضعیت approval | چه کسی اجازه‌ی اجرا دارد |
|---|---|
| `execution_key_instance_uuid = U` | فقط principalی که کلیدش `key_instance_uuid = U` دارد، **و** ردیف `ApiKey` با همان UUID در همان تراکنش وجود داشته و `enabled = True` باشد. کلید دیگر، حتی معتبر، در همان scope و با همان id عددی → ۴۰۳ `execution_principal_mismatch` اگر `U` هنوز زنده است، و ۴۰۳ `execution_key_revoked` اگر ردیف `U` دیگر وجود ندارد یا غیرفعال است |
| `execution_key_instance_uuid = NULL` | فقط principal in-process (بدون کلید). هر principal با کلید → ۴۰۳ `execution_principal_mismatch`. revoke برای این حالت معنا ندارد؛ اعتماد به خود پردازه‌ی backend است |

  این بررسی در پیش‌شرط اجرا، هر `record_effect`، retry کنترل‌شده و `finalize` انجام می‌شود (بخش ۶.۵). `registered_by_api_key_id` و هر id عددی دیگر فقط نمایشی‌اند.
- تنها راه انتقال اجرا **takeover دستی** است (بخش ۵.۱)، برای هر approval `registered`/`failed` بدون effect و مستقل از mode قبلی. کلید revoke‌شده خودش نمی‌تواند takeover کند؛ takeover راه ادامه‌دادن کار توسط یک principal معتبر دیگر با تأیید انسانی است و در `receipt_approval_authority_events` ثبت می‌شود.
- approval بعد از شروع اجرا (`mutating`) یا با effect takeover ندارد؛ اگر کلیدش وسط اجرا revoke شود، approval در `mutating` می‌ماند و فقط از `approval_recovery` (superadmin، پنل) ادامه می‌یابد.
- اگر این اعتماد پذیرفته نباشد، جایگزین یک challenge یک‌بارمصرف است که backend می‌سازد و فقط از طریق callback خود Telegram برمی‌گردد. این جایگزین در این سند طراحی نشده (بخش ۲۳).

### ۶.۵ قفل ردیف approval

سه مسیر روی یک approval رقابت می‌کنند: takeover دستی، پیش‌شرط اجرا و اولین effect (`registered → mutating`)، و هر نوشتن effect بعدی. هر سه در اولین statement تراکنش خود ردیف approval را قفل می‌کنند:

```
MariaDB:  SELECT state, version, approval_mode FROM receipt_approvals WHERE approval_uuid = :u FOR UPDATE
SQLite:   BEGIN IMMEDIATE ؛ سپس همان SELECT
```

| مسیر | شرط بعد از قفل | نوشتن |
|---|---|---|
| takeover | `state IN ('registered','failed')`، `NOT EXISTS (effects)`، principal تحویل‌گیرنده معتبر (۵.۱) | mode/approver/instance اجرا، `version + 1` ؛ ردیف authority event |
| retry کنترل‌شده | `state IN ('registered','failed')`، `NOT EXISTS (effects)`، binding instance کلید اجرا (۶.۴) | `state = 'registered'`، `version + 1` ؛ token جدید ؛ ردیف authority event |
| پیش‌شرط اجرا | `state = 'registered'`، `version = execution token`، binding کلید اجرا | `state = 'mutating'` یا `'cancelled'` |
| نوشتن effect | `state = 'mutating'`، `version = execution token`، binding کلید اجرا | INSERT effect |
| `finalize` | binding کلید اجرا | `completed` یا `failed` (فقط بدون effect) یا بدون تغییر |

- token ناهم‌خوان → ۴۰۹ `approval_superseded` با state فعلی؛ هیچ mutation انجام نمی‌شود.
- در رقابت takeover با شروع اجرای خودکار دقیقاً یکی می‌برد: اگر takeover اول commit کند، اجراکننده‌ی خودکار با token قدیمی رد می‌شود؛ اگر اجرا اول commit کند، state `mutating` است و takeover ۴۰۹ می‌گیرد.
- `version` فقط با takeover و retry کنترل‌شده‌ی `failed` عوض می‌شود؛ تغییر `registered → mutating → completed` آن را عوض نمی‌کند. پس اجراکننده‌ی معتبر در طول یک اجرا token ثابت دارد.
- `failed` فقط از `finalize` نوشته می‌شود، وقتی هیچ effectی نیست و بات شکست را گزارش کرده. `failed` با effect وجود ندارد؛ اگر ردیفی چنین باشد (دستکاری)، retry و takeover هر دو ۴۰۹ `approval_has_effects` می‌دهند.

---

## ۷. `wallet_operations` / `wallet_operation_steps`

```
wallet_operations
id               PK
operation_type   VARCHAR(32)  NOT NULL  CHECK IN ('void_receipt','wallet_credit_apply','wallet_debit','wallet_refund',
                                                  'wallet_manual_adjustment','wallet_cache_repair','wallet_identity_rebind',
                                                  'approval_recovery','wallet_hold_resolution')
business_key     VARCHAR(160) NOT NULL
request_hash     CHAR(64)     NOT NULL
tenant_scope_key VARCHAR(64)  NOT NULL
actor_kind       VARCHAR(8)   NOT NULL  CHECK IN ('admin','bot','system')
actor_id         INT          NULL
actor_ip         VARCHAR(64)  NULL
reason           TEXT         NULL
state            VARCHAR(20)  NOT NULL  CHECK IN ('pending','running','completed','failed','cleanup_required','cancelled')
epoch_at_start   BIGINT       NOT NULL
result_snapshot  TEXT/JSON    NOT NULL
attempts         INT          NOT NULL DEFAULT 0
next_retry_at    NULL
created_at       NOT NULL
started_at, completed_at  NULL
version

UNIQUE (operation_type, business_key)
INDEX  (state, next_retry_at)
```

actor فقط metadata است و در هیچ UNIQUEی نیست. درخواست تکراری با همان `(operation_type, business_key)`: اگر `request_hash` برابر است همان operation برمی‌گردد (۲۰۰)؛ اگر متفاوت است ۴۰۹.

| `operation_type` | `business_key` |
|---|---|
| `void_receipt` | `approval_uuid` |
| `wallet_credit_apply` | `{wallet_account_id}:{origin_kind}:{source_key}` |
| `wallet_debit` | `{wallet_account_id}:{idempotency_token}` (token یک UUID که مسیر خرید یک‌بار می‌سازد) |
| `wallet_refund` | `debit:{wallet_debit_event_id}` |
| `wallet_manual_adjustment` | `{wallet_account_id}:{idempotency_token}` |
| `wallet_cache_repair` | `{wallet_account_id}:{reconciliation_run_id}` |
| `wallet_identity_rebind` | `{wallet_account_id}:{idempotency_token}` |
| `approval_recovery` | `approval_uuid` |
| `wallet_hold_resolution` | `hold:{hold_ref}` |

```
wallet_operation_steps
id                   PK
wallet_operation_id  BIGINT       NOT NULL  FK wallet_operations(id) ON DELETE RESTRICT
step_key             VARCHAR(160) NOT NULL
step_order           INT          NOT NULL
state                VARCHAR(12)  NOT NULL  CHECK IN ('pending','running','done','failed','blocked','abandoned')
remote_outcome       VARCHAR(28)  NULL      CHECK IN ('verified_absent','delete_idempotently_absent','unverified','abandoned')
attempts             INT          NOT NULL DEFAULT 0
next_retry_at        NULL
error_sanitized      TEXT         NULL
result_snapshot      TEXT/JSON    NOT NULL
created_at           NOT NULL
completed_at         NULL

UNIQUE (wallet_operation_id, step_key)
INDEX  (wallet_operation_id, step_order)
CHECK  ((state = 'done'      AND (remote_outcome IS NULL OR remote_outcome IN ('verified_absent','delete_idempotently_absent')))
     OR (state = 'blocked'   AND remote_outcome = 'unverified')
     OR (state = 'abandoned' AND remote_outcome = 'abandoned')
     OR (state IN ('pending','running','failed') AND remote_outcome IS NULL))
```

گام `done` دوباره اجرا نمی‌شود. `remote_outcome` فقط برای گام‌های remote پر می‌شود. `blocked` یعنی کار remote انجام شده ولی نبودن منبع اثبات نشده؛ operation را در `cleanup_required` نگه می‌دارد. `abandoned` فقط با force صریح superadmin (بخش ۱۳.۵).

---

## ۸. `resource_locks` و پروتکل fencing

```
resource_locks
resource_key    VARCHAR(96) NOT NULL PRIMARY KEY
lease_owner     VARCHAR(64) NULL
fencing_epoch   BIGINT      NOT NULL DEFAULT 0
leased_until    DATETIME(6) NULL
heartbeat_at    DATETIME(6) NULL
version         INT         NOT NULL DEFAULT 0
```

کلیدها: `customer_identity:{id}` و `payment_card_pool:{pool_key}`. قفل جدا برای account وجود ندارد: **هر** mutation کیف‌پول یک account زیر قفل identity همان account انجام می‌شود، پس قفل identity همه‌ی accountهایش را می‌پوشاند.

`DB_NOW` همیشه در SQL محاسبه می‌شود (MariaDB: `NOW(6)`؛ SQLite: `strftime('%Y-%m-%d %H:%M:%f','now')`)؛ ساعت پردازه هرگز مقایسه نمی‌شود.

### ۸.۱ Acquire

```
tx:
  INSERT resource_locks(resource_key) VALUES(:key)   -- اگر نبود؛ IntegrityError نادیده گرفته می‌شود
tx:
  UPDATE resource_locks
     SET lease_owner = :owner, fencing_epoch = fencing_epoch + 1,
         leased_until = DB_NOW + :ttl, heartbeat_at = DB_NOW, version = version + 1
   WHERE resource_key = :key
     AND (lease_owner IS NULL OR leased_until < DB_NOW)
  rowcount = 1 → SELECT fencing_epoch  (همان tx)  → (owner, epoch) نزد executor
  rowcount = 0 → قفل مشغول؛ backoff
```

`:owner` برابر `op:{wallet_operation_id}:{attempt}` است، پس دو تلاش یک operation هم دو owner متفاوت دارند.

### ۸.۲ Renew و Release

```
renew:   UPDATE ... SET leased_until = DB_NOW + :ttl, heartbeat_at = DB_NOW, version = version + 1
         WHERE resource_key = :key AND lease_owner = :owner AND fencing_epoch = :epoch AND leased_until >= DB_NOW
         rowcount = 0 → lease از دست رفته؛ executor متوقف می‌شود

release: UPDATE ... SET lease_owner = NULL, leased_until = NULL, version = version + 1
         WHERE resource_key = :key AND lease_owner = :owner AND fencing_epoch = :epoch
```

release فقط بعد از commit یا rollback کامل آخرین تراکنش business انجام می‌شود.

### ۸.۳ Revalidation در تراکنش business

هر تراکنشی که ردیف مالی یا ردیف PaymentCard را تغییر می‌دهد، **اول** این را اجرا می‌کند:

```
MariaDB:  START TRANSACTION
SQLite:   BEGIN IMMEDIATE

SELECT resource_key, lease_owner, fencing_epoch,
       (leased_until >= DB_NOW) AS live
  FROM resource_locks
 WHERE resource_key IN (:keys)
 ORDER BY resource_key
 FOR UPDATE            -- فقط MariaDB؛ در SQLite قفل نوشتن BEGIN IMMEDIATE همین نقش را دارد

برای هر کلید: اگر lease_owner != :owner  یا  fencing_epoch != :epoch  یا  live = 0  → ROLLBACK
... UPDATEهای business (هرکدام با WHERE version = :expected) ...
COMMIT
```

هر سه شرط بررسی می‌شود. چرا کافی است:

- executor جدید برای acquire باید همان ردیف `resource_locks` را UPDATE کند. تا وقتی تراکنش business قفل ردیف را (`FOR UPDATE` / قفل نوشتن SQLite) نگه داشته، acquire رقیب منتظر می‌ماند. پس بین revalidation و COMMIT هیچ مالک جدیدی نمی‌تواند ساخته شود.
- اگر lease قبل از revalidation منقضی شده باشد، `live = 0` و تراکنش rollback می‌شود، حتی اگر هنوز هیچ‌کس acquire نکرده باشد.
- اگر رقیب قبلاً acquire کرده، `fencing_epoch` فرق دارد و تراکنش rollback می‌شود، حتی اگر `version` ردیف مالی هنوز دست‌نخورده باشد.

`version` روی ردیف‌های مالی فقط لایه‌ی دوم است، نه جایگزین این revalidation. lease برای عملیات چند-تراکنشی (گام‌های متعدد، فراخوانی remote بین گام‌ها) لازم است؛ عملیات تک-تراکنشی هم همین پروتکل را اجرا می‌کند.

---

## ۹. `wallet_runtime_state` و پروتکل phase/epoch

```
wallet_runtime_state
id                 INT         NOT NULL PRIMARY KEY   CHECK (id = 1)
phase              VARCHAR(12) NOT NULL DEFAULT 'normal'  CHECK IN ('normal','fencing','enforced')
epoch              BIGINT      NOT NULL DEFAULT 0
external_fencing   VARCHAR(16) NOT NULL DEFAULT 'none'    CHECK IN ('none','coordinator')
updated_at         NOT NULL
version
```

### ۹.۱ دو نسل writer

`wallet_service` دو پیاده‌سازی دارد و فقط یکی در هر phase مجاز است:

| phase | legacy writer (نوشتن مستقیم `User.balance`) | wallet-lot writer |
|---|---|---|
| `normal` | مجاز | ممنوع |
| `fencing` | ممنوع (۵۰۳) | ممنوع (۵۰۳) |
| `enforced` | ممنوع | مجاز |

### ۹.۲ قرارداد هر تراکنش writer

اولین statement هر تراکنش writer (قبل از revalidation بخش ۸.۳ و قبل از هر UPDATE):

```
MariaDB:  SELECT phase, epoch FROM wallet_runtime_state WHERE id = 1 LOCK IN SHARE MODE
SQLite:   BEGIN IMMEDIATE ؛ سپس SELECT phase, epoch FROM wallet_runtime_state WHERE id = 1
```

- این یک **current locking read** است. در MariaDB یک SELECT ساده زیر REPEATABLE READ می‌تواند snapshot قدیمی ببیند؛ برای همین `LOCK IN SHARE MODE` الزامی است.
- writer فقط اگر `phase` با نسل خودش سازگار است ادامه می‌دهد.
- اگر تراکنش بخشی از یک `wallet_operation` است: `epoch` باید با `wallet_operations.epoch_at_start` برابر باشد، وگرنه rollback. پس عملیات چند-تراکنشی که cutover از وسطش رد شده هرگز ادامه نمی‌یابد.
- قفل مشترک تا COMMIT نگه داشته می‌شود.

### ۹.۳ تغییر phase

تغییر phase یک `UPDATE wallet_runtime_state SET phase = ..., epoch = epoch + 1` است. این UPDATE قفل انحصاری می‌خواهد، پس تا commit/rollback همه‌ی writerهای در حال اجرا (که قفل مشترک دارند) منتظر می‌ماند، و writerهای بعدی تا commit آن منتظر می‌مانند و سپس phase جدید را می‌بینند. نتیجه: هیچ writerی نمی‌تواند با phase قدیمی بعد از تغییر phase commit کند. هیچ «تأخیر کوتاه» یا شمارش تراکنش لازم نیست.

writerی که این قرارداد را رعایت نکند وجود ندارد: همه‌ی نویسنده‌ها قبل از cutover به سرویس منتقل شده‌اند و گیت AST آن را ثابت می‌کند (بخش ۲۱).

---

## ۱۰. Schema هویت

### ۱۰.۱ `customer_identities`

```
id                       PK
tenant_scope_key         VARCHAR(64) NOT NULL
identity_key             VARCHAR(96) NOT NULL
telegram_id              BIGINT      NULL
owner_admin_id_snapshot  INT         NULL
created_at               NOT NULL

UNIQUE (tenant_scope_key, identity_key)
INDEX  (telegram_id)
```

- `tenant_scope_key` immutable و non-null است: `shared` برای مشتری بدون مالک؛ وگرنه `admin:{id}` که `id` ادمین سطح ۲ بالای درخت مالک است (seller به ادمین والدش resolve می‌شود). این کلید به هیچ FK وابسته نیست، پس حذف ادمین uniqueness را نمی‌شکند. انتخاب «ریشه‌ی درخت» به‌جای «مالک مستقیم» پیش‌فرض این سند است و در بخش ۲۳ برای تأیید Product آمده.
- `identity_key`: `tg:{telegram_id}` اگر Telegram دارد؛ وگرنه `private:{lineage_key account}`. همیشه non-null، پس UNIQUE به NULL تکیه ندارد.
- identity هرگز حذف نمی‌شود.

### ۱۰.۲ `wallet_accounts`

```
id                       PK
lineage_key              CHAR(36)     NOT NULL  UNIQUE
customer_identity_id     BIGINT       NOT NULL  FK customer_identities(id) ON DELETE RESTRICT
user_id                  INT          NULL      FK users(id) ON DELETE RESTRICT
user_id_snapshot         INT          NOT NULL
username_snapshot        VARCHAR(128) NOT NULL
owner_admin_id_snapshot  INT          NULL
topup_blocked            BOOLEAN      NOT NULL DEFAULT 0
topup_blocked_reason     VARCHAR(255) NULL
tombstoned_at            NULL
created_at               NOT NULL
version

UNIQUE (user_id)                      -- NULL-exempt عمداً: فقط accountهای زنده یکتا هستند
INDEX  (customer_identity_id)
CHECK  ((user_id IS NOT NULL AND tombstoned_at IS NULL) OR (user_id IS NULL AND tombstoned_at IS NOT NULL))
```

- **یک‌به‌یک:** `UNIQUE(user_id)` تضمین می‌کند هر User زنده حداکثر یک account دارد. backfill و ساخت همان INSERT را می‌زنند؛ retry با IntegrityError به «از قبل هست» می‌رسد و account دوم ساخته نمی‌شود. جدول `users` هیچ ستون جدیدی نمی‌گیرد؛ lookup از `wallet_accounts.user_id` است.
- `lineage_key` یک UUID است که در لحظه‌ی ساخت تولید می‌شود. به `users.id` وابسته نیست، چون SQLite بعد از حذف آخرین ردیف می‌تواند همان id را دوباره بدهد.
- **Tombstone:** `delete_user_cascade` قبل از `db.delete(user)` این را در همان تراکنش اجرا می‌کند: `user_id = NULL, tombstoned_at = now`. چون FK `RESTRICT` است، روی MariaDB حذف User بدون tombstone شکست می‌خورد؛ روی SQLite همین ترتیب را سرویس تضمین می‌کند و تست AST (بخش ۲۲) ثابت می‌کند تنها مسیر حذف همین تابع است.
- account tombstone‌شده هرگز به User دیگری وصل نمی‌شود (`user_id` آن دیگر نوشته نمی‌شود)، حتی اگر `users.id` تکرار شود. lotها و debitهایش برای audit می‌مانند و قابل‌خرج نیستند.
- ساخت User: هر شش محل ۳.۳ از یک تابع `create_user_with_wallet` عبور می‌کنند که identity را find-or-create و **همیشه** یک account تازه می‌سازد.

### ۱۰.۳ `wallet_account_identity_rebinds`

```
id                    PK
wallet_account_id     BIGINT      NOT NULL FK wallet_accounts(id) ON DELETE RESTRICT
from_identity_id      BIGINT      NOT NULL FK customer_identities(id) ON DELETE RESTRICT
to_identity_id        BIGINT      NOT NULL FK customer_identities(id) ON DELETE RESTRICT
cause                 VARCHAR(20) NOT NULL CHECK IN ('telegram_link','telegram_change','owner_transfer')
actor_admin_id        INT         NULL
wallet_operation_id   BIGINT      NOT NULL FK wallet_operations(id) ON DELETE RESTRICT
created_at            NOT NULL

UNIQUE (wallet_operation_id)
INDEX  (wallet_account_id, created_at)
```

هر تغییر `User.telegram_id` یا `User.owner_admin_id` (محل‌های ۳.۳) identity account را عوض می‌کند. این فقط با `wallet_identity_rebind` انجام می‌شود: قفل هر دو identity (ترتیب بخش ۱۲.۱)، سپس `customer_identity_id` به‌روز و ردیف بالا نوشته می‌شود.

**Fail-closed:** اگر identity مبدأ بدهی باز دارد، rebind رد می‌شود (۴۰۹). بدون این قاعده، تغییر Telegram راه فرار از بدهی می‌شد. بدهی‌های بسته‌شده با identity قبلی می‌مانند.

---

## ۱۱. Schema پول و conservation

### ۱۱.۱ جدول‌ها

```
wallet_credit_sources                    -- فقط ورود پول خارجی
id                         PK
wallet_account_id          BIGINT       NOT NULL FK wallet_accounts(id) ON DELETE RESTRICT
origin_kind                VARCHAR(20)  NOT NULL CHECK IN ('receipt_topup','loyalty_reward','referral_reward','manual_adjustment')
source_key                 VARCHAR(160) NOT NULL
causation_approval_uuid    CHAR(36)     NULL     FK receipt_approvals(approval_uuid) ON DELETE RESTRICT
actor_admin_id             INT          NULL
actor_username_snapshot    VARCHAR(128) NULL
reason                     TEXT         NULL
gross_amount               BIGINT       NOT NULL CHECK (gross_amount > 0)
unapplied_amount           BIGINT       NOT NULL CHECK (unapplied_amount >= 0)
state                      VARCHAR(10)  NOT NULL CHECK IN ('applying','applied','voiding','voided')
voided_at                  NULL
void_operation_id          BIGINT       NULL     FK wallet_operations(id) ON DELETE RESTRICT
wallet_operation_id        BIGINT       NOT NULL FK wallet_operations(id) ON DELETE RESTRICT
created_at                 NOT NULL
version

UNIQUE (wallet_account_id, origin_kind, source_key)
INDEX  (causation_approval_uuid)
CHECK  (origin_kind <> 'receipt_topup' OR causation_approval_uuid IS NOT NULL)
CHECK  (origin_kind <> 'manual_adjustment' OR (actor_admin_id IS NOT NULL AND reason IS NOT NULL))
CHECK  (state NOT IN ('applied','voided') OR unapplied_amount = 0)
CHECK  ((state IN ('voiding','voided')) = (voided_at IS NOT NULL))
```

`causation_approval_uuid` یعنی «این credit نتیجه‌ی کدام approval است» و از نوع credit مستقل است: برای `receipt_topup` اجباری، برای loyalty/referral ناشی از یک approval پر، و برای بقیه NULL.

| `origin_kind` | `source_key` |
|---|---|
| `receipt_topup` | `approval_uuid` |
| `loyalty_reward` | `loyalty:{user_id_snapshot}:{reward_ordinal}` (ordinal = مقدار `loyalty_rewards_given` بعد از این پاداش) |
| `referral_reward` | `referral:{referred_lineage_key}:referrer` یا `...:new_user` |
| `manual_adjustment` | `idempotency_token` |

```
wallet_credit_applications
id                        PK
wallet_credit_source_id   BIGINT       NOT NULL FK wallet_credit_sources(id) ON DELETE RESTRICT
effect_key                VARCHAR(160) NOT NULL
application_kind          VARCHAR(16)  NOT NULL CHECK IN ('lot','debt_settlement')
amount                    BIGINT       NOT NULL CHECK (amount > 0)
reversed_amount           BIGINT       NOT NULL DEFAULT 0
wallet_lot_id             BIGINT       NULL     FK wallet_lots(id) ON DELETE RESTRICT
wallet_debt_id            BIGINT       NULL     FK wallet_debts(id) ON DELETE RESTRICT
wallet_operation_id       BIGINT       NOT NULL FK wallet_operations(id) ON DELETE RESTRICT
created_at                NOT NULL
version

UNIQUE (wallet_credit_source_id, effect_key)
UNIQUE (wallet_lot_id)                         -- NULL-exempt عمداً: هر lot از یک application
INDEX  (wallet_debt_id, created_at, id)
CHECK  (reversed_amount >= 0 AND reversed_amount <= amount)
CHECK  ((application_kind = 'lot' AND wallet_lot_id IS NOT NULL AND wallet_debt_id IS NULL AND reversed_amount = 0)
     OR (application_kind = 'debt_settlement' AND wallet_debt_id IS NOT NULL AND wallet_lot_id IS NULL))
```

`effective = amount - reversed_amount`. `effect_key`: `lot:primary`، `settle:{debt_id}`، `realloc:{reversal_id}:settle:{debt_id}`، `realloc:{reversal_id}:lot`.

```
wallet_credit_application_reversals      -- فقط برای application از نوع debt_settlement؛ partial مجاز
id                             PK
wallet_credit_application_id   BIGINT       NOT NULL FK wallet_credit_applications(id) ON DELETE RESTRICT
effect_key                     VARCHAR(160) NOT NULL
amount                         BIGINT       NOT NULL CHECK (amount > 0)
disposition                    VARCHAR(16)  NOT NULL CHECK IN ('reallocate','void_writeoff')
wallet_operation_id            BIGINT       NOT NULL FK wallet_operations(id) ON DELETE RESTRICT
created_at                     NOT NULL

UNIQUE (wallet_credit_application_id, effect_key)
```

چند reversal روی یک application مجاز است. `reversed_amount` روی application در همان تراکنش به‌روز می‌شود و CHECK آن تضمین می‌کند مجموع reversalها از `amount` بیشتر نشود. `effect_key`: `refund:{allocation_reversal_id}` یا `void:{void_operation_id}`.

```
wallet_lots
id                        PK
wallet_account_id         BIGINT       NOT NULL FK wallet_accounts(id) ON DELETE RESTRICT
source_kind               VARCHAR(24)  NOT NULL CHECK IN ('receipt_topup','loyalty_reward','referral_reward','manual_adjustment',
                                                          'debt_settlement_remainder','settlement_reallocation','legacy_baseline')
source_key                VARCHAR(160) NOT NULL
source_credit_source_id   BIGINT       NULL     FK wallet_credit_sources(id) ON DELETE RESTRICT
original_amount           BIGINT       NOT NULL CHECK (original_amount > 0)
available_amount          BIGINT       NOT NULL CHECK (available_amount >= 0)
consumed_amount           BIGINT       NOT NULL DEFAULT 0 CHECK (consumed_amount >= 0)
voided_available_amount   BIGINT       NULL
state                     VARCHAR(10)  NOT NULL CHECK IN ('active','exhausted','voided')
voided_at                 NULL
void_operation_id         BIGINT       NULL     FK wallet_operations(id) ON DELETE RESTRICT
created_at                NOT NULL
version

UNIQUE (wallet_account_id, source_kind, source_key)
INDEX  (wallet_account_id, state, created_at, id)
INDEX  (source_credit_source_id)
CHECK  ((source_kind = 'legacy_baseline') = (source_credit_source_id IS NULL))
CHECK  ((state = 'active'    AND voided_at IS NULL AND available_amount > 0 AND available_amount + consumed_amount = original_amount)
     OR (state = 'exhausted' AND voided_at IS NULL AND available_amount = 0 AND consumed_amount = original_amount)
     OR (state = 'voided'    AND voided_at IS NOT NULL AND available_amount = 0
                             AND voided_available_amount IS NOT NULL
                             AND voided_available_amount + consumed_amount = original_amount))
```

- `consumed_amount` مقدار **خالص** مصرف است: allocation آن را زیاد و reversal روی lot غیر-voided آن را کم می‌کند. پس `available + consumed = original` همیشه برقرار است.
- lot باطل‌شده دیگر تغییر نمی‌کند: `consumed_amount` آن برای همیشه مقدار خالص لحظه‌ی void است.
- `state` با CHECK به ستون‌ها بسته است و نمی‌تواند drift کند.
- `source_kind`: lot اصلی یک source نوع همان origin را دارد؛ اگر بخشی از source صرف بدهی شده، باقی‌مانده `debt_settlement_remainder` است؛ پول آزادشده از یک settlement، `settlement_reallocation`.

```
wallet_debit_events
id                    PK
wallet_account_id     BIGINT      NOT NULL FK wallet_accounts(id) ON DELETE RESTRICT
kind                  VARCHAR(20) NOT NULL CHECK IN ('sale','manual_adjustment')
amount                BIGINT      NOT NULL CHECK (amount > 0)
hold_ref              CHAR(36)    NOT NULL                       -- UUID پایدار همین برداشت؛ کلید مشترک با عملیات provisioning
provisioning_ref      VARCHAR(64) NULL                           -- شناسه‌ی عملیات provisioning، وقتی وجود دارد
ledger_entry_id       INT         NULL     FK ledger_entries(id) ON DELETE RESTRICT
actor_admin_id        INT         NULL
reason                TEXT        NULL
state                 VARCHAR(10) NOT NULL CHECK IN ('held','captured','released','reversed')
held_at               NOT NULL
captured_at, released_at, reversed_at   NULL
wallet_operation_id   BIGINT      NOT NULL FK wallet_operations(id) ON DELETE RESTRICT
created_at            NOT NULL
version

UNIQUE (hold_ref)
UNIQUE (wallet_operation_id)
UNIQUE (ledger_entry_id)                 -- NULL-exempt عمداً
INDEX  (wallet_account_id, created_at)
INDEX  (state, held_at)
CHECK  ((kind = 'sale' AND actor_admin_id IS NULL
            AND ((state IN ('held','released') AND ledger_entry_id IS NULL)
              OR (state IN ('captured','reversed') AND ledger_entry_id IS NOT NULL)))
     OR (kind = 'manual_adjustment' AND ledger_entry_id IS NULL AND actor_admin_id IS NOT NULL AND reason IS NOT NULL
            AND state IN ('captured','reversed')))
```

`held`: پول از lotها برداشته شده ولی فروش هنوز ثبت نشده. `captured`: فروش ثبت شده و `ledger_entry_id` پر است. `released`: hold بدون فروش برگشته. `reversed`: فروش ثبت‌شده بعداً برگشته.

```
wallet_allocations
id                      PK
wallet_debit_event_id   BIGINT NOT NULL FK wallet_debit_events(id) ON DELETE RESTRICT
wallet_lot_id           BIGINT NOT NULL FK wallet_lots(id) ON DELETE RESTRICT
allocated_amount        BIGINT NOT NULL CHECK (allocated_amount > 0)
created_at              NOT NULL

UNIQUE (wallet_debit_event_id, wallet_lot_id)
INDEX  (wallet_lot_id)

wallet_allocation_reversals
id                     PK
wallet_allocation_id   BIGINT      NOT NULL FK wallet_allocations(id) ON DELETE RESTRICT
reversed_amount        BIGINT      NOT NULL CHECK (reversed_amount > 0)
lot_was_voided         BOOLEAN     NOT NULL
wallet_operation_id    BIGINT      NOT NULL FK wallet_operations(id) ON DELETE RESTRICT
created_at             NOT NULL

UNIQUE (wallet_allocation_id)            -- برگشت allocation فقط کامل است
```

```
wallet_debts
id                         PK
customer_identity_id       BIGINT      NOT NULL FK customer_identities(id) ON DELETE RESTRICT
wallet_account_id          BIGINT      NOT NULL FK wallet_accounts(id) ON DELETE RESTRICT
source_wallet_lot_id       BIGINT      NOT NULL FK wallet_lots(id) ON DELETE RESTRICT
source_void_operation_id   BIGINT      NOT NULL FK wallet_operations(id) ON DELETE RESTRICT
amount                     BIGINT      NOT NULL CHECK (amount > 0)
adjusted_amount            BIGINT      NOT NULL DEFAULT 0
settled_amount             BIGINT      NOT NULL DEFAULT 0
state                      VARCHAR(20) NOT NULL CHECK IN ('open','partially_settled','settled')
created_at                 NOT NULL
version

UNIQUE (source_void_operation_id, source_wallet_lot_id)
INDEX  (customer_identity_id, state, created_at, id)
CHECK  (adjusted_amount >= 0 AND adjusted_amount <= amount)
CHECK  (settled_amount >= 0 AND settled_amount <= amount - adjusted_amount)
CHECK  ((state = 'settled'           AND settled_amount = amount - adjusted_amount)
     OR (state = 'open'              AND settled_amount = 0 AND amount - adjusted_amount > 0)
     OR (state = 'partially_settled' AND settled_amount > 0 AND settled_amount < amount - adjusted_amount))

wallet_debt_adjustments
id                              PK
wallet_debt_id                  BIGINT NOT NULL FK wallet_debts(id) ON DELETE RESTRICT
source_allocation_reversal_id   BIGINT NOT NULL FK wallet_allocation_reversals(id) ON DELETE RESTRICT
amount                          BIGINT NOT NULL CHECK (amount > 0)
wallet_operation_id             BIGINT NOT NULL FK wallet_operations(id) ON DELETE RESTRICT
created_at                      NOT NULL

UNIQUE (source_allocation_reversal_id)
INDEX  (wallet_debt_id)

wallet_writeoffs                         -- مصرف lot باطل‌شده‌ای که بدهی نمی‌شود؛ immutable
id                         PK
wallet_operation_id        BIGINT      NOT NULL FK wallet_operations(id) ON DELETE RESTRICT
customer_identity_id       BIGINT      NOT NULL FK customer_identities(id) ON DELETE RESTRICT
wallet_account_id          BIGINT      NOT NULL FK wallet_accounts(id) ON DELETE RESTRICT
wallet_lot_id              BIGINT      NOT NULL FK wallet_lots(id) ON DELETE RESTRICT
amount                     BIGINT      NOT NULL CHECK (amount > 0)
reason                     VARCHAR(32) NOT NULL CHECK IN ('third_party_reward_consumed')
created_at                 NOT NULL

UNIQUE (wallet_operation_id, wallet_lot_id)
INDEX  (customer_identity_id)

quota_reversal_shortfalls                -- بخش برگشت‌نخورده‌ی یک پاداش حجمی؛ immutable
id                             PK
receipt_approval_effect_id     BIGINT  NULL     FK receipt_approval_effects(id) ON DELETE RESTRICT   -- پاداش حجمی approvalدار (referral، یا loyalty با approval)
loyalty_reward_event_id        BIGINT  NULL     FK loyalty_reward_events(id) ON DELETE RESTRICT      -- پاداش loyalty بدون approval
wallet_operation_id            BIGINT  NOT NULL FK wallet_operations(id) ON DELETE RESTRICT
user_id_snapshot               INT     NOT NULL
granted_bytes                  BIGINT  NOT NULL CHECK (granted_bytes > 0)
reversed_bytes                 BIGINT  NOT NULL CHECK (reversed_bytes >= 0)
shortfall_bytes                BIGINT  NOT NULL CHECK (shortfall_bytes > 0)
created_at                     NOT NULL

UNIQUE (receipt_approval_effect_id)       -- NULL-exempt عمداً
UNIQUE (loyalty_reward_event_id)          -- NULL-exempt عمداً
CHECK  (reversed_bytes + shortfall_bytes = granted_bytes)
CHECK  ((receipt_approval_effect_id IS NULL) <> (loyalty_reward_event_id IS NULL))

wallet_cache_repair_events               -- بدون اثر مالی
id                    PK
wallet_account_id     BIGINT NOT NULL FK wallet_accounts(id) ON DELETE RESTRICT
old_cached_value      BIGINT NOT NULL
new_derived_value     BIGINT NOT NULL
wallet_operation_id   BIGINT NOT NULL FK wallet_operations(id) ON DELETE RESTRICT
created_at            NOT NULL

UNIQUE (wallet_operation_id)
```

**افزوده‌ها روی جدول‌های موجود (همه nullable):**

```
ledger_entries:               approval_uuid CHAR(36) NULL            INDEX
                              amount_source VARCHAR(8) NULL          CHECK IN ('explicit','fallback') ؛ NULL = ردیف قدیمی ؛ فقط audit، در هیچ تصمیمی نیست
                              voided_at NULL
                              void_operation_id BIGINT NULL
                              reversal_of_id INT NULL                Index("uq_ledger_entries_reversal_of_id", unique=True) صریح ؛ NULL-exempt عمداً
discount_code_redemptions:    approval_uuid CHAR(36) NULL            INDEX
                              voided_at NULL
                              void_operation_id BIGINT NULL
```

### ۱۱.۲ تعریف‌ها و conservation

**بدهی:**

```
effective_obligation(d) = d.amount - d.adjusted_amount
remaining_debt(d)       = effective_obligation(d) - d.settled_amount          (طبق CHECK همیشه >= 0)
open_debt(identity)     = Σ remaining_debt(d) روی بدهی‌های آن identity
```

`adjusted_amount` برابر `Σ wallet_debt_adjustments.amount` و `settled_amount` برابر `Σ effective` applicationهای `debt_settlement` آن بدهی است. هر دو در همان تراکنش eventشان به‌روز می‌شوند، CHECK آن‌ها را محدود می‌کند و job تطبیق (بخش ۲۰.۵) برابری با eventها را می‌سنجد. `state` با CHECK از همین دو ستون مشتق است.

**Conservation هر source (I-SRC):**

```
gross_amount = Σ effective(lot applications)
             + Σ effective(debt_settlement applications)
             + Σ reversals با disposition='void_writeoff'
             + unapplied_amount
```

معادل: `Σ application.amount - Σ reversals(reallocate) + unapplied_amount = gross_amount`. source فقط وقتی `applied` یا `voided` می‌شود که `unapplied_amount = 0` (CHECK). هیچ عملیاتی source جدید برای جابه‌جایی داخلی نمی‌سازد، پس gross ورودی هر رسید دقیقاً یک‌بار دیده می‌شود.

**Conservation هر lot (I-LOT):** CHECK جدول.

**Conservation هر identity (I-NET):**

```
Σ available(lotهای غیر-voided) - open_debt
    = Σ gross(sourceهای غیر-voided) + Σ original(lotهای legacy_baseline) - net_debited

net_debited = Σ wallet_allocations.allocated_amount - Σ wallet_allocation_reversals.reversed_amount
              (روی همه‌ی lotهای accountهای آن identity، شامل lotهای voided)
```

بدهی liability است و از موقعیت خالص **کم** می‌شود. سمت راست فقط پول واقعی (source سالم و baseline) منهای ارزشی است که مشتری واقعاً برداشته.

برداشت‌های `held` هم در `net_debited` هستند (allocation دارند)؛ `released` و `reversed` با reversal از آن کم می‌شوند.

lot پاداش متعلق به identity **دیگری** که هنگام void بخش خرج‌شده‌اش بدهی نمی‌شود (بخش ۱۲.۶)، یک ردیف `wallet_writeoffs` می‌گیرد. شکل کامل I-NET:

```
Σ available(lotهای غیر-voided) - open_debt + Σ wallet_writeoffs.amount
    = Σ gross(sourceهای غیر-voided) + Σ original(lotهای legacy_baseline) - net_debited
```

I-NET و job تطبیق فقط از ردیف‌های `wallet_writeoffs` محاسبه می‌شوند. `result_snapshot` عملیات فقط projection نمایشی است و ورودی هیچ محاسبه‌ای نیست.

**Cache (I-CACHE):** برای هر account زنده: `User.balance = Σ available(lotهای غیر-voided آن account)`.

---

## ۱۲. الگوریتم‌ها

### ۱۲.۱ Lock plan

هر عملیات کیف‌پول قبل از هر mutation این را اجرا می‌کند:

1. **Resolve (بدون قفل):** فهرست همه‌ی identityها و poolهایی که لمس می‌شوند.
2. **Sort:** اول نوع، سپس شناسه: همه‌ی `customer_identity:{id}` به ترتیب صعودی عددی `id`، سپس همه‌ی `payment_card_pool:{key}` به ترتیب الفبایی.
3. **Acquire** به همان ترتیب (بخش ۸.۱). اگر یکی مشغول بود: همه‌ی گرفته‌شده‌ها release و backoff.
4. **Re-resolve زیر قفل** و مقایسه با قدم ۱. اگر مجموعه فرق کرد: release و شروع دوباره.
5. هر تراکنش business همه‌ی کلیدها را با یک SELECT مرتب revalidate می‌کند (بخش ۸.۳).
6. cache هر account متأثر در همان تراکنشی به‌روز می‌شود که lot آن account تغییر می‌کند.
7. Release فقط بعد از commit/rollback آخرین تراکنش.

| عملیات | مجموعه‌ی قفل |
|---|---|
| credit روی یک account | identity همان account |
| referral (دو طرف) | identity معرف + identity کاربر جدید |
| debit / manual adjustment | identity همان account |
| refund | identity همان account (sourceهای settle‌کننده‌ی بدهی یک identity همیشه متعلق به همان identity‌اند) |
| void | identity هر account که sourceی با `causation_approval_uuid` دارد + pool کارت رسید |
| rebind | identity مبدأ + identity مقصد |

### ۱۲.۲ Credit apply (C)

ورودی: account، `origin_kind`، `source_key`، `gross_amount`، و در صورت وجود `causation_approval_uuid`.

```
tx (phase=enforced، revalidate):
  1. INSERT wallet_credit_sources(state='applying', unapplied_amount=gross)   -- UNIQUE → retry همان ردیف
  2. اگر origin_kind = 'receipt_topup': approval باید موجود، kind='topup'، و amount_snapshot = gross باشد؛ وگرنه 422
  3. distribute(source, exclude_debt = None)
  4. source.state = 'applied'
  5. cache accountهای متأثر
  6. INSERT receipt_approval_effects(wallet_credit_source_created) اگر causation دارد
COMMIT

distribute(source, exclude_debt):
  برای هر بدهی d همان identity با remaining_debt(d) > 0 و d != exclude_debt، به ترتیب (created_at, id):
      x = min(source.unapplied_amount, remaining_debt(d)) ؛ اگر x = 0 توقف
      INSERT application(kind='debt_settlement', debt=d, amount=x)
      d.settled_amount += x ؛ d.state بازمحاسبه ؛ source.unapplied_amount -= x
  اگر source.unapplied_amount > 0:
      INSERT wallet_lots(account = source.wallet_account_id, original = available = unapplied, state='active')
      INSERT application(kind='lot', lot, amount = unapplied)
      source.unapplied_amount = 0
```

settlement هرگز از `remaining_debt` بیشتر نمی‌شود: `x` از آن مشتق است و CHECK جدول هم جلوی آن را می‌گیرد.

### ۱۲.۳ Debit: hold، capture، release (D)

برداشت فروش سه عمل جدا دارد، هرکدام یک تراکنش محلی با revalidation.

```
hold(account, amount, hold_ref):                      -- قبل از هر کار provisioning
  tx (phase=enforced):
    1. can_purchase(account) (بخش ۱۲.۷)؛ رد → 409
    2. INSERT wallet_operations(wallet_debit, business_key = '{account}:{hold_ref}')      -- تکرار → همان hold
    3. INSERT wallet_debit_events(kind='sale', state='held', hold_ref, ledger_entry_id=NULL)
    4. lotهای state='active' همان account به ترتیب (created_at, id):
           x = min(lot.available_amount, remaining)
           INSERT wallet_allocations(debit, lot, x) ؛ lot.available -= x ؛ lot.consumed += x ؛ state بازمحاسبه
    5. اگر remaining > 0 → ROLLBACK، 400 «موجودی کافی نیست»
    6. cache
  COMMIT

capture(hold_ref, ledger_entry):                      -- در همان تراکنشی که LedgerEntry فروش ساخته می‌شود
    debit.state: 'held' → 'captured' ؛ debit.ledger_entry_id = ledger_entry.id ؛ captured_at
    (UPDATE ... WHERE hold_ref = :h AND state = 'held' ؛ rowcount = 0 → rollback همان تراکنش فروش)

release(hold_ref):                                    -- فروش انجام نشد
  tx:
    برای هر allocation همان debit: R1 یا R2 (بخش‌های ۱۲.۴ و ۱۲.۵)
    debit.state: 'held' → 'released' ؛ released_at
    (rowcount = 0 یعنی قبلاً captured یا released شده → بدون اثر)
  COMMIT
```

- allocation هرگز نیمه‌کاره ذخیره نمی‌شود: hold یک تراکنش است.
- LedgerEntry فروش و `captured` یک commit‌اند، پس «فروش ثبت‌شده بدون برداشت» و «برداشت captured بدون فروش» هیچ‌کدام ممکن نیست.
- capture و release روی `state='held'` شرطی‌اند و فقط یکی می‌برد.
- برای `manual_adjustment` منفی: همان hold بدون قدم ۱، مستقیم با `state='captured'`، `actor_admin_id` و `reason`.

#### قرارداد با provisioning

«اول remote، بعد تراکنش محلی» پذیرفته نیست: اگر remote ساخته شود و commit محلی شکست بخورد، منبع remote orphan می‌ماند. قرارداد کامل بین کیف‌پول و عملیات provisioning این است:

| موضوع | قرارداد |
|---|---|
| reference مشترک | `hold_ref` (UUID). عملیات provisioning آن را به‌عنوان `provisioning_ref` خودش یا ستونی از آن نگه می‌دارد؛ کیف‌پول `provisioning_ref` را روی debit ثبت می‌کند |
| ترتیب | ۱. `hold` (commit) ۲. ثبت durable گام‌های provisioning، هر اتصال با ردیف محلی staged و credential ذخیره‌شده، **قبل از** فراخوانی remote (commit) ۳. فراخوانی remote هر گام ۴. ثبت `remote_created` هر گام (commit) ۵. تراکنش نهایی: فعال‌شدن اتصال‌ها، ساخت Purchase، flush LedgerEntry فروش، `capture`، effectهای approval ؛ یک commit |
| idempotency token remote | هر گام remote یک هویت قطعی مشتق از `hold_ref` و slot همان اتصال (بخش ۵.۳) دارد که در منبع remote نوشته می‌شود (نام peer، comment، email کلاینت، نام کاربر hub)؛ تلاش دوباره با همان هویت، منبع دوم نمی‌سازد |
| حالت `remote_created` | گام provisioning این حالت‌ها را دارد: `staged` → `remote_calling` → `remote_created` → `active`؛ و در شکست `compensating` → `removed` یا `cleanup_required` |
| زمان ساخت ردیف‌ها | `wallet_debit_events`: قدم ۱. LedgerEntry فروش: فقط قدم ۵. `capture`: همان commit قدم ۵ |
| compensation | شکست در قدم ۳ تا ۵: برای هر گام `remote_calling` یا `remote_created` حذف remote با همان هویت؛ بعد از این‌که همه `removed` شدند، `release` |
| `cleanup_required` | اگر حذف remote شکست بخورد یا نبودن منبع اثبات نشود، عملیات provisioning `cleanup_required` می‌شود و **hold آزاد نمی‌شود**؛ پول مشتری `held` می‌ماند تا retry یا تصمیم superadmin |
| recovery پس از crash | worker عملیات با lease منقضی را برمی‌دارد: گام `remote_calling` با خواندن remote بر اساس هویت قطعی به `remote_created` یا `staged` برمی‌گردد؛ اگر client خواندن ندارد → `cleanup_required`. سپس یا ادامه تا capture، یا compensation |
| hold بدون عملیات provisioning زنده | worker هر debit `held` قدیمی‌تر از آستانه را که عملیات provisioning ترمینال ندارد در `wallet_hold_resolution` با state `cleanup_required` ثبت می‌کند؛ تصمیم (capture دستی با اشاره به LedgerEntry موجود، یا release) با superadmin است. آزادسازی خودکار ممنوع است، چون ممکن است سرویس ساخته شده باشد |

این قرارداد همه‌ی چیزی است که کیف‌پول از provisioning می‌خواهد. خودِ state machine durable provisioning (جدول گام‌ها، هویت قطعی هر پروتکل، خواندن remote هر client) طراحی جدایی است که هنوز تأیید و پیاده نشده.

**پیامد برای rollout (بخش ۲۱):**

- کیف‌پول هیچ نسخه‌ی موقت یا legacy از hold ندارد. `wallet_debit_events` و سه عمل بالا فقط در `phase='enforced'` وجود دارند.
- cutover (P9) پشت P6 مسدود است: state machine durable provisioning باید تأیید، پیاده و برای همه‌ی مسیرهای خرید فعال باشد. بدون آن، ورود به lot accounting روی مسیرهایی انجام می‌شد که orphan remote، سرویس رایگان بعد از شکست capture، تحویل ناقص با شارژ کامل و crash بین remote و commit محلی را دارند.
- **قبل از cutover** پرداخت کیف‌پولی درون همان state machine provisioning انجام می‌شود و reservation آن را **عملیات provisioning** نگه می‌دارد، نه کیف‌پول. این سند از آن طراحی می‌خواهد که reservation را ساخت‌یافته نگه دارد (نه در JSON): شناسه‌ی User، مبلغ، state از `reserved`/`captured`/`released`، و شناسه‌ی LedgerEntry بعد از capture. در `phase='normal'` پیاده‌سازی reserve/release همان UPDATE شرطی امروز روی `User.balance` از طریق شاخه‌ی legacy `wallet_service` است.
- **در cutover** تراکنش cutover (بخش ۲۰.۳) بعد از گرفتن قفل انحصاری state بررسی می‌کند که هیچ عملیات provisioning غیرترمینال با reservation کیف‌پولی وجود ندارد؛ اگر هست، rollback. پس هیچ reservation بازی، در هیچ نسلی، از cutover عبور نمی‌کند.
- **بعد از cutover** همان state machine برای reservation به‌جای مسیر legacy، `hold`/`capture`/`release` این بخش را صدا می‌زند؛ انتخاب با همان خواندن phase بخش ۹.۲ است.

### ۱۲.۴ Refund روی lot غیر-voided (R1)

`wallet_service.refund_debit(wallet_debit_event_id)` برای هر allocation آن برداشت:

```
INSERT wallet_allocation_reversals(allocation, reversed_amount = allocated, lot_was_voided = false)
lot.available_amount += r ؛ lot.consumed_amount -= r ؛ lot.state = 'active'
```

برای lot `active` و `exhausted` هر دو. مجموع `available + consumed` ثابت می‌ماند، پس CHECK نقض نمی‌شود (مثال: original=100، available=0، consumed=100، refund=100 → available=100، consumed=0). debit از `captured` به `reversed` می‌رود (و در `release` از `held` به `released`، با همین مکانیک). برگشت یک debit `captured` رویداد loyalty همان فروش را هم باطل و `loyalty_reconcile` را یک‌بار اجرا می‌کند (بخش ۱۴.۲). هیچ source یا lot جدیدی ساخته نمی‌شود.

### ۱۲.۵ Refund روی lot voided (R2)

پولی که از lot باطل‌شده خرج شده بود پول اسکمی بود. برگشت آن خرید پول قابل‌خرج نمی‌سازد؛ فقط **تعهد بدهی** را کم می‌کند. اگر بدهی قبلاً با پول سالم بیشتر از تعهد جدید تسویه شده، مازاد همان پول سالم آزاد می‌شود.

```
ورودی: allocation با مبلغ r روی lot L (state='voided')، بدهی D با source_wallet_lot_id = L
(اگر L بدهی ندارد - lot شخص ثالث با ردیف `wallet_writeoffs` - فقط ردیف reversal ثبت می‌شود و تمام؛ write-off ثبت‌شده immutable می‌ماند.)

tx (revalidate):
  1. INSERT wallet_allocation_reversals(allocation, r, lot_was_voided = true)  → rev
  2. E_new  = D.amount - (D.adjusted_amount + r)                 -- همیشه >= 0، چون Σ refundهای L <= consumed لحظه‌ی void
     excess = max(D.settled_amount - E_new, 0)
  3. آزادسازی مازاد، از جدیدترین settlement:
     برای هر application a از نوع debt_settlement روی D با effective(a) > 0، به ترتیب (created_at DESC, id DESC):
         x = min(effective(a), excess) ؛ اگر x = 0 توقف
         INSERT wallet_credit_application_reversals(a, amount = x, disposition='reallocate', effect_key = 'refund:{rev.id}')
         a.reversed_amount += x ؛ D.settled_amount -= x
         a.source.unapplied_amount += x ؛ a.source.state = 'applying'
         excess -= x ؛ freed.append(a.source)
  4. INSERT wallet_debt_adjustments(D, rev, amount = r) ؛ D.adjusted_amount += r ؛ D.state بازمحاسبه
  5. برای هر source در freed:  distribute(source, exclude_debt = D) ؛ source.state = 'applied'
  6. cache accountهای متأثر
COMMIT
```

ترتیب ۳ قبل از ۴ عمدی است: CHECK `settled_amount <= amount - adjusted_amount` در هر statement برقرار می‌ماند.

**چرا درست است:**

- تعهد به اندازه‌ی **کل** `r` کم می‌شود (قدم ۴)، نه فقط به اندازه‌ی remaining فعلی.
- فقط مازاد settlement نسبت به تعهد جدید آزاد می‌شود، و partial است (قدم ۳).
- پول آزادشده روی همان source اصلی می‌ماند (reversal + application جدید)؛ source جدیدی ساخته نمی‌شود و I-SRC برقرار می‌ماند.
- `exclude_debt = D` تضمین می‌کند پول آزادشده دوباره روی همان بدهی ننشیند.
- lot حاصل (`settlement_reallocation`) به source اصلی وصل است؛ اگر آن source بعداً باطل شود، این lot هم پیدا و claw-back می‌شود.

**مثال‌ها** (D.amount=100؛ همه‌ی settlementها از source سالم H):

| # | settled قبل | refund r | E_new | excess | آزادشده از H | adjusted بعد | settled بعد | remaining بعد | نتیجه برای مشتری |
|---|---|---|---|---|---|---|---|---|---|
| ۱ | 0 | 100 | 0 | 0 | 0 | 100 | 0 | 0 | بدهی بسته؛ پول قابل‌خرج جدید: ۰ |
| ۲ | 60 | 100 | 0 | 60 | 60 | 100 | 0 | 0 | بدهی بسته؛ ۶۰ سالم به lot برمی‌گردد |
| ۳ | 70 | 50 | 50 | 20 | 20 (partial) | 50 | 50 | 0 | بدهی بسته؛ ۲۰ سالم آزاد |
| ۴ | 30 | 50 | 50 | 0 | 0 | 50 | 30 | 20 | بدهی باز ۲۰ |
| ۵ | 100 (30 قدیم + 70 جدید) | 40 | 60 | 40 | 40 از settlement جدیدتر | 40 | 60 | 0 | ۴۰ سالم آزاد؛ settlement قدیم دست‌نخورده |
| ۶ | 100 | 100 | 0 | 100 | 100 | 100 | 0 | 0 | کل ۱۰۰ سالم آزاد؛ بدون ریال اضافه |

بررسی I-NET برای مثال ۲: قبل از refund: available=0، open_debt=40، gross سالم=60، net_debited=100 → `0 - 40 = 60 - 100`. بعد: available=60، open_debt=0، net_debited=0 → `60 - 0 = 60 - 0`. مشتری ۶۰ تومان واقعی داده و ۶۰ تومان دارد.

### ۱۲.۶ Void بخش کیف‌پول یک approval (V)

برای هر kind اجرا می‌شود، چون `new`/`renew` هم می‌توانند source پاداش داشته باشند.

```
resolve:  S* = همه‌ی wallet_credit_sources با causation_approval_uuid = X
          (source پاداش‌های loyalty که reconciliation بی‌اعتبار می‌کند با همین الگوریتم، ولی داخل تراکنش خود loyalty_reconcile برگردانده می‌شوند ؛ بخش ۱۴.۲.
           گام wallet_reversal سورس از قبل 'voided' را رد می‌کند)
          lock plan = identity هر account در S*  (+ pool کارت؛ بخش ۱۵)

tx (revalidate همه‌ی کلیدها):
  برای هر source s در S* به ترتیب id (اگر s.state = 'voided' رد می‌شود):
    s.state = 'voiding' ؛ s.voided_at = now ؛ s.void_operation_id = op
    الف) برای هر application a از نوع debt_settlement با effective(a) > 0:
           INSERT reversal(a, amount = effective(a), disposition='void_writeoff', effect_key='void:{op.id}')
           a.reversed_amount = a.amount ؛ بدهی مقصد: settled_amount -= مقدار ؛ state بازمحاسبه (دوباره باز می‌شود)
    ب) برای هر lot l با source_credit_source_id = s.id و state != 'voided'  (شامل lotهای settlement_reallocation):
           l.voided_available_amount = l.available_amount ؛ l.available_amount = 0
           l.state = 'voided' ؛ l.voided_at = now ؛ l.void_operation_id = op
           اگر l.consumed_amount > 0:
               اگر identity(account l) = identity مشتری همین approval:
                   INSERT wallet_debts(identity, account, lot = l, op, amount = l.consumed_amount, state='open')
               وگرنه:
                   INSERT wallet_writeoffs(op, identity(account l), account, lot = l, amount = l.consumed_amount,
                                           reason='third_party_reward_consumed')      -- UNIQUE(op, lot)؛ بدهی ساخته نمی‌شود
    ج) s.state = 'voided'          (unapplied_amount باید 0 باشد؛ CHECK)
    د) mark_effect_reversed(effect wallet_credit_source_created برای s، state طبق بخش ۱۴.۳، reversed_value = مقدار پس‌گرفته‌شده، op)
  cache همه‌ی accountهای متأثر
COMMIT
```

- topupی که کامل صرف بدهی شده و lot ندارد: فقط شاخه‌ی (الف) اجرا می‌شود.
- topup نیمی بدهی، نیمی lot: هر دو شاخه.
- lot شخص ثالث (پاداش معرف): باقی‌مانده پس گرفته می‌شود؛ بخش خرج‌شده یک ردیف `wallet_writeoffs` می‌شود، نه بدهی، چون معرف لزوماً در تقلب دخیل نبوده. این پیش‌فرض در بخش ۲۳ برای تأیید Product آمده.
- بدهی تازه به‌طور خودکار از lotهای سالم موجود همان identity کسر نمی‌شود (بخش ۲۳).

### ۱۲.۷ Predicateهای خرید و topup

یک ماژول `wallet_policy` دو تابع دارد و هیچ کد دیگری این تصمیم را نمی‌گیرد:

| Predicate | رد می‌کند وقتی | کد خطا |
|---|---|---|
| `can_purchase(account)` برای ساخت User با خرید، خرید، تمدید و hold | `User.purchases_blocked` خود account یا هر User هم‌Telegram (رفتار امروز `_ensure_telegram_can_buy`)؛ **یا** `open_debt(identity) > 0` | 403 `purchases_blocked` ؛ 409 `open_debt` |
| `can_topup(account)` برای `receipt_topup` و ثبت رسید topup | فقط `wallet_accounts.topup_blocked = True` | 403 `topup_blocked` |

- بدهی باز و fraud hold (`purchases_blocked` که ابطال می‌گذارد) topup را رد **نمی‌کنند**؛ مشتری با topup بدهی‌اش را تسویه می‌کند.
- `manual_adjustment` ادمین تابع هیچ‌کدام نیست.
- `_ensure_can_buy` در `add_balance` (`routers/bot.py:1402`) با `can_topup` جایگزین می‌شود؛ سه فراخوانی دیگر (۸۹۶، ۱۲۷۷، ۱۳۱۹) و `_ensure_telegram_can_buy` با `can_purchase`.
- HTTP، `PanelBridge` و `RemoteBridge` هر سه به همان توابع router می‌رسند، پس policy یکی است. پیش‌بررسی UX بات (`customer_common.py`) نتیجه‌ی همین دو predicate را از API می‌خواند و خودش تصمیم نمی‌گیرد.
- **حفظ رفتار قفل‌های موجود:** امروز «قفل خرید» topup را هم می‌بندد. در cutover برای هر User با `purchases_blocked = True` مقدار `topup_blocked = True` نوشته می‌شود. فرم «قفل خرید» پنل از آن پس دو گزینه‌ی جدا دارد؛ گزینه‌ی topup پیش‌فرض روشن است تا قفل دستی ادمین همان معنای قبل را بدهد. fraud hold ابطال فقط `purchases_blocked` را می‌گذارد.

---

## ۱۳. State machine ابطال

`wallet_operations(operation_type='void_receipt', business_key=approval_uuid)`. اجرای دوم، هر actorی، همان operation را برمی‌گرداند.

**واجد ابطال** فقط approvalی است که: `state='completed'`، `registered_under_mode='required'`، و `completeness_generation >= 1` (بخش ۵.۵). approval ثبت‌شده در shadow، یا قبل از activation مشترک، هرگز قابل ابطال نیست: preview و execute ۴۰۹ `approval_not_voidable_generation`.

### ۱۳.۱ دامنه‌ی هر kind

| kind / `target_shape` | دامنه‌ی منابع | دامنه‌ی مالی |
|---|---|---|
| `new` / `new_user` | **کل account**: User + همه‌ی Purchaseها و Connectionهایش | فقط effectهای همین approval |
| `new` / `existing_user` | فقط Purchase ساخته‌شده و Connectionهای آن. User می‌ماند با fraud hold | فقط effectهای همین approval |
| `renew` | **کل account** (تصمیم Product: تمدید اسکمی کل حساب را آلوده می‌کند)، شامل سرویس‌ها و Connectionهای سالم قبلی | فقط effectهای همین approval؛ فروش‌ها و پرداخت‌های معتبر قبلی دست نمی‌خورند |
| `topup` | بدون حذف منبع: User و سرویس‌های سالم می‌مانند. fraud hold + بدهی | بخش ۱۲.۶ |

برای `new_user` و `renew`، preview اگر approvalهای دیگری روی همان account وجود دارد صریحاً هشدار می‌دهد و آن‌ها را فهرست می‌کند.

**fraud hold** یعنی `User.purchases_blocked = True` با `purchases_blocked_reason` کددار `receipt_void:{approval_uuid}`. طبق بخش ۱۲.۷ فقط خرید و تمدید را می‌بندد. `topup_blocked` را ابطال **نمی‌نویسد**، پس مشتری باقی‌مانده می‌تواند با topup بدهی را تسویه کند. برداشتن fraud hold کار ادمین است و مستقل از صفرشدن بدهی.

### ۱۳.۲ گام‌ها (ترتیب ثابت)

| # | `step_key` | kindها | کار | تراکنش |
|---|---|---|---|---|
| ۱ | `claim` | همه | INSERT operation؛ approval → `voiding`؛ lock plan | محلی |
| ۲ | `block` | همه | fraud hold؛ برای دامنه‌ی «کل account»: `status` غیرفعال؛ هر Connection دامنه `enabled=False` در DB | یک تراکنش محلی |
| ۳ | `deprovision:{connection_id}` | `new`، `renew` | حذف remote همان Connection با تابع deprovision موجود همان پروتکل؛ **تنها جایی که remote صدا زده می‌شود** | یک گام به‌ازای هر Connection |
| ۴ | `verify:{connection_id}` | `new`، `renew` | تعیین `remote_outcome` (۱۳.۴) | یک گام به‌ازای هر Connection |
| ۵ | `db_delete` | `new`، `renew` | finalizer فقط-DB (۱۳.۷) | یک تراکنش محلی |
| ۵a | `loyalty_reconciliation` | `new`، `renew` | بخش ۱۴.۲؛ source اختصاصی هر پاداش بی‌اعتبارشده در همین گام و همین تراکنش با الگوریتم ۱۲.۶ برگردانده می‌شود | یک تراکنش محلی |
| ۶ | `wallet_reversal` | همه | بخش ۱۲.۶ | یک تراکنش محلی |
| ۷ | `ledger_reversal` | همه | بخش ۱۴ | یک تراکنش محلی |
| ۸ | `card_reversal` | هر approval با کارت | بخش ۱۵.۵ | یک تراکنش محلی |
| ۹ | `finalize` | همه | approval → `voided`؛ operation → `completed`؛ release | محلی |

برای `topup` گام‌های ۳ تا ۵ وجود ندارند.

گام ۲ فوری و محلی است: RADIUS وضعیت `Connection.enabled` را زنده می‌خواند، پس PPP بلافاصله رد می‌شود. برای WireGuard/Xray/SoftEther قطع واقعی با گام ۳ است.

**پیش‌شرط گام ۵:** همه‌ی گام‌های `deprovision:*` در `done` یا `abandoned`، و همه‌ی گام‌های `verify:*` در `done` (با `verified_absent` یا `delete_idempotently_absent`) یا `abandoned`. هر حالت دیگر گام ۵ را شروع نمی‌کند.

### ۱۳.۳ شکست و `cleanup_required`

| وضعیت | نتیجه | چه چیزی برقرار می‌ماند |
|---|---|---|
| شکست گام ۲ | operation `failed`؛ approval به `completed` برمی‌گردد | هیچ اثری اعمال نشده |
| شکست گام ۳ | retry با backoff؛ پس از سقف تلاش: operation و approval → `cleanup_required` | حساب مسدود؛ گام‌های ۵ تا ۹ اجرا نشده؛ UI node و Connection را نشان می‌دهد |
| گام ۴ با `unverified` | گام `blocked`؛ operation و approval → `cleanup_required` | مثل بالا؛ ردیف‌های DB و User پاک **نمی‌شوند** |
| شکست گام ۵ | rollback همان تراکنش؛ retry | گام‌های ۳ و ۴ `done` می‌مانند و **دوباره اجرا نمی‌شوند** |
| شکست گام ۶، ۷، ۸ | rollback همان تراکنش؛ retry | هر گام مستقل idempotent است |
| crash در هر نقطه | operation `running` با lease منقضی | worker از اولین گام غیرترمینال ادامه می‌دهد |

retry (`POST .../retry`) از اولین گام غیر-`done`/`abandoned` ادامه می‌دهد. retry یک گام `blocked` همان تأیید را دوباره امتحان می‌کند.

### ۱۳.۴ نتیجه‌ی تأیید remote

| `remote_outcome` | معنا | state گام | ادامه؟ |
|---|---|---|---|
| `verified_absent` | client عملیات خواندن دارد و نبودن منبع خوانده شد | `done` | بله |
| `delete_idempotently_absent` | client خواندن ندارد، ولی اجرای دوم حذف صریحاً «از قبل نیست» برگرداند | `done` | بله |
| `unverified` | نه خواندن ممکن است و نه حذف دوم پاسخ قابل‌تشخیص «نیست» می‌دهد؛ یا پاسخ مبهم است | `blocked` | خیر؛ `cleanup_required` |
| `abandoned` | superadmin با force پذیرفته که منبع ممکن است بماند | `abandoned` | بله |

هیچ مسیری `unverified` را `done` نمی‌کند. این سند idempotency هیچ client remote را فرض نمی‌کند: پروتکلی که پاسخ «از قبل نیست» قابل‌تشخیص ندارد همیشه به `unverified` می‌رسد.

### ۱۳.۵ Force

`POST /api/accounting/receipt-void/force-step/{operation_id}` (superadmin + رمز + دلیل + تأیید متنی) یک گام `deprovision:*` شکست‌خورده یا `verify:*` در `blocked` را `abandoned` می‌کند (`remote_outcome='abandoned'`). منبع remote به‌عنوان «orphan برای پاک‌سازی دستی» با node و شناسه‌ی غیرحساس در `result_snapshot` همان گام ثبت می‌شود، همراه با actor و دلیل. سپس گام‌های بعدی ادامه می‌یابند.

### ۱۳.۶ بعد از حذف User

| موجودیت | وضعیت |
|---|---|
| `wallet_accounts` | tombstone؛ lotها و debitها برای audit باقی |
| lotهای سالم باقی‌مانده‌ی آن account | دست‌نخورده، غیرقابل‌خرج؛ در preview و audit با مبلغ نمایش داده می‌شود (سرنوشت آن: بخش ۲۳) |
| debit `held` آن account | قبل از گام ۵ باید نباشد؛ اگر هست، گام ۵ متوقف و operation `cleanup_required` |
| `customer_identities` | باقی؛ بدهی و مسدودیت روی آن می‌ماند و روی هر account آینده‌ی همان شخص اثر دارد |
| `LedgerEntry`ها | با snapshot باقی |
| شواهد رسید (`receipt_file_id_snapshot`) | باقی |

### ۱۳.۷ Finalizer فقط-DB

`delete_user_cascade` امروز (بخش ۳.۳) هم remote را صدا می‌زند و هم خودش commit می‌کند؛ گام ۵ نمی‌تواند از آن استفاده کند. تفکیک:

```
finalize_user_deletion_after_deprovision(db, user):        -- بدون هیچ فراخوانی remote ؛ بدون commit
    حذف RadiusActiveSession اتصال‌های user
    حذف UsageLog user
    جداکردن RadiusLimitEventLog (user_id و connection_id → NULL)
    wallet_accounts: user_id = NULL ، tombstoned_at = now
    loyalty_purchase_events و loyalty_reward_events همان user: user_id = NULL  (ردیف‌ها می‌مانند)
    db.delete(user)                                         -- cascade ORM موجود برای Connection و Purchase

finalize_purchase_deletion_after_deprovision(db, purchase): -- برای دامنه‌ی existing_user ؛ بدون remote ؛ بدون commit
    حذف RadiusActiveSession اتصال‌های همان purchase
    جداکردن RadiusLimitEventLog همان اتصال‌ها
    حذف Connectionهای همان purchase ؛ حذف purchase

delete_user_cascade(db, user):                              -- رفتار امروز برای callerهای فعلی، بدون تغییر
    برای هر Connection: deprovision_connection(conn)
    finalize_user_deletion_after_deprovision(db, user)
    db.commit()
```

- گام ۵ فقط یکی از دو finalizer را داخل تراکنش خودش صدا می‌زند و خودش commit می‌کند؛ `delete_user_cascade` و `deprovision_connection` را صدا نمی‌زند.
- پس هر Connection دقیقاً یک‌بار deprovision می‌شود (گام ۳)، و شکست تراکنش گام ۵ فقط همان تراکنش را برمی‌گرداند.
- سه caller فعلی `delete_user_cascade` (`routers/users.py:857`، `routers/bot.py:1447`، `user_ops.py:566`) همان رفتار قبل را دارند، با این تفاوت که tombstone account حالا داخل finalizer انجام می‌شود.
- تست AST (RV-62) ثابت می‌کند `db.delete` روی User فقط داخل `finalize_user_deletion_after_deprovision` است و کد گام ۵ هیچ ارجاعی به `deprovision_connection` یا `delete_user_cascade` ندارد.

---

## ۱۴. Effect Matrix

هر اثر دقیقاً با یک مکانیزم خنثی می‌شود.

| اثر | مکانیزم | idempotency |
|---|---|---|
| `sale_new` / `sale_renew` همین approval | `voided_at` + `void_operation_id` روی ردیف اصلی؛ یک ردیف `kind='receipt_void'` با `amount=0` و `reversal_of_id` = ردیف اصلی | `UNIQUE(reversal_of_id)` |
| `wallet_topup` همین approval | مثل بالا روی Ledger؛ اثر موجودی با بخش ۱۲.۶ | مثل بالا + state source |
| `admin_credit_spend` همین approval | **بدون** `voided_at`. یک ردیف `admin_credit_refund` به همان مبلغ با `reversal_of_id` = ردیف spend، و افزایش `AdminUser.balance` در همان تراکنش | `UNIQUE(reversal_of_id)` |
| پاداش اعتبار referral/loyalty | بخش ۱۲.۶ (source با `causation_approval_uuid`) | state source |
| پاداش حجم referral/loyalty (`quota_reward_granted`) | بخش ۱۴.۱ | `reversal_state` روی effect + `UNIQUE` shortfall |
| خرید loyalty (`loyalty_purchase_recorded`) و هر پاداشی که دیگر entitlement ندارد | بخش ۱۴.۲: void رویداد خرید، سپس `loyalty_reconciliation` | state رویدادها |
| discount redemption | `discount_code_redemptions.voided_at`؛ `used_count` از `COUNT(*) WHERE voided_at IS NULL` بازمحاسبه و نوشته می‌شود | `voided_at` |
| پرداخت کارت | بخش ۱۵.۵ | `voided_at` روی event |

چرا `admin_credit_spend` استثناست: فرمول هزینه `spend - refund` است. اگر spend هم از جمع حذف شود و refund هم کم شود، هزینه منفی می‌شود. `voided_at` روی Ledger فقط برای `sale_new`، `sale_renew`، `wallet_topup` نوشته می‌شود.

### ۱۴.۱ پاداش حجمی: مقصد، اعمال و برگشت

**وضعیت امروز.** پاداش حجمی همیشه `User.total_quota_bytes` را می‌نویسد (`services/user_ops.py:98`، `:192`، `:196`). ولی:

- quota صفر یعنی نامحدود (`models.py:792`، `:1042`)؛ جمع‌زدن روی صفر حساب نامحدود را محدود می‌کند.
- در مسیر ساخت User از بات، `absorb_legacy_pool_into_purchase` (`routers/bot.py:856`، `services/user_ops.py:1307`) قبل از `apply_referral` اجرا می‌شود. وقتی User Purchase دارد، quota مؤثر از Purchase خوانده می‌شود و ستون سطح User می‌تواند بی‌اثر باشد.
- معرف ممکن است چند Purchase داشته باشد.

پس «نوشتن روی User» همیشه مقصد درستی نیست و برگشت آن هم نمی‌تواند باشد.

**Policy مقصد (پیش‌فرض این سند؛ تصمیم Product در بخش ۲۳):** پاداش حجمی، چه referral چه loyalty، فقط وقتی اعمال می‌شود که دقیقاً یک مقصد quota‌دار بدون ابهام و محدود وجود داشته باشد. پاداش اعتباری همان شخص مستقل از این اعمال می‌شود. یک resolver برای هر دو نوع پاداش:

```
resolve_quota_reward_target(purchases_after, user_quota_after):
  -- ورودی «topology بعد از همین approval» است، نه وضعیت لحظه‌ی ثبت
  اگر purchases_after خالی است:
      user_quota_after > 0            → User
      وگرنه                            → None      (نامحدود)
  اگر دقیقاً یک Purchase:
      quota_bytes آن > 0              → همان Purchase
      وگرنه                            → None      (نامحدود)
  اگر بیش از یک Purchase               → None      (مبهم)
```

`None` یعنی ردیف quota در manifest نیست و پاداش حجمی اعمال نمی‌شود؛ بی‌صدا روی منبع دیگری نوشته نمی‌شود.

| گیرنده | topology بعد از approval | binding در manifest |
|---|---|---|
| معرف (referral) | Purchaseهای فعلی معرف؛ این approval چیزی به او اضافه نمی‌کند | `{"type","id"}` واقعی |
| کاربر جدید (`new_user`)، با اتصال | دقیقاً یک Purchase: همان که این approval می‌سازد | `{"ref": "effect:purchase_created:purchase"}` اگر quota پکیج محدود است |
| کاربر جدید، بدون اتصال | بدون Purchase | `{"ref": "effect:user_created:user"}` اگر quota محدود است |
| `existing_user` (خرید تازه) | Purchaseهای موجود + Purchase تازه (+ Purchase حاصل از absorb، اگر pool legacy دارد) | فقط اگر حاصل دقیقاً یک Purchase است: `{"ref": "effect:purchase_created:purchase"}`؛ وگرنه `None` |
| `renew_purchase` | Purchaseهای موجود | اگر User دقیقاً همان یک Purchase را دارد: `{"type":"Purchase","id"}`؛ وگرنه `None` |
| `renew_user` (legacy) | بدون Purchase | `{"type":"User","id"}` اگر quota محدود است |

- **ترتیب اعمال:** پاداش حجمی (referral و loyalty) فقط **بعد از** تثبیت topology اعمال می‌شود: بعد از ساخت Purchase و بعد از `absorb_legacy_pool_into_purchase`. امروز loyalty `new_user` قبل از ساخت Purchase روی User نوشته می‌شود (۳.۳)؛ در مسیر approval این فراخوانی به بعد از ساخت Purchase منتقل می‌شود تا روی همان مقصد manifest بنویسد. binding `ref` همین ترتیب را enforce می‌کند (۵.۳).
- پیش‌شرط اجرا (بخش ۵.۳) مقصدهای `{"type","id"}` را دوباره resolve می‌کند؛ اختلاف با manifest → `cancelled`.
- اعمال پاداش دقیقاً همان منبع را می‌نویسد. effect این‌ها را ثبت می‌کند: `resource_type`، `resource_id` واقعی، `delta_value`، و در `resource_snapshot`: `quota_before`، `quota_after`، `before_was_unlimited`. `record_effect` با projection بررسی می‌کند که منبع نوشته‌شده همان binding است.
- برگشت دقیقاً همان `resource_id` effect را لمس می‌کند، نه چیز دیگری.

ورودی برگشت یک «ثبت اعطای حجم» است: یا effect `quota_reward_granted` (پاداش approvalدار)، یا ردیف `loyalty_reward_events` (پاداش loyalty بدون approval). هر دو همان factها را دارند (`resource_type`/`resource_id` یا `quota_target_*`، `quota_before`، `quota_after`، `before_was_unlimited`، مقدار) و نتیجه روی همان ثبت نوشته می‌شود. پایین هر دو را «effect» می‌نامد.

**برگشت**، روی منبع `R` همان effect، با ستون quota `q` و مصرف `u` (`total_quota_bytes`/`used_bytes` برای User؛ `quota_bytes`/`used_bytes` برای Purchase):

```
tx:
  اگر R در همین ابطال حذف می‌شود یا دیگر وجود ندارد    → reversal_state = 'resource_deleted' ؛ پایان
  اگر R.q != effect.quota_after                        → گام blocked ؛ operation cleanup_required
                                                          (quota بعد از پاداش توسط مسیر دیگری عوض شده؛ سهم پاداش دیگر قابل‌تفکیک نیست)
  اگر effect.before_was_unlimited:                     -- فقط اگر Product رفتار امروز را برای منبع نامحدود نگه دارد
      R.q = 0                                           → 'fully_reversed' ، reversed_value = granted
  وگرنه:
      reversible = max(min(granted, R.q - R.u), 0) ؛ shortfall = granted - reversible
      R.q -= reversible
      shortfall = 0 → 'fully_reversed'
      shortfall > 0 → 'partially_reversed' ، reversed_value = reversible ، INSERT quota_reversal_shortfalls
  reconcile وضعیت اتصال‌ها با همان تابعی که بعد از هر تغییر مستقیم quota صدا زده می‌شود
COMMIT
```

- effect فقط وقتی `fully_reversed` است که کل `granted` برگشته باشد. `partially_reversed` یک ردیف durable shortfall دارد و در audit و preview نمایش داده می‌شود.
- شاخه‌ی `before_was_unlimited` مقدار را دقیقاً به صفر (نامحدود) برمی‌گرداند، نه به `used_bytes`. با policy پیش‌فرض این شاخه هرگز اجرا نمی‌شود، چون برای منبع نامحدود effectی ساخته نمی‌شود.
- در شاخه‌ی محدود با shortfall مثبت، `q = u` می‌شود؛ منبع در حد quota است و enforcement موجود (poller و RADIUS) اتصال‌هایش را می‌بندد تا خرید بعدی. مثال: quota سالم 10GB، پاداش 5GB، مصرف 12GB → reversible = 3GB، shortfall = 2GB، quota بعد = 12GB = مصرف.
- `blocked` با force قابل عبور است (superadmin، دلیل)؛ effect در آن حالت `reversal_state='not_applicable'` می‌گیرد.
- shortfall بدهی پولی نمی‌سازد و خرید بعدی را مسدود نمی‌کند (بخش ۲۳).
- رزرو تمدید (`reserved_quota_bytes`) در این الگوریتم نیست: اگر منبع در لحظه‌ی ابطال رزرو فعال دارد، شرط `R.q != quota_after` یا برابری آن تعیین‌کننده است و حالت مبهم به `blocked` می‌رسد.

### ۱۴.۲ loyalty: ledger، مدرک پرداخت، epoch آستانه، reconciliation

رفتار امروز (۳.۳) سه مشکل دارد: تمدید رزروشده‌ی User پاداش را مدت‌ها بعد و بدون ارتباط با approval می‌سازد؛ تمدید Purchase اصلاً شمرده نمی‌شود؛ و پاداش یک «حق مشتق از زنجیره‌ی خریدها» است، نه اثر محلی یک approval: اگر خرید قدیمی باطل شود، پاداشی که خرید بعدی ساخته ممکن است دیگر پشتوانه نداشته باشد.

این بخش فقط بعد از cutover فاز P9a فعال است (`timing_mode='payment_event'`). تا آن زمان loyalty دقیقاً مثل امروز کار می‌کند، هیچ ردیف loyalty در manifest نیست، و ابطال رسید (P11) هم در دسترس نیست.

#### جدول‌ها

```
-- mode زمان شمارش loyalty در receipt_approval_runtime_state.loyalty_timing_mode است (بخش ۵.۵) ؛ جدول runtime جدا ندارد

loyalty_policy_epochs                     -- هر تغییر آستانه یا مقدار پاداش یک epoch تازه ؛ immutable جز closed_at
id               PK
threshold        INT      NOT NULL CHECK (threshold >= 0)      -- 0 = loyalty خاموش
credit_amount    BIGINT   NOT NULL CHECK (credit_amount >= 0)
quota_bytes      BIGINT   NOT NULL CHECK (quota_bytes >= 0)
generation       INT      NOT NULL CHECK (generation >= 1)      -- نسل activation مشترک
policy_version   INT      NOT NULL                              -- شمارنده‌ی افزایشی ؛ UNIQUE
started_at       NOT NULL
closed_at        NULL
actor_admin_id   INT      NULL

UNIQUE (policy_version)
-- دقیقاً یک epoch باز: سرویس تضمین می‌کند ؛ job تطبیق می‌سنجد

loyalty_user_baselines                    -- شمارنده‌های قبل از cutover ؛ immutable ؛ غیرقابل‌ابطال
id                         PK
user_id                    INT     NOT NULL                  -- بدون FK ؛ finalizer حذف User این ردیف را حذف نمی‌کند
generation                 INT     NOT NULL CHECK (generation >= 1)
purchase_count_baseline    INT     NOT NULL CHECK (purchase_count_baseline >= 0)
rewards_given_baseline     INT     NOT NULL CHECK (rewards_given_baseline >= 0)
captured_at                NOT NULL

UNIQUE (user_id, generation)              -- baseline معتبر هر User = ردیف generation جاری

loyalty_user_epoch_anchors                -- نقطه‌ی شروع پیشرفت هر User در هر epoch ؛ immutable
id                          PK
user_id                     INT     NULL                 -- finalizer حذف User آن را NULL می‌کند
user_id_snapshot            INT     NOT NULL
epoch_id                    BIGINT  NOT NULL  FK loyalty_policy_epochs(id) ON DELETE RESTRICT
starts_after_active_count   INT     NOT NULL CHECK (starts_after_active_count >= 0)
created_at                  NOT NULL

UNIQUE (user_id, epoch_id)                -- NULL-exempt عمداً

sale_payment_evidence                     -- مدرک backend-owned پرداخت یک فروش ؛ immutable
id                             PK
sale_ledger_entry_id           INT         NOT NULL  FK ledger_entries(id) ON DELETE RESTRICT
tenant_scope_key               VARCHAR(64) NOT NULL
evidence_kind                  VARCHAR(20) NOT NULL  CHECK IN ('receipt_approval','wallet_capture','admin_credit_spend')
approval_uuid                  CHAR(36)    NULL      FK receipt_approvals(approval_uuid) ON DELETE RESTRICT
wallet_debit_event_id          BIGINT      NULL      FK wallet_debit_events(id) ON DELETE RESTRICT
credit_spend_ledger_entry_id   INT         NULL      FK ledger_entries(id) ON DELETE RESTRICT
wallet_account_id              BIGINT      NULL                 -- account فروش ؛ برای wallet_capture الزامی
user_id_snapshot               INT         NOT NULL
sale_amount                    BIGINT      NOT NULL  CHECK (sale_amount > 0)       -- = LedgerEntry.amount همان فروش (تومان)
captured_amount                BIGINT      NOT NULL  CHECK (captured_amount > 0)   -- مبلغ مدرک: amount_snapshot approval، amount debit، یا amount ردیف spend (تومان)
created_at                     NOT NULL

UNIQUE (sale_ledger_entry_id)                         -- یک فروش، یک مدرک
CHECK (evidence_kind = 'admin_credit_spend' OR sale_amount = captured_amount)
UNIQUE (approval_uuid)                                -- NULL-exempt ؛ یک مدرک، یک فروش
UNIQUE (wallet_debit_event_id)                        -- NULL-exempt
UNIQUE (credit_spend_ledger_entry_id)                 -- NULL-exempt
CHECK ((evidence_kind = 'receipt_approval'   AND approval_uuid IS NOT NULL AND wallet_debit_event_id IS NULL AND credit_spend_ledger_entry_id IS NULL)
    OR (evidence_kind = 'wallet_capture'     AND wallet_debit_event_id IS NOT NULL AND approval_uuid IS NULL AND credit_spend_ledger_entry_id IS NULL)
    OR (evidence_kind = 'admin_credit_spend' AND credit_spend_ledger_entry_id IS NOT NULL AND approval_uuid IS NULL AND wallet_debit_event_id IS NULL))

loyalty_purchase_events                   -- append-only
id                    PK
user_id               INT         NULL                 -- finalizer حذف User آن را NULL می‌کند
user_id_snapshot      INT         NOT NULL
payment_evidence_id   BIGINT      NOT NULL  FK sale_payment_evidence(id) ON DELETE RESTRICT
ledger_entry_id       INT         NOT NULL  FK ledger_entries(id) ON DELETE RESTRICT
approval_uuid         CHAR(36)    NULL                 -- کپی از مدرک، برای lookup ابطال
eligibility_reason    VARCHAR(24) NOT NULL  CHECK IN ('card_paid','wallet_captured','admin_credit_paid')
completeness_generation  INT      NOT NULL  CHECK (completeness_generation >= 1)   -- نسل activation مشترک در لحظه‌ی ثبت
state                 VARCHAR(8)  NOT NULL  CHECK IN ('active','voided')
voided_at             NULL
void_operation_id     BIGINT      NULL
created_at            NOT NULL

UNIQUE (payment_evidence_id)
UNIQUE (ledger_entry_id)
INDEX  (user_id, state)
INDEX  (approval_uuid)
INDEX  (completeness_generation)
CHECK  ((state = 'voided') = (voided_at IS NOT NULL))

loyalty_reward_events                     -- append-only ؛ هر پاداش یک ردیف، یک source و effectهای خودش
id                            PK
user_id                       INT      NULL
user_id_snapshot              INT      NOT NULL
epoch_id                      BIGINT   NOT NULL  FK loyalty_policy_epochs(id) ON DELETE RESTRICT
slot                          INT      NOT NULL  CHECK (slot >= 1)          -- شماره‌ی entitlement داخل epoch
reearn_generation             INT      NOT NULL  CHECK (reearn_generation >= 1)   -- ۱ برای اولین اعطا ؛ +۱ برای هر بازکسب همان slot بعد از ابطال. ربطی به completeness_generation ندارد
required_active_count         INT      NOT NULL  CHECK (required_active_count >= 1)   -- مطلق و ثابت
triggering_purchase_event_id  BIGINT   NOT NULL  FK loyalty_purchase_events(id) ON DELETE RESTRICT
causation_type                VARCHAR(20) NOT NULL CHECK IN ('receipt_approval','wallet_capture','admin_credit_spend')   -- = evidence_kind خرید محرک
causation_approval_uuid       CHAR(36) NULL                -- فقط وقتی causation_type = 'receipt_approval'
causation_wallet_debit_event_id      BIGINT NULL           -- فقط وقتی 'wallet_capture'
causation_credit_spend_ledger_entry_id INT  NULL           -- فقط وقتی 'admin_credit_spend'
credit_amount                 BIGINT   NOT NULL DEFAULT 0
quota_bytes                   BIGINT   NOT NULL DEFAULT 0
wallet_credit_source_id       BIGINT   NULL                -- source اختصاصی همین پاداش
quota_target_type             VARCHAR(8) NULL
quota_target_id               INT      NULL
quota_before                  BIGINT   NULL                -- factهای اعطای حجم ؛ همان‌ها که ۱۴.۱ برای برگشت می‌خواند
quota_after                   BIGINT   NULL
before_was_unlimited          BOOLEAN  NULL
credit_reversal_state         VARCHAR(24) NULL  CHECK IN ('fully_reversed','reversed_with_debt','reversed_with_writeoff','unrecoverable')
credit_reversed_amount        BIGINT   NULL
quota_reversal_state          VARCHAR(24) NULL  CHECK IN ('fully_reversed','partially_reversed','resource_deleted','not_applicable')
quota_reversed_bytes          BIGINT   NULL
state                         VARCHAR(8) NOT NULL CHECK IN ('active','voided')
void_reason                   VARCHAR(24) NULL CHECK IN ('receipt_void','loyalty_reconciliation','sale_refund')
voided_at                     NULL
void_operation_id             BIGINT   NULL
created_at                    NOT NULL

UNIQUE (user_id, epoch_id, slot, reearn_generation)   -- NULL-exempt عمداً
INDEX  (user_id, state, required_active_count)
CHECK  ((state = 'voided') = (voided_at IS NOT NULL AND void_reason IS NOT NULL AND void_operation_id IS NOT NULL))
CHECK  (credit_amount > 0 OR quota_bytes > 0)                                         -- پاداش با هر دو مقدار صفر ساخته نمی‌شود
CHECK  ((credit_reversal_state IS NOT NULL) = (state = 'voided' AND credit_amount > 0))
CHECK  ((credit_reversed_amount IS NOT NULL) = (credit_reversal_state IS NOT NULL))
CHECK  ((quota_reversal_state IS NOT NULL) = (state = 'voided' AND quota_bytes > 0))
CHECK  ((quota_reversed_bytes IS NOT NULL) = (quota_reversal_state IN ('fully_reversed','partially_reversed')))
CHECK  ((causation_type = 'receipt_approval')   = (causation_approval_uuid IS NOT NULL))
CHECK  ((causation_type = 'wallet_capture')     = (causation_wallet_debit_event_id IS NOT NULL))
CHECK  ((causation_type = 'admin_credit_spend') = (causation_credit_spend_ledger_entry_id IS NOT NULL))
```

هیچ CHECKی `required_active_count` را به حاصل‌ضرب index در آستانه نمی‌بندد؛ مقدار یک‌بار در اعطا محاسبه و برای همیشه ثابت می‌ماند.

**منبع حقیقت هر پاداش ردیف `loyalty_reward_events` است**، همراه source اختصاصی کیف‌پول و factهای حجم روی همان ردیف. این برای هر سه `causation_type` یکسان است. `receipt_approval_effects` فقط برای پاداش‌هایی وجود دارد که `causation_type='receipt_approval'` دارند (چون `approval_uuid` آن جدول اجباری است)؛ برای پاداش فروش کیف‌پولی یا اعتبار reseller هیچ effect ساختگی ساخته نمی‌شود و audit، idempotency و برگشت از خود ردیف پاداش، source آن و عملیات محرک تأمین می‌شود.

منبع حقیقت شمارنده‌ها جدول‌های رویداد است. `User.purchase_count` و `User.loyalty_rewards_given` cache‌اند:

```
baseline = ردیف loyalty_user_baselines آن user با generation جاری (نبود = صفر)
active_count(user) = baseline.purchase_count_baseline + COUNT(loyalty_purchase_events فعال آن user)
live_rewards(user) = baseline.rewards_given_baseline  + COUNT(loyalty_reward_events فعال آن user)
User.purchase_count = active_count ؛ User.loyalty_rewards_given = live_rewards
```

هر mutation loyalty هر دو cache را در همان تراکنش می‌نویسد و job تطبیق برابری‌شان را می‌سنجد.

#### مدرک پرداخت و واجدبودن

`LedgerEntry` به‌تنهایی مدرک پرداخت نیست: `_record_bot_sale` (`routers/bot.py:278`) `paid_amount` و `payment_method` را از بدنه‌ی درخواست می‌گیرد و `/api/bot` برای integration بیرونی هم باز است. `amount_source='explicit'` فقط می‌گوید مبلغ از payload آمده. پس واجدبودن به یک ردیف `sale_payment_evidence` بسته است که **فقط backend** و در همان تراکنش ثبت فروش می‌سازد:

| `evidence_kind` | چه کسی می‌سازد | validator (همه لازم؛ هر شکست تراکنش فروش را rollback می‌کند) |
|---|---|---|
| `receipt_approval` | `record_effect` هنگام نوشتن effect `ledger_sale` | approval موجود، `registered_under_mode='required'`، `completeness_generation` جاری، state `mutating`، kind `new`/`renew`؛ `tenant_scope_key` برابر؛ User فروش همان target approval (`target_user_id_snapshot`، یا User ساخته‌ی effect `user_created`)؛ برای `renew_purchase` همان `purchase_id`؛ ردیف Ledger همان منبع effect `ledger_sale` همین approval؛ `sale_amount = amount_snapshot = captured_amount > 0` |
| `wallet_capture` | `capture` (بخش ۱۲.۳) | `wallet_debit_events.state = 'captured'` (نه `held`، `released`، `reversed`)؛ `kind='sale'`؛ `ledger_entry_id` debit همان فروش؛ `wallet_account_id` debit همان account User فروش و همان `customer_identity`؛ `debit.amount = sale_amount = captured_amount`؛ عملیات `wallet_debit` همان `hold_ref` فروش |
| `admin_credit_spend` | مسیر کسر اعتبار reseller، فقط اگر تصمیم ۱۶ «بله» باشد | ردیف `admin_credit_spend` با `amount > 0` در همان تراکنش و همان عملیات فروش؛ `admin_id` آن همان مالک فروش؛ همان User و همان Package/Purchase فروش؛ `captured_amount` = مبلغ spend (هزینه‌ی همکاری)، `sale_amount` = مبلغ فروش به مشتری؛ این دو لزوماً برابر نیستند و هر دو ثبت می‌شوند |

همه‌ی مبلغ‌ها تومان صحیح‌اند. `sale_amount` همیشه مبلغ فروش به مشتری است و `captured_amount` مبلغ خود مدرک. برای کارت و کیف‌پول CHECK برابری‌شان را می‌خواهد؛ برای اعتبار reseller نه.

- هیچ endpointی مدرک را از caller نمی‌پذیرد. فروشی که با کلید عادی و `payment_method='card'` یا `'wallet'` و مبلغ مثبت ثبت شود، بدون approval معتبر یا capture واقعی، هیچ مدرکی و هیچ رویداد loyalty نمی‌گیرد.
- پرداخت بیرونی (integration شخص ثالث) نوع مدرک ندارد و واجد نیست؛ اگر Product بخواهد، یک `evidence_kind` جدا با capability اختصاصی لازم است (بخش ۲۳).
- چهار UNIQUE جدول تضمین می‌کنند یک فروش بیش از یک مدرک و یک مدرک بیش از یک فروش نداشته باشد؛ INSERT دوم در همان تراکنش فروش شکست می‌خورد و فروش دوم rollback می‌شود. دو تراکنش هم‌زمان با یک مدرک: یکی IntegrityError می‌گیرد.
- مدرک به approval یا debit اشاره می‌کند، نه به کلید API؛ revoke یا حذف کلید بعد از ساخت مدرک چیزی را عوض نمی‌کند.

```
is_loyalty_eligible_sale(sale_ledger_entry):
  evidence = sale_payment_evidence با sale_ledger_entry_id همان فروش
  واجد اگر و فقط اگر: evidence وجود دارد ؛ sale_ledger_entry.kind IN ('sale_new','sale_renew') ؛ voided_at IS NULL
  eligibility_reason: receipt_approval → 'card_paid' ؛ wallet_capture → 'wallet_captured' ؛ admin_credit_spend → 'admin_credit_paid'
```

| وضعیت | واجد؟ |
|---|---|
| فروش کارتی با approval معتبر، مبلغ > 0 | بله |
| فروش کیف‌پولی با debit `captured` | بله |
| debit فقط `held` یا `released` | خیر (فروشی ثبت نشده) |
| فروش پنل با کسر اعتبار reseller | تصمیم ۱۶؛ پیش‌فرض: خیر |
| ساخت/تمدید دستی رایگان؛ `reset_usage`؛ trial؛ قیمت صفر؛ تخفیف ۱۰۰٪؛ `paid_amount` صفر | خیر (مدرکی ساخته نمی‌شود: `amount > 0` شرط مدرک است) |
| مبلغ fallback یا `paid_amount` NULL | خیر (approval مبلغ دارد؛ فروش بدون approval مدرک ندارد) |
| فروش با `payment_method` ادعایی از payload بدون approval/capture | خیر |

`amount_source` روی Ledger فقط برای audit می‌ماند و در هیچ تصمیمی نیست.

**چرا P9a بعد از P9 است:** مدرک `wallet_capture` به `wallet_debit_events` نیاز دارد که فقط بعد از cutover کیف‌پول وجود دارد؛ امروز برداشت کیف‌پول یک `add_balance(-amount)` با commit مستقل است و capture durable ندارد. مدرک `receipt_approval` به approval `required` نیاز دارد، که خودش فقط همراه همین cutover روشن می‌شود (بخش ۵.۵). تا آن زمان loyalty با رفتار امروز می‌ماند، correlation فقط shadow است، و هیچ approvalی قابل ابطال نیست.

#### Epoch آستانه (policy آینده‌نگر)

- هر تغییر `loyalty_purchase_threshold` یا مقدار پاداش‌ها epoch باز را می‌بندد و یک epoch تازه می‌سازد. این تغییر فقط از `loyalty_service.change_policy` انجام می‌شود (نه از `setattr` generic تنظیمات)، زیر قفل انحصاری ردیف `receipt_approval_runtime_state`.
- پاداش‌های گذشته معتبر می‌مانند؛ تغییر آستانه هیچ پاداشی را بی‌اعتبار نمی‌کند و catch-up هم نمی‌سازد.
- پیشرفت هر User در epoch جدید از **anchor** خودش شروع می‌شود: `active_count` او در اولین خرید واجدش در آن epoch، قبل از شمردن همان خرید.
- slot شماره‌ی `s` در آن epoch: `required_active_count = anchor.starts_after_active_count + s * epoch.threshold`.
- فقط epoch باز پاداش می‌دهد. slot یک epoch بسته، حتی اگر پاداشش باطل شده باشد، دوباره اعطا نمی‌شود.

```
record_loyalty_purchase(user, sale_ledger_entry):
  (loyalty_timing_mode و epoch باز را با locking read از receipt_approval_runtime_state بخوان ؛ باید 'payment_event' باشد)
  اگر is_loyalty_eligible_sale نیست → پایان
  قفل ردیف User  (SELECT ... FOR UPDATE / BEGIN IMMEDIATE)
  N_before = active_count(user)
  INSERT loyalty_purchase_events(payment_evidence_id, ledger_entry_id, ...,
         completeness_generation = مقدار همان ردیف receipt_approval_runtime_state که بالا با locking read خوانده شد)
                                                                                    -- UNIQUE: retry همان فروش ردیف دوم نمی‌سازد
  N = N_before + 1
  E = epoch باز ؛ اگر E.threshold = 0 → cacheها ؛ پایان
  anchor = ردیف (user, E) ؛ نبود → INSERT با starts_after_active_count = N_before
  earned = (N - anchor.starts_after_active_count) // E.threshold
  برای s = 1 تا earned که پاداش فعالی برای (user, E, s) ندارد:
      اگر E.credit_amount = 0 و E.quota_bytes = 0 (یا حجم مقصد ندارد و اعتبار صفر است) → پاداشی ساخته نمی‌شود ؛ ادامه
      reearn_generation = 1 + تعداد پاداش‌های باطل‌شده‌ی همان (user, E, s)
      INSERT loyalty_reward_events(E, s, reearn_generation, required_active_count = anchor.starts_after + s * E.threshold,
                                   credit_amount = E.credit_amount, quota_bytes = E.quota_bytes,
                                   causation_type و ارجاع آن از مدرک همین خرید)
      اعمال اعتبار با یک wallet_credit_source جدا (source_key = 'loyalty:{lineage}:{E.id}:{s}:{reearn_generation}') و حجم (بخش ۱۴.۱)
  cacheها
```

**هر خرید حداکثر یک پاداش می‌سازد.** `N` با هر خرید دقیقاً یک واحد بالا می‌رود و آستانه حداقل ۱ است، پس `earned` حداکثر یک واحد زیاد می‌شود؛ slotهای قبلی که هنوز فعال‌اند رد می‌شوند. اگر slotی قبلاً با reconciliation باطل شده بود، `N` بعد از آن ابطال به زیر `required_active_count` آن رفته بود و فقط خریدی که `N` را دقیقاً به همان مقدار برمی‌گرداند آن را بازکسب می‌کند؛ در یک epoch هر مقدار `required_active_count` متعلق به یک slot است. پس در مجموع حداکثر یک slot در هر خرید.

**manifest.** چون cardinality حداکثر یک است، ثبت approval پیش‌بینی می‌کند: با `N_before` فعلی، آیا این خرید slotی را (تازه یا بازکسب) می‌سازد؟

| پیش‌بینی | ردیف‌های manifest |
|---|---|
| خرید واجد است | `loyalty_purchase_recorded:loyalty_purchase`، `required` |
| slot `s` از epoch `E` ساخته می‌شود و `E.credit_amount > 0` | `wallet_credit_source_created:credit:loyalty:{E.id}:{s}`، `required` |
| همان و `E.quota_bytes > 0` و مقصد یکتا و محدود (۱۴.۱) | `quota_reward_granted:quota:loyalty:{E.id}:{s}`، `required` |
| هیچ slotی ساخته نمی‌شود | هیچ ردیف پاداش |

پیش‌شرط اجرا (بخش ۵.۳) همین پیش‌بینی را زیر قفل ردیف User دوباره می‌سازد؛ اگر epoch باز عوض شده یا `N_before` تغییر کرده و نتیجه فرق دارد → approval `cancelled` با `loyalty_projection_changed`. پس برای خرید approvalدار کلید effectها همیشه قطعی و برای هر پاداش جداست. برای خرید بدون approval (کیف‌پول، اعتبار reseller) manifest وجود ندارد و همان الگوریتم بدون effect اجرا می‌شود. در هر دو حالت هر پاداش ردیف و source خودش را دارد و هیچ دو پاداشی تجمیع نمی‌شوند.

**مثال‌ها:**

| سناریو | anchor / slot | نتیجه |
|---|---|---|
| آستانه ۳؛ شش خرید | epoch ۱، anchor 0: R1 (`required=3`)، R2 (`required=6`) | دو پاداش |
| سپس آستانه → ۱۰ در `N=6` | epoch ۲؛ anchor در خرید بعدی = 6؛ slot ۱: `required=16` | پاداش بعدی دقیقاً در `N=16` (نه ۱۰ و نه ۳۰) |
| آستانه ۱۰، `N=7`، بدون پاداش؛ سپس آستانه → ۳ | epoch ۲؛ anchor = 7؛ slot ۱: `required=10` | یک پاداش در `N=10`؛ بدون catch-up و بدون پاداش تکراری |
| ۳ → ۱۰ در `N=6`؛ دو خرید؛ ۱۰ → ۵ در `N=8` | epoch ۳؛ anchor = 8؛ slot ۱: `required=13` | پاداش بعدی در `N=13` |
| epoch ۲ با anchor 6 و پاداش `required=16`؛ void یک خرید epoch ۱ | `N=15` | پاداش `required=16` باطل؛ R1 و R2 (۳ و ۶) معتبر |
| سپس یک خرید جایگزین | `N=16` | همان slot با `reearn_generation=2` بازکسب می‌شود؛ source و effect تازه |

#### Reconciliation (گام `loyalty_reconciliation` و refund فروش)

یک پاداش فعال معتبر است اگر و فقط اگر `required_active_count <= active_count`. هیچ ضرب دوباره‌ای انجام نمی‌شود.

```
loyalty_reconcile(user, operation, purchase_events_to_void):
  -- کل این تابع یک تراکنش است، زیر قفل ردیف User و قفل‌های identity همان operation (بخش ۱۲.۱)
  قفل ردیف User
  رویدادهای خرید داده‌شده → state = 'voided'
  N = active_count(user)
  برای هر پاداش با state = 'active' همین user به ترتیب (required_active_count DESC, id DESC):
      اگر required_active_count <= N → رد (پاداش معتبر می‌ماند)
      -- تا UPDATE نهایی پایین، هیچ ستونی از ردیف پاداش نوشته نمی‌شود
      void_reason = 'receipt_void' اگر causation_approval_uuid همان approval باطل‌شده است
                    'sale_refund'  اگر محرک refund یک فروش است
                    وگرنه 'loyalty_reconciliation'
      ۱. جزء اعتبار (اگر credit_amount > 0): برگشت source اختصاصی همین پاداش با الگوریتم بخش ۱۲.۶، در همین تراکنش
             → credit_state ، credit_reversed
      ۲. جزء حجم (اگر quota_bytes > 0): بخش ۱۴.۱ با factهای quota_before/quota_after همین ردیف ؛
             shortfall با loyalty_reward_event_id ثبت می‌شود
             → quota_state ، quota_reversed
      ۳. یک UPDATE اتمیک، همه‌ی فیلدها با هم:
             UPDATE loyalty_reward_events
                SET state = 'voided', void_reason = :reason, voided_at = DB_NOW, void_operation_id = :op,
                    credit_reversal_state = :credit_state, credit_reversed_amount = :credit_reversed,
                    quota_reversal_state  = :quota_state,  quota_reversed_bytes   = :quota_reversed
              WHERE id = :reward_id AND state = 'active'
             rowcount = 0 → این پاداش قبلاً reconcile شده ؛ ROLLBACK کل تراکنش، پس برگشت مالی/حجمی قدم‌های ۱ و ۲ هم ثبت نمی‌شود
      ۴. اگر causation_type = 'receipt_approval':
             effect اعتبار (credit:loyalty:{epoch}:{slot}) با mark_effect_reversed و state جزء اعتبار
             effect حجم   (quota:loyalty:{epoch}:{slot})  با mark_effect_reversed و state جزء حجم
         وگرنه:
             هیچ receipt_approval_effects جست‌وجو یا ساخته نمی‌شود
  cacheها
COMMIT
```

- محرک‌ها: ابطال رسید (رویدادهای با `approval_uuid = X`)، و `refund_debit` یک فروش کیف‌پولی `captured` (رویداد همان `ledger_entry_id`). هر دو دقیقاً یک‌بار reconcile می‌کنند.
- **ترتیب نوشتن:** ردیف پاداش فقط یک‌بار و در قدم ۳ نوشته می‌شود، بعد از این‌که نتیجه‌ی هر دو جزء معلوم است؛ پس CHECKهای جدول هرگز ردیف `voided` بدون state جزء نمی‌بینند. retry فقط پاداش‌های `active` را انتخاب می‌کند و پاداش برگشته را دوباره لمس نمی‌کند؛ اگر با وجود قفل، UPDATE قدم ۳ ردیفی پیدا نکند، کل تراکنش rollback می‌شود و هیچ برگشت مالی یا حجمی دوباره‌ای باقی نمی‌ماند.
- نتیجه فقط به `N` نهایی بستگی دارد، پس ابطال دو approval به هر ترتیب یک نتیجه می‌دهد.
- **دو جزء، دو نتیجه.** یک پاداش می‌تواند هم اعتبار و هم حجم داشته باشد و هرکدام سرنوشت خودش را دارد؛ مثلاً اعتبار خرج‌شده `reversed_with_debt` و حجم نیمه‌مصرف‌شده `partially_reversed`. جزء با مقدار صفر state و مقدار برگشتی NULL دارد. پاداش فعال هر چهار ستون NULL دارد. `quota_reversal_shortfalls` فقط به جزء حجم مربوط است. audit و preview دو نتیجه را جدا نشان می‌دهند.
- هر پاداش ردیف رویداد و source برگشت‌پذیر خودش را دارد؛ فقط پاداش‌های approvalدار علاوه بر آن `receipt_approval_effects` دارند. پس ابطال یکی از چند پاداش، lot (و effect، اگر هست) پاداش‌های سالم دیگر را لمس نمی‌کند.
- idempotency برگشت: شرط `state = 'active'` روی ردیف پاداش. retry refund یا retry ابطال پاداشی را که از قبل `voided` است دوباره برنمی‌گرداند، چه effect داشته باشد چه نه.
- پاداش متعلق به approval سالم دیگر: با `void_reason='loyalty_reconciliation'` و `void_operation_id` عملیات محرک ثبت می‌شود؛ approval سالم `completed` می‌ماند (بخش ۱۴.۳).
- اگر User در همان ابطال حذف می‌شود، رویدادها باطل می‌شوند و cache نوشته نمی‌شود؛ پاداش اعتباری همچنان با بخش ۱۲.۶ پس گرفته می‌شود.

#### Cutover

cutover loyalty جزئی از activation مشترک فاز P9a است (بخش ۵.۵) و جدا اجرا نمی‌شود. سهم loyalty در آن تراکنش:

```
preflight: هیچ User و هیچ Purchase با reserved_quota_bytes > 0 یا reserved_duration_days > 0 یا reserved_package_id IS NOT NULL
    اگر حتی یکی هست → 409 loyalty_cutover_blocked با فهرست دقیق (نوع، id، username)
INSERT loyalty_policy_epochs (epoch اول این generation از تنظیمات فعلی)
برای هر User با purchase_count > 0 یا loyalty_rewards_given > 0:
    INSERT loyalty_user_baselines(generation, P = purchase_count, G = loyalty_rewards_given)
    اگر threshold T > 0: INSERT loyalty_user_epoch_anchors(epoch اول, starts_after_active_count = P - (P mod T))
```

- رزرو امروز یک مقدار تجمیعی است و تعداد پرداخت‌های داخلش را نگه نمی‌دارد؛ رزرو ساخته‌شده قبل از cutover هنوز شمرده نشده و بعد از آن هم در activation شمرده نمی‌شود. هیچ backfill حدسی‌ای امن نیست؛ اپراتور رزروها را قبل از activation حل می‌کند.
- **anchor baseline:** پیشرفت داخل چرخه‌ی جاری حفظ می‌شود و پاداش بعدی در `P - (P mod T) + T` است. catch-up معوق رفتار قدیمی (وقتی `P // T > G`) ساخته نمی‌شود. مثال: `P=7`، `T=3`، `G=2` → anchor 6 → پاداش بعدی در `N=9`.
- تغییر آستانه بلافاصله بعد از activation: epoch اول بسته و epoch دوم ساخته می‌شود؛ anchor هر User در epoch دوم در اولین خرید واجدش ساخته می‌شود. آستانه‌ی صفر یعنی epoch بدون پاداش.
- بعد از activation، دو تابع activation رزرو در هر فراخوانی `loyalty_timing_mode` را می‌خوانند و در `payment_event` چیزی نمی‌شمارند. restart و deploy نسخه‌ی بعدی آن را عوض نمی‌کند.
- کدی که `loyalty_timing_mode` را می‌فهمد یک release زودتر منتشر می‌شود و در `legacy_activation` دقیقاً مثل امروز رفتار می‌کند؛ rollback کد به قبل از آن release بعد از activation پشتیبانی نمی‌شود.
- baselineها غیرقابل‌ابطال‌اند. اگر activation قبل از اولین approval برگردانده شود (بخش ۵.۵)، activation بعدی baseline و epoch نسل تازه می‌سازد و ردیف‌های نسل قبل فقط تاریخچه‌اند.

### ۱۴.۳ transition برگشت effectها

```
mark_effect_reversed(effect, reversal_state, reversed_value, operation):
  UPDATE receipt_approval_effects
     SET reversal_state = :s, reversed_value = :v, voided_at = now, void_operation_id = :op
   WHERE id = :effect AND voided_at IS NULL
  rowcount = 0 → effect قبلاً برگشته ؛ بدون تغییر ؛ مقدار ثبت‌شده‌ی قبلی برگردانده می‌شود
```

این تنها نویسنده‌ی این چهار ستون است و هر چهار را با هم می‌نویسد؛ هیچ pseudocode این سند `voided_at` را جدا نمی‌نویسد.

| `reversal_state` | معنا | `reversed_value` |
|---|---|---|
| `fully_reversed` | کل اثر واقعاً برگشته | مقدار کامل اثر |
| `reversed_with_debt` | بخش موجود پس گرفته شد؛ بخش خرج‌شده بدهی `wallet_debts` شد | مقدار پس‌گرفته‌شده (بدهی جدا در `wallet_debts`) |
| `reversed_with_writeoff` | بخش موجود پس گرفته شد؛ بخش خرج‌شده `wallet_writeoffs` شد | مقدار پس‌گرفته‌شده |
| `partially_reversed` | پاداش حجمی با shortfall | بایت برگشته |
| `unrecoverable` | اثر باطل علامت خورد ولی چیزی قابل پس‌گرفتن نبود (پاداش اعتباری قبل از cutover کیف‌پول) | 0 |
| `resource_deleted` | منبع در همین ابطال حذف شد | NULL |
| `not_applicable` | منبع پیش از ابطال از مسیر دیگری رفته بود، یا چیزی برای برگشت نبود | NULL |

| `effect_type` | transitionهای ممکن |
|---|---|
| `user_created`، `purchase_created`، `purchase_renewed`، `connection_created` | `resource_deleted`؛ `not_applicable` |
| `ledger_sale`، `ledger_topup` | `fully_reversed` (مقدار = مبلغ)؛ با `voided_at` روی Ledger و ردیف `receipt_void` |
| `ledger_credit_spend` | `fully_reversed` (مقدار = مبلغ refund) |
| `wallet_credit_source_created` | `fully_reversed` اگر هیچ مصرفی نبود؛ `reversed_with_debt`؛ `reversed_with_writeoff` (lot شخص ثالث)؛ `unrecoverable` |
| `quota_reward_granted` | `fully_reversed`؛ `partially_reversed`؛ `resource_deleted`؛ `not_applicable` |
| `loyalty_purchase_recorded` | `fully_reversed` (مقدار = 1) |
| `discount_redeemed` | `fully_reversed` (مقدار = `discount_amount`)؛ `not_applicable` |
| `card_payment_recorded` | `fully_reversed` (مقدار = مبلغ)؛ `not_applicable` (approval قبل از `event_logged`) |

- `fully_reversed` فقط یک معنا دارد: همه‌ی اثر برگشته. مصرف‌شده‌ای که بدهی یا write-off شد state خودش را دارد.
- این جدول فقط درباره‌ی `receipt_approval_effects` است. هر پاداش loyalty نتیجه‌ی دو جزء خودش را روی `loyalty_reward_events.credit_reversal_state` و `quota_reversal_state` دارد (بخش ۱۴.۲)؛ برای پاداش approvalدار، effect اعتبار state جزء اعتبار و effect حجم state جزء حجم را می‌گیرد. پاداش بدون approval effect ندارد.
- **effect متعلق به approval سالم دیگر** (پاداش بی‌پشتوانه در reconciliation): همان `mark_effect_reversed` با `void_operation_id` عملیات محرک. approval سالم `completed` می‌ماند؛ `finalize` و job تطبیق effect برگشته را خطا نمی‌دانند. endpoint audit هر دو approval (صاحب effect و محرک) این برگشت را نشان می‌دهد. چون شرط `voided_at IS NULL` است، نه retry همان عملیات و نه ابطال بعدی approval صاحب effect نمی‌تواند آن را دوباره برگرداند.

---

## ۱۵. PaymentCard: ابطال علّی پرداخت

**Policy ثابت:** ابطال یک پرداخت فقط مبلغ همان پرداخت و reset شمارنده‌ای را که **همان پرداخت** باعثش شده برمی‌دارد. کارت تاریخی پرداخت‌های دیگر، resetهای ناشی از پرداخت‌های سالم، pointer کارت فعال، ترتیب کارت‌ها، mode و threshold هیچ‌کدام تغییر نمی‌کنند.

### ۱۵.۱ جدول‌ها

```
payment_card_pool_states
pool_key        VARCHAR(32) NOT NULL PRIMARY KEY         -- 'global' یا 'admin:{id}'
logging_phase   VARCHAR(12) NOT NULL CHECK IN ('legacy','event_logged')
epoch           BIGINT      NOT NULL DEFAULT 0
updated_at      NOT NULL
version

payment_card_pool_baselines
pool_key              VARCHAR(32) NOT NULL PRIMARY KEY  FK payment_card_pool_states(pool_key) ON DELETE RESTRICT
accumulated_by_card   TEXT/JSON   NOT NULL        -- {card_id: accumulated_amount} در لحظه‌ی capture
captured_at           NOT NULL

payment_card_pool_events
id                           PK                                 -- ترتیب اعمال
pool_key                     VARCHAR(32) NOT NULL  FK payment_card_pool_states(pool_key) ON DELETE RESTRICT
event_kind                   VARCHAR(28) NOT NULL  CHECK IN ('payment_recorded','payment_recorded_uncorrelated')
card_id                      INT         NULL      FK payment_cards(id) ON DELETE SET NULL
card_id_snapshot             INT         NOT NULL               -- کارت واقعی این رویداد؛ immutable
card_label_snapshot          VARCHAR(32) NULL                   -- فقط ۴ رقم آخر + نام دارنده
approval_uuid                CHAR(36)    NULL
amount                       BIGINT      NOT NULL  CHECK (amount > 0)
accumulated_before           BIGINT      NOT NULL  CHECK (accumulated_before >= 0)   -- شمارنده‌ی زنده‌ی همین کارت درست قبل از این پرداخت
active_card_id_before        INT         NULL                   -- pointer فعال pool درست قبل از این پرداخت (NULL = بدون کارت فعال)
card_order_snapshot          TEXT/JSON   NOT NULL               -- [card_id,...] به ترتیب _pool_query در همان تراکنش
reset_after                  BOOLEAN     NOT NULL DEFAULT 0     -- آیا همین پرداخت در اجرای زنده شمارنده‌ی همین کارت را صفر کرد
rotated_to_card_id_snapshot  INT         NULL                   -- فقط audit: pointer بعد از این پرداخت به کدام کارت رفت
mode_snapshot                VARCHAR(16) NOT NULL               -- mode همان تراکنش
threshold_snapshot           BIGINT      NULL                   -- threshold همان تراکنش
voided_at                    NULL
void_operation_id            BIGINT      NULL
created_at                   NOT NULL

INDEX  (pool_key, card_id_snapshot, id)
UNIQUE (approval_uuid)                                    -- NULL-exempt عمداً؛ CHECK پایین تضمین می‌کند فقط payment_recorded مقدار دارد
CHECK  ((event_kind = 'payment_recorded' AND approval_uuid IS NOT NULL)
     OR (event_kind = 'payment_recorded_uncorrelated' AND approval_uuid IS NULL))
CHECK  (voided_at IS NULL OR event_kind = 'payment_recorded')
CHECK  (reset_after = 1 OR rotated_to_card_id_snapshot IS NULL)
CHECK  (reset_after = 0 OR (mode_snapshot = 'threshold' AND threshold_snapshot IS NOT NULL AND threshold_snapshot > 0
                            AND accumulated_before + amount >= threshold_snapshot
                            AND active_card_id_before IS NOT NULL AND active_card_id_before = card_id_snapshot))
```

- `payment_recorded` همیشه `approval_uuid`، کارت و مبلغ مثبت دارد. `payment_recorded_uncorrelated` هرگز `approval_uuid` ندارد، پس correlation تصادفی ممکن نیست.
- `payment_recorded_uncorrelated`: پرداختی که بدون approval ثبت‌شده می‌رسد (پیش از اجباری‌شدن ثبت approval). جمع می‌شود و هرگز قابل‌ابطال نیست.
- reset و rotation به‌صورت علّی روی **همان ردیف پرداختی** ثبت می‌شوند که باعثشان شده، نه به‌صورت رویداد جدا.
- `reset_after = 0` یعنی rotation هم نبوده (`rotated_to` باید NULL باشد). `reset_after = 1` با `rotated_to = NULL` مجاز است: در pool تک‌کارت، شمارنده صفر می‌شود ولی کارت بعدی وجود ندارد.
- `reset_after = 1` فقط با snapshot سازگار پذیرفته می‌شود، دقیقاً همان چهار شرط منطق زنده (۳.۳): mode `threshold`، threshold مقداردار، `accumulated_before + amount >= threshold_snapshot`، **و کارت همان active pointer قبل از پرداخت** (`active_card_id_before = card_id_snapshot`). پس reset برای کارت غیرفعال در DB هم رد می‌شود. snapshotها همان مقادیری‌اند که منطق زنده در همان تراکنش خواند.
- `rotated_to_card_id_snapshot` باید کارت بعدی واقعی باشد: اگر `card_order_snapshot` بیش از یک عضو دارد، عضو بعد از `card_id_snapshot` با wrap-around؛ اگر یک عضو دارد، NULL. این را validator سرویس قبل از INSERT می‌سنجد (CHECK نمی‌تواند داخل JSON را روی هر دو دیالکت بخواند)، و job تطبیق (بخش ۲۰.۵) همان قاعده را روی ردیف‌های ذخیره‌شده دوباره می‌سنجد تا دستکاری مستقیم DB هم دیده شود.
- هیچ رویدادی برای تنظیم دستی شمارنده وجود ندارد: `PaymentCardUpdate` (`schemas.py:1139-1144`) فیلد `accumulated_amount` نمی‌پذیرد و چنین قابلیتی در scope نیست. اگر روزی اضافه شود، endpoint، authorization، رمز، دلیل و رویداد خودش را می‌خواهد.
- تغییرهای config (افزودن/حذف/فعال‌سازی کارت، ترتیب، pointer دستی، mode، threshold) رویداد ندارند، چون ابطال هیچ‌کدام را بازسازی نمی‌کند.
- حذف فیزیکی کارت (`panel_settings.py:368,626`) چیزی را نمی‌شکند: FK `SET NULL` است و محاسبه فقط `card_id_snapshot` را می‌خواند. روی SQLite، `payment_card_service.remove_card` خودش `card_id` رویدادهای قبلی را NULL می‌کند.

### ۱۵.۲ نویسنده‌ها و اتمیک‌بودن baseline

همه‌ی نویسنده‌های ۳.۴ به `payment_card_service` منتقل می‌شوند: `advance_after_payment`، نُه endpoint نویسنده‌ی کارت در `routers/panel_settings.py` (۳۲۹-۳۹۰ و ۵۴۶-۶۴۱)، و سه فیلد کارت در `update_settings` (`panel_settings.py:219`) که از حلقه‌ی generic `setattr` بیرون کشیده می‌شوند. تست AST (RV-64) هر نوشتن دیگری را رد می‌کند، از جمله `setattr` generic روی `PanelSettings`/`AdminUser` با نام این فیلدها. هر تابع سرویس:

```
tx:
  قفل payment_card_pool:{pool_key}  (بخش ۸؛ revalidate)
  SELECT logging_phase, epoch FROM payment_card_pool_states WHERE pool_key = :k  FOR UPDATE / زیر BEGIN IMMEDIATE
  mutation زنده (همان منطق امروز)
  اگر logging_phase = 'event_logged' و این نوشتن شمارنده‌ی یک کارت را عوض می‌کند: INSERT payment_card_pool_events در همین تراکنش
COMMIT
```

برای پرداخت، سرویس در **همان تراکنش** و همان commit که منطق زنده‌ی `advance_after_payment` شمارنده و pointer را می‌نویسد، event را هم می‌نویسد: `accumulated_before`، `active_card_id_before`، `card_order_snapshot`، این‌که شمارنده‌ی همان کارت صفر شد (`reset_after`)، pointer به کجا رفت، و mode/threshold همان تصمیم. rollback هر دو را با هم برمی‌گرداند.

**گرفتن baseline** برای هر pool یک تراکنش است، زیر همان قفل pool:

```
tx:
  قفل pool ؛ SELECT ... FOR UPDATE روی payment_card_pool_states
  INSERT payment_card_pool_baselines(accumulated_amount زنده‌ی هر کارت در همین لحظه)
  UPDATE payment_card_pool_states SET logging_phase = 'event_logged', epoch = epoch + 1
COMMIT
```

چون هر نویسنده‌ی pool همان ردیف state را با locking read می‌خواند، هیچ پرداختی بین baseline و اولین event جا نمی‌افتد. این fence مخصوص pool است و از fence کیف‌پول (بخش ۹) مستقل.

pool یا ادمینی که بعد از rollout ساخته می‌شود: ردیف state با `event_logged` و baseline خالی در همان تراکنش ساخت اولین کارت آن ایجاد می‌شود. کارتی که در baseline نیست از صفر شروع می‌کند.

### ۱۵.۳ محاسبه‌ی شمارنده‌ی یک کارت

```
accumulator(pool, card):
  acc = baseline.accumulated_by_card.get(card, 0)
  برای هر event با pool_key = pool و card_id_snapshot = card به ترتیب id:
      payment_* با voided_at IS NOT NULL     → رد (مبلغ و reset آن هر دو)
      payment_* غیر-voided                   → acc += event.amount ؛ اگر event.reset_after: acc = 0
  return acc
```

- ورودی فقط baseline و ردیف‌های همان کارت است؛ قطعی است و به وضعیت هیچ کارت دیگری وابسته نیست.
- هیچ پرداختی دوباره به کارت دیگری نسبت داده نمی‌شود و هیچ rotation دوباره شبیه‌سازی نمی‌شود.
- pointer فعال، ترتیب، mode و threshold هرگز توسط این محاسبه نوشته نمی‌شوند.

### ۱۵.۴ شبیه‌سازی مرجع

pool با ترتیب `[A, B, C]`، `mode='threshold'`، `threshold=100`، baseline همه صفر، `active=A`.

| event | زنده | `reset_after` | وضعیت زنده بعد |
|---|---|---|---|
| e1 پرداخت A، 60 | جمع | 0 | A=60، active=A |
| e2 پرداخت A، 50 (**اسکمی**) | A=110 ≥ 100 → reset و rotation | 1 | A=0، active=B |
| e3 پرداخت B، 70 | جمع | 0 | B=70 |
| e4 پرداخت B، 40 | B=110 → reset و rotation | 1 | B=0، active=C |
| e5 پرداخت C، 30 | جمع | 0 | A=0، B=0، C=30، active=C |

ابطال e2 فقط کارت A را دوباره محاسبه می‌کند:

| کارت | محاسبه | نتیجه |
|---|---|---|
| A | e1: 60 ؛ e2 voided (مبلغ و reset آن حذف) | **60** |
| B | دست‌نخورده: e3: 70 ؛ e4: 110 → reset سالم می‌ماند | **0** |
| C | دست‌نخورده: e5: 30 | **30** |
| pointer فعال | نوشته نمی‌شود | **C** |

نتیجه‌ی نهایی: `A=60`، `B=0`، `C=30`، `active=C`. e4 سالم واقعاً از سقف گذشته و reset آن معتبر می‌ماند. مشتری بعدی همان کارت C را می‌بیند که پیش از ابطال می‌دید.

یک پیامد شناخته: اگر بعد از reset اسکمی همان کارت دوباره پرداخت سالم گرفته باشد، شمارنده‌ی بازمحاسبه‌شده می‌تواند از threshold بالاتر باشد بدون این‌که reset شده باشد. منطق زنده‌ی موجود (`>= threshold` در اولین پرداخت بعدی که آن کارت فعال است) آن را در همان پرداخت بعدی reset و rotate می‌کند؛ ابطال خودش rotation نمی‌سازد.

### ۱۵.۵ ابطال پرداخت کارت

```
tx (قفل pool؛ revalidate):
  event = payment_card_pool_events با approval_uuid = X
  نبود (approval قبل از event_logged، یا بدون کارت) → گام done با note؛ شمارنده دست نمی‌خورد
  event.voided_at = now ؛ event.void_operation_id = op
  اگر کارت event.card_id_snapshot هنوز وجود دارد:
      old = PaymentCard.accumulated_amount خوانده‌شده در همین تراکنش
      new = accumulator(pool, event.card_id_snapshot)
      UPDATE payment_cards SET accumulated_amount = :new WHERE id = :card AND accumulated_amount = :old
      rowcount = 0 → ROLLBACK و اجرای دوباره‌ی گام
COMMIT
```

`payment_cards` ستون `version` ندارد؛ CAS روی خود مقدار است و قفل pool (که هر پرداخت همان pool هم می‌گیرد) لایه‌ی اول. هیچ ردیف کارت دیگری و هیچ pointer نوشته نمی‌شود.

فقط شمارنده‌ی همان یک کارت نوشته می‌شود. `receipt_approvals.payment_card_id_snapshot`، `card_id_snapshot` هر event و `LedgerEntry.payment_card_id` هیچ approvalی تغییر نمی‌کند. کم‌کردن مستقیم مبلغ از شمارنده ممنوع است (بعد از یک reset، شمارنده‌ی فعلی متعلق به دور بعدی است).

---

## ۱۶. Business View / Audit View

`voided_at` روی Ledger فقط برای `sale_new`، `sale_renew`، `wallet_topup` نوشته می‌شود؛ ردیف `receipt_void` مبلغ صفر دارد و در هیچ `SALE_KINDS` نیست.

| مصرف‌کننده | محل | سیاست |
|---|---|---|
| `summary`، `series`، `subtree_rollup`، `by_admin`، `by_card` | `services/accounting.py` روی `scoped_query` | business: `voided_at IS NULL` داخل `scoped_query` |
| `list_transactions` | `routers/accounting.py:109` | پیش‌فرض business؛ `include_voided=true` → audit |
| `export_xlsx` | `routers/accounting.py:285` | پیش‌فرض business؛ گزینه‌ی audit جدا |
| داشبورد فروش/سود | از `scoped_query` | business |
| `get_sales_stats` بات | `routers/bot.py:563-595`؛ query مستقل، از `scoped_query` عبور نمی‌کند | باید جدا `voided_at IS NULL` بگیرد |
| receivables | `services/accounting.py` | بدون تغییر: فقط `admin_credit_change`/`admin_usage_charge`/`admin_payment` را می‌شمارد |
| هزینه/سود ادمین | `spend - refund` | بدون فیلتر جدید؛ refund واقعی آن را صفر می‌کند |
| موجودی کیف‌پول | `User.balance` (cache) | برابر `Σ available` lotهای غیر-voided |

**Audit view:** همه‌ی ردیف‌ها بدون فیلتر، با جفت اصلی + `receipt_void` (از `reversal_of_id`)، و از `wallet_operations`: actor، IP، زمان، دلیل، state.

سه مفهوم جدا نمایش داده می‌شوند: موجودی قابل‌خرج، تاریخچه‌ی audit، بدهی.

بعد از ابطال: فروش خالص و تعداد فروش مؤثر و سود همان approval صفر؛ topup مؤثر صفر؛ اعتبار reseller برگشته؛ export و داشبورد برابر (هر دو از `scoped_query`).

---

## ۱۷. Authorization

- منبع مالکیت: `receipt_approvals.owner_admin_id_snapshot` (immutable). برای هر preview/execute/status/retry/force/audit، این مقدار باید در `accounting.visible_admin_ids(db, current)` باشد؛ approval بدون مالک (`NULL`) فقط برای superadmin.
- lookup با `ledger_entry_id` اول به `approval_uuid` همان ردیف resolve می‌شود و سپس همان چک اجرا می‌شود. ردیف بدون `approval_uuid` → ۴۰۹ «قابل ردیابی نیست».
- ناموجود و غیرمجاز هر دو ۴۰۴ می‌گیرند تا وجود approval نشت نکند. UUID غیرقابل‌حدس جایگزین این چک نیست.
- فاز اول: همه‌ی endpointهای ابطال `require_superadmin`. execute و force علاوه بر آن `require_confirm_password`.
- manual-adjustment: superadmin، یا ادمینی که account در درخت اوست؛ همیشه با `require_confirm_password`.
- هر عملیات actor، IP، زمان و دلیل را در `wallet_operations` ثبت می‌کند.

---

## ۱۸. API Contract

| Endpoint | Auth | Body | پاسخ‌ها |
|---|---|---|---|
| `POST /api/accounting/receipt-approvals/auto` | X-API-Key + `RECEIPT_AUTO_APPROVAL_WRITE` | intent (kind، target، پکیج، مبلغ، کارت، کد تخفیف/دعوت؛ هویت‌ها فقط برای تطبیق) | 201 تازه با `{approval_uuid, execution_token, registration_seq}`؛ 200 تکراری؛ 409 `intent_changed` (intent متفاوت برای همان pending، حتی بعد از `cancelled`) یا approval موجود manual؛ 403 خارج از scope یا `auto_approval_policy_denied` (با reason code، از جمله `kind_not_auto_eligible` برای topup)؛ 422 `identity_mismatch`؛ **429 `auto_approval_rate_limited` با `retry_after_seconds`** |
| `POST /api/accounting/receipt-approvals/manual` | X-API-Key + `RECEIPT_MANUAL_APPROVAL_WRITE` | intent + `approved_by_telegram_id` | 201 تازه؛ 200 تکراری یا takeover (با `execution_token` جدید)؛ 409 `intent_changed` یا takeover بعد از شروع اجرا؛ 403 خارج از scope یا `manual_approver_not_authorized`؛ 422 `identity_mismatch` |
| هر production call با `approval_uuid` (`create_user`، `purchase_package`، `renew_service`، `renew`، `add_balance`، `apply_referral`، `redeem_discount`، `record_card_payment`) | auth موجود همان endpoint | بدنه‌ی موجود + `approval_uuid` + `execution_token` | 409 `approval_superseded`؛ 409 `manifest_precondition_failed`؛ 409 اختلاف هدف با snapshot approval |
| `POST /api/accounting/receipt-approvals/manifest-preview` | یکی از دو capability بالا | intent | 200 `{manifest, differs_from_cancelled}`؛ هیچ ردیفی ساخته نمی‌شود؛ 422 `identity_mismatch` |
| `PUT /api/accounting/receipt-approvals/runtime-mode` | superadmin + رمز | `{registration_mode?, auto_rate_limit_mode?}` | 200؛ 409 `use_joint_activation` برای `required`؛ 409 `ha_requires_external_fencing`؛ 409 ترتیب نامعتبر (`enforced` بدون `required`) |
| `POST /api/accounting/receipt-approvals/runtime-mode/activate` | superadmin + رمز | — | 200 `{completeness_generation}`؛ تکرار 200 بدون تغییر؛ 409 با کد پیش‌شرط شکست‌خورده (`wallet_not_enforced`، `key_identity_not_ready`، `ha_requires_external_fencing`، `loyalty_cutover_blocked`، `managed_bot_protocol_outdated`) و فهرست موارد |
| `POST /api/accounting/receipt-approvals/runtime-mode/deactivate` | superadmin + رمز | — | 200؛ 409 `activation_irreversible` |
| `GET /api/accounting/receipt-approvals/runtime-mode` | superadmin | — | 200 `{requested_registration_mode, effective_registration_mode, requested_auto_rate_limit_mode, effective_auto_rate_limit_mode, key_identity_ready, key_identity_failure, key_identity_failed_at}` |
| `POST /api/accounting/receipt-approvals/runtime-mode/recheck` | superadmin | — | 200 با همان بدنه بعد از محاسبه‌ی دوباره‌ی آمادگی |
| `POST /api/accounting/receipt-approvals/{uuid}/finalize` | یکی از دو capability بالا | — | 200 `{state, effect_count, missing_effects, unexpected_effects}` |
| `GET /api/accounting/auto-approval/status?owner_admin_id=&telegram_id=` | همان | — | 200 `{retry_after_seconds}`؛ فقط برای نمایش به ادمین |
| `POST /api/accounting/receipt-void/preview` | superadmin | `{approval_uuid}` یا `{ledger_entry_id}` | 200 preview + `preview_token`؛ 404؛ 409 غیرقابل‌ردیابی یا kind نامجاز |
| `POST /api/accounting/receipt-void/execute` | superadmin + رمز | `{preview_token, typed_username, reason, danger_ack}` | 200 `{operation_id, state}`؛ **تکرار یکسان: 200 با همان operation**؛ 409 preview منقضی/ناهم‌خوان یا payload متفاوت برای همان approval؛ 422 username/reason/ack نامعتبر؛ 403 رمز غلط |
| `GET /api/accounting/receipt-void/status/{operation_id}` | superadmin | — | 200 operation + گام‌ها؛ 404 |
| `POST /api/accounting/receipt-void/retry/{operation_id}` | superadmin | — | 200؛ 409 اگر state `cleanup_required` یا `failed` نیست |
| `POST /api/accounting/receipt-void/force-step/{operation_id}` | superadmin + رمز | `{step_key, reason, danger_ack}` | 200؛ 409 اگر گام قابل force نیست |
| `GET /api/accounting/receipt-void/audit/{approval_uuid}` | superadmin | — | 200؛ 404 |
| `POST /api/accounting/wallet/manual-adjustment` | ادمین مجاز + رمز | `{user_id, delta_amount, reason, idempotency_token}` | 200 `{balance, open_debt}`؛ تکرار یکسان 200؛ 409 payload متفاوت؛ 400 موجودی ناکافی؛ 422 |
| `POST /api/bot/users/{username}/add-balance` | X-API-Key + `WALLET_WRITE` | `{amount, source_kind, approval_uuid?, reason?, idempotency_token?}` | 422 اگر `source_kind` نباشد یا با فیلدها سازگار نباشد، برای هر علامت؛ 403 `topup_blocked` |
| `POST /api/accounting/wallet/holds/{hold_ref}/resolve` | superadmin + رمز | `{action: 'capture'|'release', ledger_entry_id?, reason}` | 200؛ 409 اگر hold دیگر `held` نیست |

refund endpoint HTTP ندارد؛ فقط `wallet_service.refund_debit` است که مسیر خرید آن را صدا می‌زند.

**Preview:** username و Telegram ID؛ kind و مبلغ؛ auto/manual و تأییدکننده؛ User/Purchase/Connectionهای دامنه با نام node (بدون هیچ credential)؛ مبلغ فروش قابل‌ابطال؛ ردیف‌های Ledger مرتبط؛ اعتبار reseller برگشتی؛ اثر discount/referral/loyalty؛ کارت و اثر شمارنده؛ هشدار approvalهای دیگر همان account. برای کیف‌پول: هر source متأثر با `gross`، applicationها، `projected_clawback`، `projected_debt`، `projected_settlement_reopen`، `projected_writeoff`، allocationهای lot (با برچسب «این فروش‌ها باطل نمی‌شوند»)، و `open_debt` فعلی identity.

**`preview_token`:** امضاشده؛ شامل `approval_uuid`، انقضا، و hash وضعیت (version approval، version sourceها و lotها، فهرست Connectionهای دامنه). execute همان hash را دوباره می‌سازد؛ اختلاف → ۴۰۹.

**نمایش User حذف‌شده:** همیشه از snapshotهای `receipt_approvals` و `wallet_accounts`؛ هیچ join زنده‌ای به `users` لازم نیست.

---

## ۱۹. UI Flow

- دکمه‌ی ابطال در تب تراکنش‌های حسابداری فقط روی ردیف `sale_new`/`sale_renew`/`wallet_topup` که `approval_uuid` دارد، و فقط برای superadmin.
- ردیف‌های expense، `admin_payment`، رویدادهای دستی و `link` هرگز دکمه ندارند.
- ردیف legacy بدون `approval_uuid`: دکمه‌ی غیرفعال با توضیح «قابل ردیابی نیست»؛ بدون هیچ حدس خودکار.
- وضعیت‌ها روی ردیف: `voiding`، `cleanup_required`، `voided`.
- جریان: preview کامل → تایپ دقیق username → دلیل اجباری → رمز superadmin → تأیید دوم با متن صریح خطر.
- بعد از execute: نمایش گام‌ها با polling از status؛ در `cleanup_required` نمایش node/Connection شکست‌خورده با دکمه‌ی retry و force جدا.
- دکمه‌ی اجرا بعد از اولین کلیک غیرفعال می‌شود.
- خطاهای ۴۰۳/۴۰۹/۴۲۲/۵۰۰ هرکدام پیام مشخص دارند؛ ۴۰۹ preview منقضی، preview را دوباره بار می‌کند.
- فرم ویرایش کاربر: فیلد «موجودی» فقط نمایشی است؛ تغییر با دکمه‌ی «تعدیل موجودی» (مبلغ ±، دلیل اجباری) و نمایش جدای «بدهی».
- فرم «قفل خرید»: دو گزینه‌ی جدا «خرید و تمدید» و «topup».
- تأیید خودکارِ محدودشده: رسید در صف بررسی دستی می‌ماند؛ مشتری پیام «درخواست برای بررسی دستی ثبت شد» می‌بیند؛ اعلان ادمین زمان باقی‌مانده تا تأیید خودکار بعدی را نشان می‌دهد (نمایشی؛ تصمیم با backend).
- فهرست holdهای `cleanup_required` با دو عمل capture دستی و release.
- desktop و mobile؛ ترجمه‌ی fa/en با کلیدهای flat در `translations.js`؛ focus صفحه‌کلید و `aria` برای dialogها.

---

## ۲۰. Migration، Cutover، HA

### ۲۰.۱ Migration schema

همه‌ی جدول‌های این سند جدیدند و با `create_all` ساخته می‌شوند. ستون‌های افزوده به `ledger_entries`، `discount_code_redemptions` و `api_keys` (`key_instance_uuid`، `approval_protocol_version`) همه nullable‌اند و با ابزار موجود اضافه می‌شوند. هر یکتایی روی جدول **موجود** با `Index(..., unique=True)` صریح تعریف می‌شود (`uq_api_keys_key_instance_uuid`، و index یکتای `ledger_entries.reversal_of_id`)، چون ابزار migration فقط `table.indexes` را می‌سازد و `UniqueConstraint` را روی جدول موجود نمی‌سازد (۳.۳). ساخته‌شدن واقعی هر index یکتا با inspector تأیید می‌شود، نه با نبود warning: برای کلید در بخش ۵.۵، و برای `reversal_of_id` به‌عنوان پیش‌شرط فاز P11. backfill `key_instance_uuid` idempotent است. جدول `users` هیچ ستون جدیدی نمی‌گیرد. `wallet_runtime_state` با یک ردیف `(1,'normal',0)` و `payment_card_pool_states` با `legacy` برای هر pool موجود seed می‌شوند.

### ۲۰.۲ Preflight

قبل از cutover، بدون هیچ نوشتن:

| وضعیت User | رفتار backfill |
|---|---|
| `balance > 0` | identity + account + یک lot `legacy_baseline` |
| `balance = 0` یا NULL | identity + account؛ بدون lot |
| `balance < 0` | **cutover شروع نمی‌شود**. فهرست این Userها گزارش می‌شود و تا تصمیم صریح برای هرکدام (بخش ۲۳) متوقف می‌ماند |

preflight همچنین گزارش می‌دهد: Userهای با `telegram_id` تکراری زیر یک tenant (مجاز است؛ فقط برای آگاهی).

### ۲۰.۳ Cutover

پیش‌شرط: گیت‌های بخش ۲۱ سبز؛ P6 فعال؛ `ha_enabled = False` (۲۰.۴)؛ preflight بدون مورد منفی.

```
tx واحد:
  UPDATE wallet_runtime_state SET phase='fencing', epoch = epoch + 1 WHERE id = 1 AND phase = 'normal'
      -- منتظر پایان همه‌ی writerهای در حال اجرا می‌ماند؛ writerهای بعدی تا COMMIT منتظرند
  برای هر User:
      find-or-create customer_identities
      INSERT wallet_accounts            -- UNIQUE(user_id) ؛ topup_blocked = purchases_blocked همان User
      اگر balance > 0: INSERT wallet_lots(source_kind='legacy_baseline', source_key='baseline:{lineage_key}',
                                         original = available = balance, state='active')
  بررسی reservation: هیچ عملیات provisioning غیرترمینال با reservation کیف‌پولی وجود ندارد ؛ وگرنه ROLLBACK
  reconciliation: برای هر User  Σ available = COALESCE(balance, 0) ؛ تعداد account = تعداد User
      اگر حتی یک اختلاف → ROLLBACK کل تراکنش (phase همان 'normal' می‌ماند)
  UPDATE wallet_runtime_state SET phase='enforced', epoch = epoch + 1 WHERE id = 1
COMMIT
```

- backfill، reconciliation و روشن‌شدن enforcement یک commit‌اند؛ هیچ حالت میانی ذخیره نمی‌شود و backfill نیمه‌کاره وجود ندارد.
- writerی که قبل از این تراکنش شروع شده، قفل مشترک state را دارد و این تراکنش منتظر او می‌ماند. writerی که بعد شروع می‌شود `enforced` را می‌بیند. عملیات چند-تراکنشی قدیمی با اختلاف `epoch` رد می‌شود (بخش ۹.۲).
- phase `fencing` برای دو حالت باقی می‌ماند: توقف دستی اضطراری همه‌ی mutationهای کیف‌پول، و وضعیت HA (۲۰.۴).

### ۲۰.۴ HA

HA این پروژه (`PanelSettings.ha_enabled`، `models.py:1749`) primary/standby با pull دوره‌ای snapshot یک‌طرفه است. standby دیتابیس جدا و عقب‌افتاده دارد و بعد از ۵ شکست پیاپی خودش را promote می‌کند. در split-brain، primary قدیمی هم خودش را primary می‌داند، پس هیچ وضعیت محلی («من primary هستم») نمی‌تواند fencing باشد.

**قاعده:**

| وضعیت | رفتار |
|---|---|
| `ha_enabled = False` | cutover و همه‌ی قابلیت‌های این سند مجاز |
| `ha_enabled = True` و `external_fencing = 'none'` | cutover رد می‌شود؛ phase `normal` می‌ماند؛ wallet-lot و ابطال رسید در دسترس نیستند |
| `phase = 'enforced'` و تلاش برای روشن‌کردن HA بدون external fencing | رد می‌شود (guard روی endpoint تنظیمات HA) |
| `external_fencing = 'coordinator'` | مجاز فقط با یک coordinator بیرونی که token یکتا و افزایشی می‌دهد و هر تراکنش writer آن را در کنار phase تأیید می‌کند. این coordinator امروز وجود ندارد و طراحی آن خارج از این سند است |

**ثبت approval و سقف خودکار روی HA.** همان استدلال برای correlation هم صادق است: approval یا `auto_granted_at` ثبت‌شده روی primary ممکن است هنوز در snapshot standby نباشد. بعد از failover روی snapshot عقب‌افتاده، همان subject می‌تواند زودتر از ۶۰ دقیقه grant دوم بگیرد و pending اخیر می‌تواند دوباره اجرا شود؛ در split-brain هر دو طرف می‌توانند برای یک pending approval جدا بسازند. UNIQUE و قفل ردیف محلی این را حل نمی‌کنند. قرنطینه‌ی موقت بعد از promotion هم کافی نیست، چون primary قدیمی از آن خبر ندارد.

| فاز | روی `ha_enabled = True` بدون coordinator |
|---|---|
| P0، P1، P2، P3 (shadow)، P5 | مجاز: هیچ‌کدام به correlation مرکزی یا سقف خودکار تکیه نمی‌کنند. shadow فقط log می‌کند و هیچ mutation را gate نمی‌کند |
| P9a (activation مشترک: `registration_mode = 'required'` + loyalty `payment_event`) | **رد**، ۴۰۹ `ha_requires_external_fencing` |
| P9b (`auto_rate_limit_mode = 'enforced'`) | **رد**، همان |
| روشن‌کردن HA وقتی P9a یا P9b فعال است | **رد**، همان (guard روی endpoint تنظیمات HA) |
| P6 تا P9 و P10، P11 | رد (جدول بالا) |

غیرفعال‌کردن تأیید خودکار و دستی‌کردن همه‌ی رسیدها هم مشکل را برنمی‌دارد: اجرای دستی یک pending بعد از failover عقب‌افتاده همچنان تضمین exactly-once ندارد. پس ثبت اجباری approval هم به fencing یا store مشترک durable نیاز دارد. coordinator بیرونی خارج از scope این سند است؛ نصب HA تا آن زمان روی رفتار امروز (به‌علاوه‌ی shadow) می‌ماند.

یعنی: snapshot HA بدون external fencing → mutation کیف‌پول wallet-lot روی هر دو طرف، در هر وضعیتی از جمله failover و split-brain، اجرا نمی‌شود، چون هرگز فعال نشده است. نصب‌های HA روی مسیر legacy (از طریق همان سرویس، phase `normal`) می‌مانند.

این سند split-brain را حل نمی‌کند. مسیر «تأیید اپراتوری قطع primary قدیمی + epoch جدید» هم تا وقتی طراحی جدا و قابل‌اثبات ندارد پشتیبانی نمی‌شود.

### ۲۰.۵ Cache و reconciliation

- `User.balance` در همان تراکنش هر تغییر lot به‌روز می‌شود: سرویس `Σ available` را بعد از تغییر محاسبه و می‌نویسد.
- قبل از هر mutation، سرویس `User.balance` را با `Σ available` فعلی مقایسه می‌کند. اختلاف → تراکنش rollback و خطای ۵۰۰ با کد `wallet_cache_mismatch`؛ هیچ تعمیر خودکاری در مسیر نوشتن نیست.
- job تطبیق علاوه بر موارد زیر، برای هر event پرداخت کارت قاعده‌ی `rotated_to` (بخش ۱۵.۱) و برای هر effect سه بررسی `finalize` (بخش ۵.۳) را می‌سنجد؛ وضعیت زنده‌ی منابع mutable را با snapshot مقایسه نمی‌کند.
- job دوره‌ای فقط‌خواندنی این‌ها را می‌سنجد و فقط alert می‌دهد: I-CACHE، I-SRC، I-LOT، I-NET، برابری `reversed_amount`/`adjusted_amount`/`settled_amount` با eventها، نبود User بدون account، برابری `manifest_hash` با ردیف‌های manifest، queryهای G-ORPHAN برای هر FK جدول ۴.۱، و debitهای `held` قدیمی‌تر از آستانه.
- تعمیر cache فقط با `wallet_cache_repair` است: `User.balance` را از lotها بازنویسی می‌کند و `wallet_cache_repair_events` می‌نویسد. هیچ source، lot یا Ledger نمی‌سازد.

---

## ۲۱. Rollout / Rollback / Drain

| فاز | محتوا | Flag | Rollback |
|---|---|---|---|
| P0 | جدول‌ها، ستون‌های nullable و indexهای یکتای صریح؛ seed؛ backfill `key_instance_uuid`؛ محاسبه‌ی آمادگی (۵.۵) | — | لازم نیست (additive) |
| P1 | `payment_card_service`: انتقال همه‌ی نویسنده‌های ۳.۴ از جمله سه فیلد `update_settings`؛ رفتار زنده بدون تغییر | — | برگرداندن کد |
| P2 | گرفتن baseline هر pool و `event_logged` (۱۵.۲) | per-pool phase | بازگشت به `legacy`؛ eventها می‌مانند و نادیده گرفته می‌شوند |
| P3 | ثبت `receipt_approvals`، manifest و effectها در mode `shadow` (بخش ۵.۶): شکست ثبت، approval را متوقف نمی‌کند؛ `record_effect`، binding کلید، evaluator مرکزی و سقف خودکار همه فقط log می‌شوند. همه‌ی زیرساخت ثبت اجباری در همین فاز deploy می‌شود ولی `required` روشن نمی‌شود | `registration_mode = 'shadow'` | `off` |

| P5 | انتقال اعمال پاداش حجمی به بعد از تثبیت topology و به مقصد manifest (۱۴.۱)؛ `wallet_service` با شاخه‌ی legacy: انتقال هر پنج نویسنده‌ی ۳.۱؛ `source_kind` اجباری برای callerهای داخلی؛ `wallet_policy` (دو predicate)؛ `create_user_with_wallet`؛ تفکیک `delete_user_cascade` و finalizerها؛ rebind | — | برگرداندن کد |
| P6 | **هنوز طراحی نشده.** state machine durable provisioning برای همه‌ی مسیرهای خرید، با reservation ساخت‌یافته و قرارداد ۱۲.۳؛ حذف برداشت و refund مستقل از `miniapp.py` و `customer_purchase.py`؛ کد hold/capture/release کیف‌پول آماده و با RV-116 و RV-117 سبز | متعلق به rollout طراحی provisioning | **مسدود** تا تأیید و پیاده‌سازی آن طراحی؛ همه‌ی فازهای بعد از آن هم مسدودند |
| P7 | حذف `UserUpdate.balance`؛ فرانت به manual-adjustment؛ `add_balance` بدون `source_kind` → ۴۲۲ برای همه | — | برگرداندن فیلد، هماهنگ با فرانت |
| P8 | **گیت:** تست‌های AST (RV-60 تا RV-65 و RV-100) سبز؛ RV-116 و RV-117 سبز؛ preflight بدون مورد منفی؛ `ha_enabled = False`؛ P6 در production فعال | — | — |
| P9 | Cutover (۲۰.۳) | `wallet_runtime_state.phase` | پایین |
| P9a | **Activation مشترک (بخش ۵.۵): `required` + loyalty `payment_event` در یک تراکنش.** شامل `sale_payment_evidence`، ledger و epoch loyalty، `loyalty_service.change_policy`، و enforcement کامل `record_effect`/`finalize`/binding کلید. پیش‌شرط‌ها: P9، `key_identity_ready`، `ha_enabled = False` یا coordinator، هیچ رزرو موجود، نسخه‌ی پروتکل همه‌ی کلیدهای managed، و بسته‌بودن تصمیم‌های ۱۵، ۱۶، ۱۸، ۱۹ | `POST runtime-mode/activate` | فقط قبل از اولین approval یا رویداد loyalty نسل جاری؛ بعد از آن ۴۰۹ |
| P9b | enforcement سقف خودکار: seed از ۶۰ دقیقه‌ی اخیر در همان تراکنش روشن‌شدن؛ سپس ۴۲۹. فقط بعد از P9a | `auto_rate_limit_mode = 'enforced'` | `shadow`: فقط ۴۲۹ متوقف می‌شود؛ grantها و eventها می‌مانند |
| P10 | UI preview؛ نیازمند P9a | `RECEIPT_VOID_PREVIEW` | خاموش |
| P11 | execute برای superadmin | `RECEIPT_VOID_EXECUTE` | پایین |

**گراف وابستگی:**

```
P0 ─┬─ P1 ── P2
    ├─ P3 (shadow)
    └─ P5 ── P7 ─┐
                 ├─ P8 (گیت) ── P9 ── P9a ─┬─ P9b
    P6 (مسدود) ──┘                         └─ P10 ── P11
```

- P9 فقط بعد از P5، **P6**، P7 و گیت P8. هیچ نویسنده‌ی مستقیمی در لحظه‌ی cutover وجود ندارد و مسیرهای خرید دیگر commitهای میانی بدون state durable ندارند.
- P9a فقط بعد از P9 و P3: هیچ approval `required` با loyalty legacy یا بدون capture durable کیف‌پول اجرا نمی‌شود.
- P9b، P10 و P11 فقط بعد از P9a. P11 فقط approval با `completeness_generation >= 1` را می‌پذیرد.
- **آماده‌ی پیاده‌سازی امروز:** P0، P1، P2، P3، P5. P6 هنوز طراحی نشده و schema واقعی reservation و operation آن باید در طراحی canonical lifecycle/provisioning تعریف و با قرارداد ۱۲.۳ تطبیق داده شود؛ همه‌ی فازهای بعد از آن پشتش می‌مانند.
- روی نصب HA بدون coordinator فقط P0 تا P3 و P5 اجرا می‌شوند (۲۰.۴).

**Rollback P9:** فقط تا وقتی هیچ mutation کیف‌پول بعد از cutover رخ نداده (هیچ source، debit، allocation، reversal یا بدهی جدیدی نیست)، با یک تراکنش `phase='normal', epoch+1`. بعد از اولین mutation، مسیر legacy و lot از هم جدا شده‌اند؛ بازگشت فقط با یک reverse-migration جدا ممکن است که این سند آن را طراحی نکرده و پشتیبانی نمی‌کند.

**Drain P11:** دو flag مستقل: `RECEIPT_VOID_EXECUTE` (پذیرش ابطال جدید) و `RECEIPT_VOID_WORKER` (ادامه‌ی عملیات در حال اجرا و cleanup). خاموش‌کردن اولی، دومی را متوقف نمی‌کند. خاموش‌کردن worker با operation در `running`/`cleanup_required` رد می‌شود.

**داده‌ی legacy:** فروش‌های قبل از P3 `approval_uuid` ندارند؛ approvalهای ثبت‌شده در shadow (`completeness_generation = 0`) هم کامل‌بودن effect ندارند. هر دو برای همیشه غیرقابل‌ابطال‌اند و backfill نمی‌شوند. lot `legacy_baseline` هم غیرقابل‌ابطال است.

**بات remote قدیمی:** نصبی که ثبت approval را نمی‌فرستد، بعد از P9a از endpointهای mutation خطای «approval_uuid الزامی» می‌گیرد. پس P9a فقط بعد از به‌روزرسانی همه‌ی نصب‌های remote روشن می‌شود.

**نصب تازه:** P0 تا P8 بدون داده‌ی legacy اجرا می‌شوند؛ cutover با صفر User همان تراکنش است.

---

## ۲۲. Test Matrix

همه بدون شبکه‌ی واقعی (guard سوکت fail-closed و client remote mock). هر تستی که «SQLite و MariaDB» دارد روی هر دو اجرا می‌شود (MariaDB در service container CI).

### هویت و account

| کد | سناریو | Invariant |
|---|---|---|
| RV-1 | دو User با یک `telegram_id` زیر یک tenant؛ topup و خرید جدا | دو account؛ موجودی‌ها مستقل؛ خرید یکی lot دیگری را مصرف نمی‌کند |
| RV-2 | همان دو User؛ یکی بدهی باز دارد؛ fraud hold روی یکی | خرید و تمدید روی **هر دو** account رد (`open_debt`)؛ topup روی هر دو **پذیرفته** و اول بدهی را تسویه می‌کند؛ هیچ‌کدام `topup_blocked` ندارند |
| RV-3 | ساخت User از هر شش محل ۳.۳ | هر User دقیقاً یک account |
| RV-4 | دو INSERT هم‌زمان account برای یک User؛ و backfill دوباره | یک account (`UNIQUE(user_id)`) |
| RV-5 | حذف User | account tombstone؛ CHECK برقرار؛ lotها باقی |
| RV-6 | حذف User، ساخت User جدید با همان Telegram (و در SQLite با همان `id`) | account جدید با موجودی صفر؛ account قدیمی وصل نمی‌شود؛ بدهی identity روی حساب جدید اعمال می‌شود |
| RV-7 | `link_telegram` / تغییر Telegram / `transfer_user` روی identity با بدهی باز | ۴۰۹ |
| RV-8 | همان، بدون بدهی | rebind ثبت می‌شود؛ account به identity جدید می‌رود |
| RV-9 | حذف ادمین مالک | `tenant_scope_key` و UNIQUE identity سالم |

### Correlation و reconciliation

| کد | سناریو | Invariant |
|---|---|---|
| RV-10 | ثبت دوباره‌ی approval، همان hash | همان `approval_uuid` |
| RV-11 | ثبت دوباره، hash متفاوت | ۴۰۹ |
| RV-12 | دو ثبت هم‌زمان | یک ردیف |
| RV-13 | retry نوشتن effect با همان `effect_key` | یک ردیف |
| RV-14 | کلید غیرمجاز یا credential در `resource_snapshot` | رد |
| RV-15 | `finalize` بدون effectهای لازم | state تغییر نمی‌کند؛ `missing_effects` پر |
| RV-16 | principal خارج از scope، `finalize` | ۴۰۳ |
| RV-17 | `perform_approval` واقعی برای new (User جدید)، new (User موجود)، renew با `renew_purchase_id`، renew legacy، topup؛ دستی و خودکار | همه‌ی effectها ثبت؛ `completed` |
| RV-18 | `link` | هیچ approval، source یا Ledger ساخته نمی‌شود |
| RV-19 | crash بعد از هر effect | approval `mutating`؛ بات `release_pending` نمی‌زند؛ recovery ساخته می‌شود |
| RV-20 | بات remote شبیه‌سازی‌شده | همان correlation؛ بدون خواندن sqlite بات |

### Credit، debit، FIFO

| کد | سناریو | Invariant |
|---|---|---|
| RV-21 | هر چهار `origin_kind` | source `applied`؛ I-SRC |
| RV-22 | `receipt_topup` بدون approval معتبر یا با مبلغ ناسازگار | ۴۲۲ |
| RV-23 | topup بزرگ‌تر، برابر و کوچک‌تر از بدهی | settlement تا سقف remaining؛ باقی‌مانده lot؛ برابر/کوچک‌تر بدون lot |
| RV-24 | referral: دو طرف | دو source جدا؛ قفل هر دو identity به ترتیب id |
| RV-25 | خرید از دو lot | دو allocation؛ جمع = مبلغ |
| RV-26 | دو lot با `created_at` برابر | ترتیب با `id` |
| RV-27 | debit `sale` در `captured` بدون LedgerEntry؛ یا `held` با LedgerEntry | رد با CHECK |
| RV-28 | شکست تابع خرید بعد از hold | `release`؛ lotها بایت‌به‌بایت مثل قبل از hold؛ debit `released` |
| RV-29 | خرید/تمدید/hold با `open_debt > 0`؛ سپس topup با همان بدهی | خرید ۴۰۹ `open_debt`؛ topup ۲۰۰ و بدهی کم می‌شود |
| RV-30 | manual adjustment مثبت/منفی از پنل و بات | actor و reason ثبت؛ تکرار token → همان نتیجه |
| RV-31 | `add_balance` بدون `source_kind`، مثبت و منفی | ۴۲۲ |
| RV-32 | دو debit هم‌زمان روی آخرین موجودی | یکی موفق؛ موجودی هرگز منفی |

### Refund و بدهی

| کد | سناریو | Invariant |
|---|---|---|
| RV-33 | refund روی lot `active` | available زیاد، consumed کم؛ CHECK برقرار |
| RV-34 | refund کامل روی lot `exhausted` (original=100) | available=100، consumed=0، state=`active` |
| RV-35 | refund قبل از void، سپس void | بدهی = consumed خالص |
| RV-36 تا RV-41 | شش مثال جدول ۱۲.۵، به ترتیب | اعداد دقیق همان جدول |
| RV-42 | بعد از هر یک از RV-33 تا RV-41 | I-NET، I-SRC، I-LOT |
| RV-43 | R2 با بدهی باز دیگر در همان identity | پول آزادشده اول بدهی دیگر را تسویه می‌کند؛ هرگز روی همان D نمی‌نشیند |
| RV-44 | void sourceی که lot `settlement_reallocation` دارد | آن lot هم claw-back می‌شود |
| RV-45 | refund روی lot voided بدون بدهی (write-off) | فقط reversal؛ بدون lot یا بدهی |
| RV-46 | هیچ عملیات داخلی source نمی‌سازد | تعداد source = تعداد رویداد خارجی |

### Void

| کد | سناریو | Invariant |
|---|---|---|
| RV-47 | void topup خرج‌نشده / نیمه / کامل | clawback و بدهی دقیق؛ User و سرویس‌ها باقی؛ fraud hold نوشته؛ `topup_blocked` دست‌نخورده؛ topup بعدی پذیرفته و بدهی را تسویه می‌کند؛ خرید تا برداشتن hold رد |
| RV-48 | topup کاملاً صرف بدهی، بدون lot | settlement write-off؛ بدهی قبلی دوباره باز |
| RV-49 | topup نیمی بدهی نیمی lot | هر دو شاخه |
| RV-50 | چند topup سالم + یکی اسکمی، FIFO | lot و Ledger سالم بایت‌به‌بایت برابر قبل |
| RV-51 | void approval با پاداش referral برای معرف در identity دیگر؛ هم‌زمان خرید معرف | قفل هر دو identity؛ نتیجه‌ی سریال؛ یک ردیف `wallet_writeoffs` برای معرف، بدون بدهی |
| RV-52 | `new` (User جدید) با چند Connection | ترتیب گام‌ها؛ account tombstone؛ فروش/سود/تعداد صفر |
| RV-53 | `new` (User موجود) | فقط Purchase و Connectionهای همان approval حذف؛ User مسدود و باقی |
| RV-54 | `renew` روی User با سرویس قدیمی سالم | کل account حذف؛ Ledger سالم قبلی دست‌نخورده؛ preview هشدار داده |
| RV-55 | شکست deprovision Connection دوم | `cleanup_required`؛ حساب مسدود؛ گام‌های ۵+ اجرا نشده؛ retry ادامه می‌دهد |
| RV-56 | force یک گام | `abandoned`؛ orphan ثبت؛ ادامه |
| RV-57 | restart بعد از هر گام | ادامه از اولین گام غیر-`done`؛ بدون اثر تکراری |
| RV-58 | دو void هم‌زمان با دو actor؛ و execute تکراری | یک operation؛ دومی ۲۰۰ با همان |
| RV-59 | admin credit، discount، شمارنده‌ی loyalty، حجم پاداش | هرکدام دقیقاً یک‌بار برگشته |

### گیت‌های AST

| کد | سناریو | Invariant |
|---|---|---|
| RV-60 | assignment یا `setattr` یا `update().values(balance=...)` روی `models.User` / جدول `users` بیرون از `wallet_service` | صفر. `AdminUser.balance` هدف این تست نیست و با یک مورد مثبت کنترل می‌شود |
| RV-61 | فراخوانی `models.User(` بیرون از `create_user_with_wallet` | صفر |
| RV-62 | `db.delete` روی User بیرون از `finalize_user_deletion_after_deprovision`؛ نبود tombstone در آن؛ ارجاع کد گام `db_delete` به `deprovision_connection` یا `delete_user_cascade` | صفر |
| RV-63 | نوشتن `User.telegram_id` / `User.owner_admin_id` بیرون از مسیر rebind | صفر |
| RV-64 | نوشتن `PaymentCard.accumulated_amount`، pointerها، mode/threshold و ساخت/حذف کارت بیرون از `payment_card_service`، شامل `setattr` generic روی `PanelSettings`/`AdminUser` با نام این فیلدها | صفر |
| RV-65 | self-check: fixture با یک تخلف از هر نوع | RV-60 تا RV-64 و RV-100 هرکدام تخلف خود را پیدا می‌کنند |

### قفل، phase، HA

| کد | سناریو | Invariant |
|---|---|---|
| RV-66 | executor قدیمی؛ رقیب acquire کرده ولی lot را لمس نکرده | rollback با اختلاف epoch |
| RV-67 | lease منقضی، هیچ‌کس acquire نکرده | rollback با `live = 0` |
| RV-68 | acquire رقیب حین تراکنش business | منتظر COMMIT می‌ماند |
| RV-69 | دو عملیات با مجموعه‌ی قفل متقاطع | بدون deadlock (ترتیب سراسری) |
| RV-70 | legacy writer در `enforced`؛ lot writer در `normal`؛ هر دو در `fencing` | رد |
| RV-71 | writer شروع‌شده قبل از cutover | cutover منتظر می‌ماند؛ نتیجه در backfill دیده می‌شود |
| RV-72 | عملیات چند-تراکنشی که cutover از وسطش رد شده | rollback با اختلاف epoch |
| RV-73 | MariaDB: خواندن phase بدون locking read | تست ثابت می‌کند snapshot قدیمی دیده می‌شود و سرویس از locking read استفاده می‌کند |
| RV-74 | cutover با اختلاف reconciliation | rollback کامل؛ هیچ account یا lot ذخیره نشده؛ phase `normal` |
| RV-75 | preflight با `balance < 0` | cutover شروع نمی‌شود |
| RV-76 | cutover با `ha_enabled = True` | رد |
| RV-77 | روشن‌کردن HA در `enforced` | رد |
| RV-78 | rollback P9 بعد از یک mutation | رد |

### PaymentCard

| کد | سناریو | Invariant |
|---|---|---|
| RV-79 | هر دو نوع event (`payment_recorded`، `payment_recorded_uncorrelated`) با و بدون reset | `accumulator()` هر کارت = `accumulated_amount` زنده |
| RV-80 | شبیه‌سازی مرجع ۱۵.۴ (e1 تا e5، ابطال e2) | دقیقاً A=60، B=0، C=30، active=C؛ فقط ردیف کارت A نوشته شده؛ `card_id_snapshot` و `reset_after` e3/e4/e5 بدون تغییر |
| RV-81 | ابطال پرداخت، با تغییرهای config (ترتیب، pointer دستی، mode، threshold) قبل و بعد از آن | pointer، ترتیب، mode و threshold دقیقاً برابر مقدار زنده‌ی پیش از ابطال |
| RV-82 | حذف فیزیکی کارت، سپس ابطال پرداختی روی همان کارت و روی کارت دیگر | بدون خطا؛ کارت حذف‌شده نوشته نمی‌شود؛ کارت دیگر درست محاسبه می‌شود |
| RV-83 | پرداخت هم‌زمان با گرفتن baseline | یا کامل در baseline، یا کامل به‌صورت event |
| RV-84 | pool و کارت ساخته‌شده بعد از rollout | state `event_logged` و baseline خالی در همان تراکنش؛ شمارنده از صفر |
| RV-85 | approval قبل از `event_logged` | گام `done` با note؛ شمارنده بدون تغییر |
| RV-86 | دو بار محاسبه‌ی پیاپی؛ و ابطال پرداختی که reset نساخته بود | نتیجه‌ی یکسان؛ فقط مبلغ کم می‌شود |

### حسابداری، دسترسی، cache، UI

| کد | سناریو | Invariant |
|---|---|---|
| RV-87 | summary/series/subtree/transactions/export/داشبورد/`get_sales_stats` بعد از void | فروش، تعداد، سود، topup مؤثر صفر؛ export = داشبورد |
| RV-88 | audit view | اصلی + `receipt_void` + actor/IP/دلیل |
| RV-89 | IDOR بین دو tenant با UUID و با `ledger_entry_id` | ۴۰۴ |
| RV-90 | admin/seller روی هر endpoint ابطال | ۴۰۳ |
| RV-91 | رمز غلط؛ username غلط؛ دلیل خالی | رد؛ operation ساخته نمی‌شود |
| RV-92 | preview منقضی | ۴۰۹ |
| RV-93 | approval با ادمین مالک حذف‌شده | authorization از snapshot |
| RV-94 | ردیف بدون `approval_uuid` | ۴۰۹؛ دکمه غیرفعال |
| RV-95 | cache mismatch عمدی | mutation با `wallet_cache_mismatch` رد |
| RV-96 | `wallet_cache_repair` | cache درست؛ هیچ source، lot یا Ledger جدید |
| RV-97 | job تطبیق با هر نوع drift عمدی | alert؛ بدون نوشتن |
| RV-98 | هیچ credential در preview، status، audit، log، error | جست‌وجوی مقادیر credential تست در همه‌ی خروجی‌ها: صفر |
| RV-99 | Frontend: دکمه فقط روی ردیف واجد شرایط؛ preview؛ تأیید دومرحله‌ای؛ progress؛ `cleanup_required`/retry/force؛ double-click؛ خطاها؛ desktop/mobile؛ fa/en؛ focus | مطابق بخش ۱۹ |

---

### Enforcement کلید خارجی

| کد | سناریو | Invariant |
|---|---|---|
| RV-100 | AST: هر `db.delete`، `query(M).delete` یا `delete(M.__table__)` روی مدل‌های جدید این سند یا `LedgerEntry` | صفر |
| RV-101 | SQLite: کاشتن یک فرزند بدون parent برای هر FK جدید | job G-ORPHAN همه را گزارش می‌کند |
| RV-102 | `delete_admin` وقتی approval، identity، account و event کارت با snapshot همان ادمین وجود دارد | موفق؛ هیچ ردیف جدیدی نمی‌شکند |
| RV-103 | `restore_from_upload` | job G-ORPHAN بعد از restore اجرا می‌شود |
| RV-104 | Purchase یا Connection دامنه قبل از void از مسیر دیگری حذف شده | effect با `reversal_state='not_applicable'`؛ void کامل می‌شود |

### Predicate خرید و topup

| کد | سناریو | Invariant |
|---|---|---|
| RV-105 | fraud hold، بدون بدهی: خرید، تمدید، ساخت User هم‌Telegram، topup؛ هرکدام از HTTP، `PanelBridge` و `RemoteBridge` | سه مورد اول ۴۰۳ `purchases_blocked`؛ topup پذیرفته؛ نتیجه در هر سه مسیر یکسان |
| RV-106 | `topup_blocked = True`، بدون قفل خرید | topup ۴۰۳ `topup_blocked`؛ خرید مجاز |
| RV-107 | cutover با Userهای `purchases_blocked` | `topup_blocked = True` برای همان‌ها؛ `False` برای بقیه |
| RV-108 | manual adjustment روی account با هر دو قفل | مجاز |

### Hold، capture، release

| کد | سناریو | Invariant |
|---|---|---|
| RV-109 | خرید موفق کیف‌پولی | debit `held` → `captured` در همان commit LedgerEntry فروش |
| RV-110 | rollback تراکنش ثبت فروش | debit `held` می‌ماند؛ LedgerEntry وجود ندارد؛ سپس `release` |
| RV-111 | crash بعد از hold | بعد از آستانه یک `wallet_hold_resolution` در `cleanup_required`؛ آزادسازی خودکار رخ نمی‌دهد |
| RV-112 | capture و release هم‌زمان روی یک hold | دقیقاً یکی اثر می‌کند |
| RV-113 | lot در حالی که debit `held` است void می‌شود؛ سپس release | مسیر R2؛ بدهی به اندازه‌ی hold کم می‌شود |
| RV-114 | ابطال با حذف User وقتی debit `held` دارد | گام `db_delete` متوقف؛ `cleanup_required` |
| RV-115 | resolve دستی hold: capture با LedgerEntry موجود؛ و release | هر دو audit‌شده؛ تکرار ۴۰۹ |
| RV-116 | قرارداد provisioning (۱۲.۳) روی state machine واقعی P6: remote ساخته شد، commit نهایی شکست خورد | حذف remote با همان هویت، سپس release؛ اگر حذف remote شکست بخورد hold `held` می‌ماند. **پیش‌شرط P9** |
| RV-117 | قرارداد provisioning: crash در `remote_calling` | recovery با خواندن هویت قطعی؛ بدون منبع دوم. **پیش‌شرط P9** |

### حذف DB-only و تأیید remote

| کد | سناریو | Invariant |
|---|---|---|
| RV-118 | void `new_user` با سه Connection | هر client mock دقیقاً یک فراخوانی حذف می‌گیرد (به‌علاوه‌ی فراخوانی تأیید طبق ۱۳.۴) |
| RV-119 | شکست تراکنش `db_delete`، سپس retry | هیچ فراخوانی remote جدید؛ گام‌های ۳ و ۴ `done` می‌مانند |
| RV-120 | `delete_user_cascade` از سه caller فعلی | deprovision + حذف + commit مثل امروز؛ account tombstone |
| RV-121 | client با خواندن | `verified_absent`؛ گام `done` |
| RV-122 | client بدون خواندن، حذف دوم «نیست» | `delete_idempotently_absent`؛ گام `done` |
| RV-123 | client بدون خواندن و بدون پاسخ قابل‌تشخیص | `unverified`؛ گام `blocked`؛ `cleanup_required`؛ ردیف‌های DB و User باقی |
| RV-124 | force روی گام RV-123 | `abandoned`؛ orphan و actor ثبت؛ گام ۵ اجرا می‌شود |
| RV-125 | تلاش اجرای گام ۵ با یک `verify` در `blocked` | رد |

### Manifest

| کد | سناریو | Invariant |
|---|---|---|
| RV-126 | ثبت هر پنج `target_shape` | ردیف‌های `required`/`optional` دقیقاً طبق ۵.۳ |
| RV-127 | `existing_user` با پکیج سه‌اتصالی؛ فقط دو اتصال ساخته شد | `finalize` کامل نمی‌کند؛ `missing_effects` اتصال سوم را نام می‌برد |
| RV-128 | نوشتن effectی که در manifest نیست | تراکنش mutation rollback |
| RV-129 | retry ثبت بعد از تغییر پکیج | همان manifest ذخیره‌شده |
| RV-130 | دستکاری یک ردیف manifest | job تطبیق اختلاف `manifest_hash` را گزارش می‌کند |
| RV-131 | ردیف‌های شرطی `ledger_credit_spend` و `card_payment_recorded` | فقط وقتی شرطشان برقرار است در manifest |

### Write-off و پاداش حجمی

| کد | سناریو | Invariant |
|---|---|---|
| RV-132 | void با lot معرفِ خرج‌شده؛ retry گام | یک ردیف `wallet_writeoffs`؛ بدون بدهی |
| RV-133 | I-NET با `result_snapshot` دستکاری‌شده | نتیجه فقط از `wallet_writeoffs`؛ بدون تغییر |
| RV-134 | پاداش حجمی مصرف‌نشده روی User با quota محدود | `fully_reversed`؛ quota = `quota_before`؛ بدون shortfall |
| RV-135 | quota سالم 10GB، پاداش 5GB، مصرف 12GB | reversed=3GB، shortfall=2GB، `partially_reversed`، total=used |
| RV-136 | بعد از RV-135 | enforcement موجود اتصال‌ها را می‌بندد؛ خرید بعدی مجاز |
| RV-137 | پاداش حجمی روی User در حال حذف | `resource_deleted`؛ بدون shortfall |
| RV-138 | effect پاداش حجمی که `quota` فعلی منبع با `quota_after` آن برابر نیست (مثلاً تمدید بعدی quota را بازنویسی کرده) | گام `blocked`؛ `cleanup_required`؛ بعد از force: `not_applicable` |

### PaymentCard (تکمیلی)

| کد | سناریو | Invariant |
|---|---|---|
| RV-139 | `update_settings` با سه فیلد کارت؛ و هر نُه endpoint کارت | از سرویس و زیر قفل pool؛ هیچ event ساخته نمی‌شود مگر شمارنده عوض شود |
| RV-140 | INSERT event ناقض هر CHECK: پرداخت correlated بدون approval؛ uncorrelated با approval؛ مبلغ صفر؛ `reset_after=0` با `rotated_to`؛ `reset_after=1` با mode غیر-threshold یا `accumulated_before + amount < threshold` | رد |
| RV-141 | `payment_recorded_uncorrelated` | جمع می‌شود؛ ابطال ندارد |

### سقف تأیید خودکار

| کد | سناریو | Invariant |
|---|---|---|
| RV-142 | اولین auto؛ دومی در دقیقه‌ی ۵۹؛ سومی دقیقاً در دقیقه‌ی ۶۰ | 201؛ 429 با `retry_after_seconds`؛ 201 |
| RV-143 | دو درخواست هم‌زمان یک subject | دقیقاً یکی 201 |
| RV-144 | retry همان pending | همان UUID؛ `last_granted_at` بدون تغییر |
| RV-145 | new سپس renew در یک ساعت؛ و topup از مسیر auto | new: 201؛ renew: 429؛ topup: 403 `auto_approval_policy_denied` با `kind_not_auto_eligible`، بدون ردیف approval، بدون تغییر `last_granted_at`، رسید در صف دستی |
| RV-146 | manual بعد از auto | پذیرفته |
| RV-147 | auto بعد از manual | پذیرفته و اولین grant |
| RV-148 | void یا شکست بعد از effect | سهمیه آزاد نمی‌شود |
| RV-149 | شکست قبل از هر mutation؛ سپس takeover دستی همان pending | سهمیه آزاد نمی‌شود؛ `auto_granted_at` می‌ماند؛ approval `manual` می‌شود |
| RV-150 | دو User با Telegram یکسان در یک tenant | سقف مشترک |
| RV-151 | همان Telegram در دو tenant | مستقل |
| RV-152 | بات مشترک، اختصاصی و remote | سقف مشترک مرکزی |
| RV-153 | کلید بدون `RECEIPT_MANUAL_APPROVAL_WRITE`؛ کلید با capability ولی approver نامجاز؛ فیلد `approval_mode` در بدنه | ۴۰۳؛ ۴۰۳ `manual_approver_not_authorized`؛ فیلد نادیده |
| RV-154 | هر سه `approver_evidence_kind` معتبر | پذیرفته و ثبت |
| RV-155 | restart بین دو درخواست؛ چند process هم‌زمان | سقف برقرار |
| RV-156 | ساعت process جلو/عقب | تصمیم فقط با `DB_NOW` |
| RV-157 | بدنه‌ی 429 | فقط `error` و `retry_after_seconds` |
| RV-158 | shadow | `shadow_would_limit` log؛ approval متوقف نمی‌شود |
| RV-159 | روشن‌شدن enforcement با grant ۳۰ دقیقه‌ی پیش | seed؛ درخواست بعدی 429 |
| RV-160 | خاموش‌کردن flag | 429 متوقف؛ subjectها و eventها باقی |
| RV-161 | MariaDB: دو subject متفاوت هم‌زمان | هیچ‌کدام منتظر دیگری نمی‌ماند |
| RV-162 | بات بعد از 429 | pending در صف دستی؛ پیام عمومی به مشتری؛ اعلان ادمین با زمان باقی‌مانده |

---

### هویت subject و evaluator مرکزی

| کد | سناریو | Invariant |
|---|---|---|
| RV-163 | `existing_user`/renew/topup با `telegram_id` متفاوت از `User.telegram_id` در payload | ۴۲۲ `identity_mismatch`؛ approval ساخته نمی‌شود |
| RV-164 | همان subject با `target_username` یا `owner_admin_id` دستکاری‌شده برای ساختن subject تازه | ۴۲۲ یا ۴۰۴؛ `tenant_scope_key` و Telegram همیشه از User |
| RV-165 | `new_user` از کلید غیر-managed | ۴۰۳ |
| RV-166 | `create_user` با `approval_uuid` ولی `telegram_id` یا `username` متفاوت از snapshot | ۴۰۹ قبل از هر mutation |
| RV-167 | User بدون Telegram، مسیر auto | ۴۰۳ `no_telegram_identity`؛ مسیر manual پذیرفته |
| RV-168 | evaluator مرکزی، «فقط مشتری قبلی»، همان مشتری از بات مشترک، اختصاصی و remote | نتیجه‌ی یکسان در هر سه؛ sqlite محلی بات خوانده نمی‌شود |
| RV-169 | مشتری قدیمی با فروش کارتی legacy در Ledger و بدون approval ثبت‌شده | `returning = True` |
| RV-170 | فروش کارتی legacy با `user_id = NULL` برای همان Telegram | `returning_unknown`؛ دستی |
| RV-171 | تنها approval قبلی subject `voided` است | `not_returning` |
| RV-172 | caller فیلد «مشتری قبلی» می‌فرستد | نادیده؛ نتیجه فقط از دیتابیس مرکزی |
| RV-173 | evaluator با ورودی ثابت، دو بار | خروجی یکسان؛ بدون I/O (تست واحد pure) |

### Manifest: referral و مشخصات منجمد

| کد | سناریو | Invariant |
|---|---|---|
| RV-174 | referral با هر چهار پاداش > 0 (اعتبار و حجم، هر دو طرف) | چهار ردیف `required` با کلیدهای ثابت؛ هر چهار effect پذیرفته؛ `completed` |
| RV-175 | تغییر مقدار پاداش در `PanelSettings` بین ثبت و اجرا | مقدار manifest اعمال می‌شود |
| RV-176 | حذف معرف، یا referred شدن مشتری، بین ثبت و اجرا | ۴۰۹ `manifest_precondition_failed`؛ approval `cancelled`؛ هیچ mutation |
| RV-177 | کد دعوت نامعتبر در لحظه‌ی ثبت | بدون ردیف referral؛ approval بدون referral کامل می‌شود |
| RV-178 | افزودن/حذف یک `PackageConnection` بین ثبت و اجرا | ۴۰۹؛ `cancelled`؛ صفر فراخوانی remote؛ صفر ردیف جدید |
| RV-179 | حذف یا غیرفعال‌شدن node؛ و تغییر protocol یا flow بین ثبت و اجرا | مثل RV-178 |
| RV-180 | تغییر پکیج **بعد از** قدم `mutating` | اجرا از مشخصات manifest؛ effectها با manifest می‌خوانند |
| RV-181 | ثبت دوباره‌ی pending بعد از `cancelled` | `registration_seq + 1`؛ manifest تازه؛ سهمیه‌ی auto قبلی آزاد نشده |

### Takeover و execution token

| کد | سناریو | Invariant |
|---|---|---|
| RV-182 | دو Session واقعی: takeover دستی و شروع اجرای خودکار هم‌زمان (SQLite و MariaDB) | دقیقاً یکی می‌برد؛ دیگری ۴۰۹ با state فعلی |
| RV-183 | production call با execution token قدیمی بعد از takeover | ۴۰۹ `approval_superseded`؛ هیچ mutation |
| RV-184 | takeover وقتی یک effect وجود دارد | ۴۰۹ |
| RV-185 | revoke کلید نصب managed؛ سپس اجرای approval `registered` همان کلید از endpoint واقعی production با dependency واقعی `get_bot_principal` | رد؛ audit فهرست approvalهای آن نصب را می‌دهد (جزئیات binding: RV-239 تا RV-243) |

### quota نامحدود

| کد | سناریو | Invariant |
|---|---|---|
| RV-186 | پاداش حجمی و اعتباری برای مقصد نامحدود (User با `total_quota_bytes = 0`؛ و User با تک Purchase `quota_bytes = 0`) | حجم اعمال نمی‌شود و quota صفر می‌ماند؛ اعتبار اعمال می‌شود؛ ردیف quota در manifest نیست |
| RV-187 | رفتار legacy حفظ‌شده: effect با `before_was_unlimited = True` روی **User**، سپس void | quota دقیقاً 0؛ `fully_reversed` |
| RV-188 | همان RV-187 روی **Purchase** (`quota_bytes`) | quota دقیقاً 0؛ `fully_reversed` |
| RV-189 | برگشت محدود روی Purchase با مصرف بیشتر از quota سالم | shortfall ثبت؛ `partially_reversed` |

### Cutover و provisioning

| کد | سناریو | Invariant |
|---|---|---|
| RV-190 | cutover وقتی یک عملیات provisioning غیرترمینال reservation کیف‌پولی دارد | rollback کامل cutover |
| RV-191 | تلاش cutover وقتی P6 فعال نیست یا RV-116/RV-117 سبز نیستند | گیت P8 رد می‌کند |
| RV-192 | خرید کیف‌پولی شروع‌شده قبل از cutover که reservation legacy آن released شده | مبلغ در `User.balance` و در baseline lot دیده می‌شود |

---

### تصمیم مرکزی، intent ثابت و ثبت دوباره

| کد | سناریو | Invariant |
|---|---|---|
| RV-193 | بات remote با sqlite محلی خالی؛ مشتری با سابقه‌ی مرکزی معتبر؛ «فقط مشتری قبلی» روشن | بات مسیر auto را صدا می‌زند؛ تأیید خودکار می‌شود |
| RV-194 | تصمیم محلی «نه»، backend «بله»؛ و برعکس | نتیجه همیشه پاسخ backend؛ اختلاف فقط در `local_decision` log می‌شود |
| RV-195 | ثبت دوباره بعد از `cancelled` با username، مبلغ، رسید، کارت، kind یا پکیج متفاوت (هرکدام جدا) | ۴۰۹ `intent_changed`؛ ردیف جدید ساخته نمی‌شود |
| RV-196 | ثبت دوباره بعد از `cancelled` با همان intent | `registration_seq + 1`؛ `supersedes_approval_uuid` = ردیف قبلی؛ `immutable_intent_hash` برابر؛ `manifest_hash` تازه |
| RV-197 | دو ثبت دوباره‌ی هم‌زمان (SQLite و MariaDB) | یک ردیف با seq جدید؛ دومی همان را می‌گیرد |
| RV-198 | auto → `manifest_precondition_failed` → رفتار بات | pending در صف دستی؛ هیچ فراخوانی auto دوم؛ اگر دستی صدا زده شود ۴۲۹ |
| RV-199 | ثبت دوباره‌ی دستی بعد از `cancelled` | `manifest-preview` تفاوت را نشان می‌دهد؛ ثبت پذیرفته؛ `auto_granted_at` ردیف قبلی دست‌نخورده |
| RV-200 | ثبت دوباره‌ی خودکار همان pending بعد از ۶۰ دقیقه | پذیرفته؛ grant جدید |
| RV-201 | approval با `registered_by_key_instance_uuid` و `pending_source_instance_id`؛ `new_user` | هر دو ثبت؛ `telegram_id_source = 'bot_attested'` |

### پیش‌شرط target

| کد | سناریو | Invariant |
|---|---|---|
| RV-202 | `new_user`: username بین ثبت و اجرا گرفته شد | `cancelled` با `username_taken`؛ هیچ mutation |
| RV-203 | `existing_user`: تغییر username، Telegram یا مالک target بین ثبت و اجرا (هرکدام جدا) | `cancelled` با `target_user_changed` |
| RV-204 | `renew_purchase`: Purchase حذف شده؛ متعلق به User دیگر؛ پکیج دیگر | `cancelled` با `target_purchase_changed` |
| RV-205 | `renew_user`: User بین ثبت و اجرا Purchase گرفت | `cancelled` با `target_shape_changed` |
| RV-206 | `topup`: User حذف شده یا account tombstone | `cancelled` با `target_user_changed` |
| RV-207 | انتقال target به tenant دیگر | `cancelled` با `target_tenant_changed` |
| RV-208 | رقابت بعد از پیش‌شرط (username هم‌زمان گرفته می‌شود) روی state machine P6 | شکست قبل از هر remote یا compensation بدون orphan. **وابسته به P6** |

### Schema manifest و مقصد quota

| کد | سناریو | Invariant |
|---|---|---|
| RV-209 | `expected` با کلید اضافه؛ کلید کم؛ نوع اشتباه (برای هر `effect_type`) | رد |
| RV-210 | همان `expected` با ترتیب کلید متفاوت | `manifest_hash` یکسان |
| RV-211 | کاربر جدید با پکیج محدود که Purchase می‌سازد؛ پاداش حجمی referral | effect روی همان Purchase؛ برگشت همان `resource_id`؛ `User.total_quota_bytes` دست‌نخورده |
| RV-212 | معرف با دو Purchase | پاداش حجمی اعمال نمی‌شود و در manifest نیست؛ پاداش اعتباری اعمال می‌شود |
| RV-213 | معرف با تک Purchase در ثبت، دو Purchase در اجرا | `cancelled`؛ هیچ mutation |
| RV-214 | «مشتری قبلی»: فروش با `payment_method = NULL` و بدون `payment_card_id`؛ و همان با `payment_card_id` | اولی `returning_unknown`؛ دومی `returning` |
| RV-215 | گزارش shadow | تعداد «محلی returning، مرکزی unknown/not_returning» و برعکس |

### رویداد علّی کارت

| کد | سناریو | Invariant |
|---|---|---|
| RV-216 | pool تک‌کارت؛ پرداخت از threshold می‌گذرد | `reset_after = 1`، `rotated_to = NULL`؛ CHECK می‌پذیرد؛ ابطال آن شمارنده را به مجموع قبل برمی‌گرداند |
| RV-217 | شکست نوشتن event بعد از mutation زنده، در همان تراکنش | هر دو rollback؛ شمارنده و pointer بدون تغییر |
| RV-218 | پرداخت هم‌زمان روی همان کارت حین گام `card_reversal` | قفل pool سریال می‌کند؛ اگر مقدار عوض شده CAS رد و گام تکرار می‌شود؛ فقط همان ردیف کارت نوشته می‌شود |

---

### Retry approval `failed`

| کد | سناریو | Invariant |
|---|---|---|
| RV-219 | auto `failed` بدون effect → retry auto | همان `approval_uuid`؛ `state='registered'`؛ `version + 1`؛ token تازه؛ اجرای بعدی موفق |
| RV-220 | بعد از RV-219 | `auto_granted_at`، `auto_approval_subjects` و تعداد event `granted` بدون تغییر؛ retry در دقیقه‌ی ۱۰ هم ۴۲۹ نمی‌گیرد |
| RV-221 | manual `failed` → retry manual، با همان approver و با approver مجاز دیگر | پذیرفته؛ فیلدهای approver به‌روز؛ اجرای موفق |
| RV-222 | auto `failed` → درخواست manual | takeover؛ `approval_mode='manual'`؛ `original_approval_mode='auto'` بدون تغییر؛ ردیف `receipt_approval_authority_events` |
| RV-223 | ردیف `failed` که effect دارد (fixture دستکاری‌شده) → retry و takeover | هر دو ۴۰۹ `approval_has_effects`؛ ردیف بدون تغییر |
| RV-224 | token قبل از retry، بعد از retry | ۴۰۹ `approval_superseded` |
| RV-225 | manual `failed` → درخواست auto | ۴۰۹ |

### Manifest: Purchase، binding و مقادیر

| کد | سناریو | Invariant |
|---|---|---|
| RV-226 | `new_user` با اتصال | manifest ردیف `purchase_created` با `user: {"ref": "effect:user_created:user"}` دارد؛ effect به `user_id` واقعی bind می‌شود؛ ابطال Purchase را از همین effect پیدا می‌کند |
| RV-227 | `new_user` بدون اتصال | ردیف `purchase_created` در manifest نیست؛ تلاش ثبت effect Purchase رد می‌شود |
| RV-228 | نوشتن `purchase_created` قبل از `user_created` | `effect_manifest_mismatch` (ref resolve نمی‌شود) |
| RV-229 | `ledger_sale` با مبلغ اشتباه؛ و با کارت اشتباه؛ کلید `sale` درست | `effect_manifest_mismatch`؛ تراکنش mutation rollback؛ هیچ LedgerEntry باقی نمی‌ماند |
| RV-230 | `purchase_created` برای User یا پکیج اشتباه | همان |
| RV-231 | `connection_created` با protocol یا flow ناسازگار ولی کلید معتبر | همان |
| RV-232 | پاداش حجمی روی منبعی غیر از binding manifest | همان |
| RV-233 | effect `optional` (تخفیف؛ loyalty) با مقدار ناهم‌خوان | همان |
| RV-234 | هیچ مسیر API برای فرستادن projection یا snapshot | `record_effect` فقط شیء منبع می‌گیرد؛ فیلد اضافه در بدنه‌ی درخواست نادیده و بی‌اثر |
| RV-235 | دستکاری `actual_projection` ذخیره‌شده؛ و دستکاری فیلد immutable یک منبع گروه ۱ بعد از نوشتن effect | `finalize` کامل نمی‌کند؛ `projection_mismatch`؛ `approval_recovery`. (تغییر معتبر منبع mutable: RV-267) |
| RV-236 | مقدار binding ساختگی (صفر، `null`، شکل نامعتبر) در manifest | validator رد می‌کند |

### مقصد loyalty quota

| کد | سناریو | Invariant |
|---|---|---|
| RV-237 | loyalty حجمی `new_user` با اتصال و quota محدود | بعد از ساخت Purchase اعمال می‌شود؛ effect روی همان Purchase با `quota_before/after`؛ ابطال همان `resource_id` را برمی‌گرداند |
| RV-238 | loyalty حجمی `existing_user` که بعد از خرید دو Purchase دارد | ردیف quota در manifest نیست؛ حجم اعمال نمی‌شود؛ اعتبار اعمال می‌شود |
| RV-244 | loyalty حجمی `renew_purchase` با تک Purchase محدود؛ و `renew_user` legacy | مقصد منجمد همان Purchase؛ و User |
| RV-245 | مقصد loyalty نامحدود | skip؛ اعتبار اعمال می‌شود |
| RV-246 | مقصد `{"type","id"}` loyalty بین ثبت و اجرا عوض شد | `cancelled`؛ هیچ mutation |

### Binding کلید اجرا

| کد | سناریو | Invariant |
|---|---|---|
| RV-239 | approval ثبت‌شده با کلید K؛ K غیرفعال یا حذف شد؛ اجرای پیش‌شرط، `record_effect` و `finalize` با کلید معتبر دیگری در همان scope، از endpoint واقعی و `get_bot_principal` واقعی | هر سه ۴۰۳ `execution_key_revoked` |
| RV-240 | K هنوز enabled؛ کلید معتبر دیگری تلاش اجرا می‌کند | ۴۰۳ `execution_principal_mismatch` |
| RV-241 | بعد از revoke K: takeover دستی توسط نصب managed معتبر دیگر با approver مجاز | `execution_key_instance_uuid` منتقل؛ event با `reason='revoked_key_takeover'`؛ اجرا موفق |
| RV-242 | approval in-process (`execution_key_instance_uuid = NULL`) | principal in-process اجرا می‌کند؛ هر principal با کلید ۴۰۳ |
| RV-243 | revoke K وسط اجرا (`mutating`) | approval در `mutating` می‌ماند؛ takeover ۴۰۹؛ فقط `approval_recovery` |

### Active pointer در رویداد کارت

| کد | سناریو | Invariant |
|---|---|---|
| RV-247 | INSERT مستقیم DB: `reset_after=1` با `active_card_id_before` متفاوت از کارت، یا NULL | CHECK رد می‌کند |
| RV-248 | `rotated_to` که عضو بعدی `card_order_snapshot` نیست؛ و `rotated_to` غیر-NULL در pool تک‌کارت | validator سرویس رد می‌کند؛ ردیف کاشته‌شده‌ی مستقیم را job تطبیق گزارش می‌کند |
| RV-249 | پرداخت زنده روی کارت غیرفعال که از threshold می‌گذرد | `reset_after=0`؛ شمارنده جمع می‌شود؛ pointer بدون تغییر |
| RV-250 | pool سه‌کارته، پرداخت روی آخرین کارت فعال از threshold می‌گذرد | `rotated_to` = اولین کارت (wrap-around) |

---

### هویت instance کلید و takeover

| کد | سناریو | Invariant |
|---|---|---|
| RV-251 | SQLite: approval با کلید K؛ حذف K (بالاترین id)؛ ساخت کلید جدید که همان id عددی را می‌گیرد؛ اجرای approval قبلی با کلید جدید | ۴۰۳ `execution_key_revoked`؛ id عددی برابر است ولی `key_instance_uuid` نه |
| RV-252 | همان سناریو روی MariaDB (id تکرار نمی‌شود) و با درج دستی id تکراری | همان نتیجه |
| RV-253 | rotation مقدار کلید روی همان ردیف؛ و حذف و ساخت دوباره با همان label | approval قبلی اجرا نمی‌شود |
| RV-254 | backfill `key_instance_uuid` دو بار؛ کلید بدون UUID | بار دوم بدون تغییر؛ کلید بدون UUID ۴۰۳ در ثبت و اجرا |
| RV-255 | approval دستی `registered`؛ کلید اجرا revoke شد | takeover توسط principal معتبر پذیرفته؛ اجرا موفق |
| RV-256 | approval دستی `failed`؛ کلید اجرا حذف شد | همان |
| RV-257 | in-process → managed؛ و managed → in-process | هر دو پذیرفته با event |
| RV-258 | takeover روی approval `mutating`؛ و روی approval دارای effect | ۴۰۹؛ بدون تغییر |
| RV-259 | token قبل از takeover، بعد از آن | ۴۰۹ `approval_superseded` |
| RV-260 | takeover با: کلید بدون capability؛ tenant دیگر؛ approver نامجاز؛ کلید غیرفعال؛ کلید غیر-managed (هرکدام جدا) | ۴۰۳؛ بدون تغییر و بدون event |

### Evidence تایپ‌شده و finalize

| کد | سناریو | Invariant |
|---|---|---|
| RV-261 | پاداش حجمی: `quota_before`/`quota_after` واقعی در evidence | projection از evidence؛ `resource_snapshot` همان مقادیر؛ ابطال از همان‌ها می‌خواند |
| RV-262 | تمدیدی که کاملاً رزرو می‌شود (ردیف نهایی quota را عوض نمی‌کند) | `add_quota_bytes` و `add_days` از evidence درست؛ بدون حدس از ردیف نهایی |
| RV-263 | شمارنده‌ی loyalty با before/after واقعی؛ و loyalty با `rewards_count = 2` | projection و `amount_per_reward` دقیق |
| RV-264 | اعتبار قبل از cutover با `balance_before`/`balance_after` در همان تراکنش | مبلغ = delta دقیق |
| RV-265 | AST: ساخت هر کلاس evidence بیرون از ماژول سرویس مالک؛ فیلد evidence یا projection در بدنه‌ی هر endpoint | صفر؛ فیلد اضافه بی‌اثر |
| RV-266 | evidence ناسازگار با منبع؛ و projection ناهم‌خوان با `expected` | `effect_evidence_invalid` / `effect_manifest_mismatch`؛ mutation همان تراکنش rollback |
| RV-267 | بین effect و `finalize`: مصرف quota؛ تمدید Purchase؛ rename و transfer User؛ تغییر موجودی و شمارنده‌ی loyalty (هرکدام جدا) | `finalize` → `completed` |
| RV-268 | دستکاری `actual_projection` ذخیره‌شده؛ و دستکاری یک ردیف manifest | اولی: `projection_mismatch`؛ دومی: اختلاف `manifest_hash`؛ هیچ‌کدام `completed` نمی‌شود |
| RV-269 | دستکاری مبلغ `LedgerEntry` (گروه ۱) بعد از effect | `finalize` و job تطبیق هر دو می‌بینند |
| RV-270 | job تطبیق بعد از RV-267 | هیچ alert |

### Slot اتصال

| کد | سناریو | Invariant |
|---|---|---|
| RV-271 | پکیج با دو `PackageConnection` کاملاً یکسان؛ ترتیب relationship معکوس | هر effect با `package_connection_id` خودش؛ `completed` در هر دو ترتیب |
| RV-272 | شکست slot اول، موفقیت slot دوم | effect slot دوم با کلید خودش؛ `missing_effects` فقط slot اول |
| RV-273 | بدنه با دو spec کاملاً یکسان | دو effect `conn:request_slot:0` و `conn:request_slot:1` |
| RV-274 | تغییر ترتیب لیست اتصال‌های بدنه بین ثبت و retry | `immutable_intent_hash` متفاوت → ۴۰۹ `intent_changed` |

### تاریخچه‌ی authority

| کد | سناریو | Invariant |
|---|---|---|
| RV-275 | manual `failed` → retry با approver دیگر | `original_approved_by_telegram_id` بدون تغییر؛ `approved_by_telegram_id` جدید؛ event `approver_change` با from/to approver، evidence kind و version |
| RV-276 | زنجیره: ثبت auto → takeover → retry با approver دوم → takeover با کلید دیگر | `original_*` به‌علاوه‌ی سه event، به ترتیب `version_after`، کل تاریخچه را بازمی‌سازند |
| RV-277 | AST و DB: UPDATE یا DELETE روی `receipt_approval_authority_events`؛ UPDATE روی ستون‌های `original_*` | صفر مسیر در کد |

---

### آمادگی هویت کلید و migration

| کد | سناریو | Invariant |
|---|---|---|
| RV-278 | دیتابیس قدیمی بدون ستون `key_instance_uuid` → migration واقعی | inspector: index `uq_api_keys_key_instance_uuid` وجود دارد و unique است؛ `key_identity_ready = 1` |
| RV-279 | INSERT دو کلید با UUID یکسان، SQLite و MariaDB | IntegrityError؛ چند NULL پذیرفته |
| RV-280 | شکست عمدی ساخت index در migration (فقط warning)، با mode درخواست‌شده‌ی `shadow` | `key_identity_ready = 0` با `unique_index_missing`؛ تلاش رفتن به `required` ۴۰۹ `key_identity_not_ready` |
| RV-281 | بدون index، دو UUID تکراری کاشته‌شده | `duplicate_uuid`؛ آماده نیست |
| RV-282 | اجرای دوباره‌ی محاسبه‌ی آمادگی و backfill | هیچ UUID عوض نمی‌شود؛ نتیجه یکسان |
| RV-283 | index یکتای `ledger_entries.reversal_of_id` روی دیتابیس قدیمی | با inspector تأیید؛ نبودش P11 را متوقف می‌کند |

### Evidence تمدید

هر سناریو یک‌بار روی User (`renew_user`) و یک‌بار روی Purchase (`renew_purchase`).

| کد | سناریو | Invariant |
|---|---|---|
| RV-284 | منبع فعال با quota و انقضای باقی‌مانده | `reserved`؛ ستون‌های اصلی ثابت؛ `rq`/`rd` به اندازه‌ی add زیاد |
| RV-285 | quota تمام‌شده: `q=100` مصرف‌شده، add=50 | `immediate_reset`؛ `q_after=50`، `u_after=0`؛ projection `add_quota_bytes=50` |
| RV-286 | انقضای گذشته | `immediate_reset`؛ `e_after = effective_now + add_days` دقیق |
| RV-287 | فقط حجم؛ فقط روز؛ هرکدام در هر دو mode | ستون بُعد صفر بدون تغییر |
| RV-288 | quota نامحدود با انقضای معتبر؛ انقضای NULL با quota باقی‌مانده؛ بدون هیچ حد | دو مورد اول `reserved`؛ سومی `immediate_reset` |
| RV-289 | تمدید رزروشده با `package_id`؛ و reset با `package_id` | اولی `rp_after`؛ دومی `pk_after` |
| RV-290 | evidence با mode نادرست؛ با `q_after = q_before + add` در reset؛ با `effective_now` ناسازگار با `e_after` | `effect_evidence_invalid`؛ rollback mutation |
| RV-291 | intent تمدید با `add_quota_bytes = 0` و `add_days = 0` | ثبت ۴۲۲ |
| RV-292 | تمدید رزروشده‌ی User و Purchase، با approval `required` (بعد از P9a)، پرداخت کارتی با مبلغ > 0 | effect `loyalty_purchase_recorded` در همان تراکنش؛ `finalize` → `completed` |

### HA و correlation

| کد | سناریو | Invariant |
|---|---|---|
| RV-293 | activation مشترک وقتی HA روشن است؛ و `auto_rate_limit_mode='enforced'` روی HA | هر دو ۴۰۹ `ha_requires_external_fencing` |
| RV-294 | روشن‌کردن HA وقتی P9a فعال است؛ و وقتی P9b فعال است | هر دو ۴۰۹ |
| RV-295 | شبیه‌سازی: grant روی primary؛ snapshot standby قبل از grant؛ promote | روی هیچ‌کدام enforcement فعال نیست (فعال‌سازی‌اش رد شده)؛ تست ثابت می‌کند با enforcement فرضی، grant دوم ممکن می‌بود |
| RV-296 | دو primary شبیه‌سازی‌شده با DB جدا | `registration_mode` و `auto_rate_limit_mode` هیچ‌کدام `required`/`enforced` نمی‌شوند |
| RV-297 | P3 shadow روی HA روشن | فقط log؛ approval واقعی مثل امروز؛ هیچ ۴۲۹ و هیچ gate |

### CHECK هویت approver

| کد | سناریو | Invariant |
|---|---|---|
| RV-298 | INSERT مستقیم approval، SQLite و MariaDB: original manual بدون approver؛ original auto با approver؛ فعلی auto با approver؛ approver بدون evidence kind و برعکس | هر پنج رد |
| RV-299 | INSERT مستقیم authority event: `from_mode='auto'` با from approver؛ `to_mode='manual'` بدون approver؛ approver بدون evidence kind؛ takeover با `to_mode='auto'` | هر چهار رد |

---

### آمادگی durable و mode مؤثر

| کد | سناریو | Invariant |
|---|---|---|
| RV-300 | وضعیت اولیه `required` و `ready = 1`؛ index حذف می‌شود یا inspector شکست می‌خورد؛ startup | `key_identity_ready = 0` و `key_identity_failure` **نوشته می‌شود** (هیچ CHECK مانع نیست)؛ mode درخواست‌شده `required` می‌ماند؛ mode مؤثر `blocked` |
| RV-301 | restart دوم بدون تعمیر | همچنان `blocked` |
| RV-302 | در `blocked`: ثبت auto و manual، production call، `finalize` | همه ۵۰۳ `receipt_approval_blocked`؛ هیچ mutation؛ هیچ اجرای legacy؛ هیچ `auto_granted_at` |
| RV-303 | تعمیر index، سپس `recheck` | `ready = 1`؛ mode مؤثر `required`؛ بدون گذر از `shadow` |
| RV-304 | همان شکست وقتی mode درخواست‌شده `shadow` است | legacy ادامه می‌یابد با `shadow_error`؛ و در حالت `required` هیچ‌وقت mode درخواست‌شده خودکار عوض نمی‌شود |
| RV-305 | شکست نوشتن tx ب آمادگی | guard همچنان ۵۰۳؛ درخواست بعدی دوباره تلاش نوشتن می‌کند |
| RV-306 | `GET runtime-mode` در هر پنج ترکیب جدول ۵.۵ | mode درخواست‌شده، mode مؤثر و دلیل شکست جدا و درست |

### loyalty در زمان پرداخت

| کد | سناریو | Invariant |
|---|---|---|
| RV-307 | `renew_user`، reset فوری | یک increment و یک effect |
| RV-308 | `renew_user`، رزرو؛ سپس activation | یک increment در لحظه‌ی approval؛ activation صفر increment و صفر پاداش |
| RV-309 | `renew_purchase`، reset فوری | یک increment روی User مالک |
| RV-310 | `renew_purchase`، رزرو؛ سپس activation | یک increment در لحظه‌ی approval؛ activation صفر |
| RV-311 | retry همان approval بعد از ثبت effect شمارنده | increment دوم ساخته نمی‌شود |
| RV-312 | `reset_usage` بدون add | بدون increment |
| RV-313 | void **قبل** از activation رزرو؛ و void **بعد** از آن | در هر دو، effect شمارنده و پاداش‌ها دقیقاً یک‌بار معکوس (یا `resource_deleted`)؛ هیچ پاداش دیررس بدون correlation |
| RV-314 | عبور از آستانه در هر چهار مسیر (RV-307 تا RV-310) | effectهای پاداش حاضر؛ بدون عبور، غایب |
| RV-315 | دو تمدید هم‌زمان روی یک User (دو Session واقعی، SQLite و MariaDB) | شمارنده دقیقاً +۲؛ بدون lost update؛ بدون پاداش دوباره برای یک آستانه |
| RV-316 | تمدید پرداخت‌شده‌ی غیر-approval (پنل؛ کیف‌پول)، رزرو | increment در لحظه‌ی تمدید؛ activation صفر |

### قرارداد shadow برای bridge

| کد | سناریو | Invariant |
|---|---|---|
| RV-317 | `PanelBridge`، mode `shadow`، آمادگی هویت کلید شکست‌خورده | پاسخ `proceed_legacy:true` با `shadow_error`؛ approval واقعی مثل امروز کامل می‌شود؛ یک ردیف `receipt_approval_shadow_events` |
| RV-318 | `RemoteBridge`، همان | همان |
| RV-319 | همان شکست در `required` (یعنی `blocked`) | ۵۰۳؛ هیچ mutation |
| RV-320 | caller فیلد `proceed_legacy` یا `mode` می‌فرستد؛ و در `required` production call کارتی بدون `approval_uuid` | فیلد نادیده؛ ۴۰۹ `approval_required` |
| RV-321 | mode بین ثبت و mutation عوض می‌شود: shadow→required؛ required→shadow؛ required→blocked | ۴۰۹ `approval_mode_changed` و `cancelled`؛ enforcement کامل ادامه دارد؛ ۵۰۳ |
| RV-322 | mode `off` | `proceed_legacy:true`؛ هیچ ردیفی ثبت نمی‌شود |
| RV-323 | `record_effect` ناهم‌خوان در `shadow`؛ و در `required` | shadow: savepoint برمی‌گردد، mutation commit، event ثبت؛ required: کل تراکنش rollback |
| RV-324 | خطای HTTP غیر-۲۰۰ از ثبت، در هر mode | `perform_approval` متوقف می‌شود؛ هیچ ادامه‌ی legacy از مسیر `except` |
| RV-325 | مرجع تصمیم خودکار: `shadow` در برابر `required` | shadow: تصمیم محلی، مرکزی فقط log؛ required: فقط backend |

---

### loyalty: reconciliation

| کد | سناریو | Invariant |
|---|---|---|
| RV-326 | آستانه ۳؛ خریدهای A، B، C؛ void A | R1 باطل با `loyalty_reconciliation` و `void_operation_id` ابطال A؛ cacheها ۲ و ۰؛ approval C همچنان `completed` |
| RV-327 | همان؛ void B؛ و جدا void C | void B مثل RV-326؛ void C: R1 باطل با `receipt_void` |
| RV-328 | شش خرید، دو پاداش؛ void یکی از خریدهای اول | فقط R2 باطل؛ R1 فعال |
| RV-329 | پاداش بی‌اعتبارشده‌ای که خرج شده (بعد از cutover کیف‌پول)؛ و پاداش اعطاشده قبل از cutover | اولی: claw-back و بدهی طبق ۱۲.۶؛ دومی: باطل علامت می‌خورد، بدون حرکت پول |
| RV-330 | void دو approval به دو ترتیب مختلف | وضعیت نهایی رویدادها و cacheها یکسان |
| RV-331 | retry گام `loyalty_reconciliation` | هیچ ردیف تازه‌ای باطل نمی‌شود؛ بدون claw-back دوم |
| RV-332 | خرید جدید بعد از RV-326 | `N=3` → پاداش جدید با `required=3` |
| RV-333 | پاداشی با `required_active_count` ذخیره‌شده؛ تغییر آستانه؛ سپس void یک خرید | اعتبار فقط با مقایسه‌ی `required_active_count` و `N`؛ هیچ ضرب دوباره‌ای با آستانه‌ی فعلی |
| RV-334 | خرید و void هم‌زمان روی یک User (دو Session، SQLite و MariaDB) | سریال با قفل ردیف User؛ cache = ledger |
| RV-335 | drift عمدی `purchase_count` یا `loyalty_rewards_given` | job تطبیق گزارش می‌کند |
| RV-336 | void با حذف User | رویدادها باطل؛ `user_id` رویدادها NULL؛ cache نوشته نمی‌شود |

### loyalty: cutover زمان شمارش

| کد | سناریو | Invariant |
|---|---|---|
| RV-337 | User با رزرو موجود؛ cutover | ۴۰۹ `loyalty_cutover_blocked` با فهرست؛ mode بدون تغییر |
| RV-338 | Purchase با رزرو موجود (فقط `reserved_package_id`) | همان |
| RV-339 | رزرو تجمیعی چند پرداخت قبل از cutover | cutover رد؛ هیچ backfill حدسی |
| RV-340 | بعد از حل رزروها | cutover موفق؛ baseline هر User با شمارنده‌ی غیرصفر |
| RV-341 | رزرو ساخته‌شده بعد از cutover، User و Purchase | یک رویداد در پرداخت؛ activation صفر |
| RV-342 | تلاش برگشت به `legacy_activation`؛ restart؛ deploy نسخه‌ی بعدی | مسیری وجود ندارد؛ mode ثابت |
| RV-343 | تمدید هم‌زمان با cutover | سریال با locking read؛ یا کامل legacy و دیده‌شده در preflight، یا کامل `payment_event` |
| RV-344 | mode `legacy_activation` (قبل از P9a)، correlation در shadow | loyalty دقیقاً مثل امروز؛ هیچ رویداد، هیچ مدرک؛ approvalها `completeness_generation = 0` و غیرقابل‌ابطال |

### loyalty: واجدبودن

| کد | سناریو | Invariant |
|---|---|---|
| RV-345 | فروش کارتی با approval معتبر `required`، مبلغ > 0 | دقیقاً یک `sale_payment_evidence` (`receipt_approval`) و یک رویداد `card_paid` |
| RV-346 | کیف‌پول: `captured`؛ فقط `held`؛ `released` | فقط اولی مدرک (`wallet_capture`) و رویداد دارد |
| RV-347 | فروش پنل با کسر اعتبار reseller | طبق تصمیم ۱۶؛ پیش‌فرض: بدون رویداد |
| RV-348 | ساخت/تمدید دستی رایگان پنل؛ `reset_usage` | بدون رویداد |
| RV-349 | trial؛ پکیج قیمت صفر با تأیید دستی | بدون رویداد |
| RV-350 | تخفیف ۱۰۰٪ | بدون رویداد؛ ردیف loyalty در manifest نیست؛ `finalize` → `completed` |
| RV-351 | `paid_amount` صفر؛ NULL (fallback)؛ مثبت بدون approval | هیچ‌کدام مدرک و رویداد ندارند؛ `amount_source` فقط ثبت می‌شود |
| RV-352 | retry هر مسیر واجد | یک مدرک و یک رویداد (UNIQUEهای `sale_payment_evidence` و `loyalty_purchase_events`) |
| RV-353 | Ledger قدیمی (قبل از P9a) | مدرک ندارد؛ واجد نیست؛ فقط در baseline شمرده شده |

---

### loyalty: epoch آستانه

| کد | سناریو | Invariant |
|---|---|---|
| RV-354 | آستانه ۳، شش خرید؛ آستانه → ۱۰ در `N=6` | هیچ پاداشی در `N=10` و `N=15`؛ پاداش بعدی دقیقاً در `N=16` با `required_active_count=16` |
| RV-355 | آستانه ۱۰، `N=7`؛ آستانه → ۳ | یک پاداش در `N=10`؛ بدون catch-up؛ بدون پاداش تکراری |
| RV-356 | ۳ → ۱۰ در `N=6`؛ دو خرید؛ ۱۰ → ۵ در `N=8` | پاداش بعدی در `N=13` |
| RV-357 | epoch ۲ با anchor 6 و پاداش `required=16`؛ void یک خرید epoch ۱ | آن پاداش باطل؛ R1 و R2 معتبر |
| RV-358 | بعد از RV-357 یک خرید جایگزین | همان slot با `reearn_generation=2`؛ source و effect تازه؛ ردیف باطل‌شده‌ی قبلی دست‌نخورده |
| RV-359 | خرید (و retry آن) هم‌زمان با `change_policy` (دو Session، SQLite و MariaDB) | سریال؛ خرید کاملاً در یک epoch؛ یک رویداد |
| RV-360 | سه پاداش یک User: یکی از فروش approvalدار، دو تا از فروش کیف‌پولی | سه ردیف `loyalty_reward_events` و سه `wallet_credit_source` جدا؛ فقط پاداش approvalدار effect دارد (با کلید `{epoch_id}:{slot}`)؛ هیچ تجمیع |
| RV-361 | ابطال یکی از سه پاداش | lot و effect دو پاداش دیگر بایت‌به‌بایت بدون تغییر |
| RV-362 | property: برای آستانه‌های ۱ تا ۱۰ و دنباله‌های تصادفی خرید/ابطال | هر خرید حداکثر یک پاداش می‌سازد |
| RV-363 | `N_before` یا epoch بین ثبت approval و اجرا عوض می‌شود و پیش‌بینی slot فرق می‌کند | `cancelled` با `loyalty_projection_changed`؛ هیچ mutation |
| RV-364 | cutover با `P=7`، `T=3`، `G=2`؛ و با catch-up معوق (`P=9`، `G=1`) | anchor 6 → پاداش بعدی در ۹؛ anchor 9 → پاداش بعدی در ۱۲، بدون پاداش معوق |
| RV-365 | تغییر آستانه بلافاصله بعد از cutover؛ و آستانه‌ی صفر | epoch تازه؛ anchor در اولین خرید؛ آستانه‌ی صفر بدون پاداش |
| RV-366 | پاداش باطل‌شده‌ی یک epoch بسته؛ سپس خرید که `N` را برمی‌گرداند | بازکسب نمی‌شود |
| RV-367 | تغییر تنظیمات loyalty از مسیر generic تنظیمات (AST) | صفر؛ فقط `change_policy` |

### مدرک پرداخت

| کد | سناریو | Invariant |
|---|---|---|
| RV-368 | کلید API عادی، `paid_amount` مثبت، `payment_method='card'`، بدون approval | فروش ثبت می‌شود؛ هیچ مدرک؛ هیچ رویداد loyalty |
| RV-369 | همان با `payment_method='wallet'` بدون debit captured | همان |
| RV-370 | یک approval یا یک debit برای دو فروش | INSERT دوم مدرک رد؛ فروش دوم rollback (SQLite و MariaDB) |
| RV-371 | approval tenant دیگر به‌عنوان مدرک | رد |
| RV-372 | revoke و حذف کلید API بعد از ساخت مدرک | مدرک و رویداد بدون تغییر |
| RV-373 | `refund_debit` یک فروش کیف‌پولی `captured` (بدون approval) که پاداش ساخته بود؛ و retry | رویداد خرید باطل؛ پاداش از روی ردیف خودش برگشته (`sale_refund`، state جزءهای غیرصفر پر)؛ هیچ lookup یا ردیف `receipt_approval_effects`؛ retry بدون برگشت دوم |
| RV-374 | مبلغ فروش متفاوت از `amount_snapshot` approval | مدرک ساخته نمی‌شود؛ `effect_manifest_mismatch` |
| RV-375 | فروش با اعتبار reseller | طبق تصمیم ۱۶؛ پیش‌فرض: بدون مدرک |

### transition برگشت effect

| کد | سناریو | Invariant |
|---|---|---|
| RV-376 | اجرای هر transition جدول ۱۴.۳ برای هر `effect_type` روی schema واقعی، SQLite و MariaDB | همه با CHECKها پذیرفته |
| RV-377 | ترکیب نامعتبر: `voided_at` بدون state؛ state بدون `void_operation_id`؛ `reversed_value` با `resource_deleted`؛ `fully_reversed` بدون مقدار | هر چهار رد |
| RV-378 | source کاملاً مصرف‌نشده؛ نیمه‌مصرف‌شده‌ی خود مشتری؛ lot معرف؛ پاداش قبل از cutover کیف‌پول | به ترتیب `fully_reversed`، `reversed_with_debt`، `reversed_with_writeoff`، `unrecoverable` |
| RV-379 | effect approval سالم C در ابطال A برگشته | `void_operation_id` = عملیات A؛ approval C `completed`؛ audit هر دو آن را نشان می‌دهد؛ `finalize` و job تطبیق خطا نمی‌دهند |
| RV-380 | retry عملیات A؛ و سپس ابطال خود C | effect دوباره برگشته نمی‌شود؛ مقدار ثبت‌شده‌ی اول می‌ماند |
| RV-381 | AST: نوشتن `voided_at`، `reversal_state`، `reversed_value` یا `void_operation_id` روی effect بیرون از `mark_effect_reversed` | صفر |

### ترتیب فازها

| کد | سناریو | Invariant |
|---|---|---|
| RV-382 | activation مشترک قبل از P9 | ۴۰۹ `wallet_not_enforced` |
| RV-383 | فعال‌کردن P9b، P10 یا P11 قبل از P9a | رد |

---

### Activation مشترک و نسل کامل‌بودن

| کد | سناریو | Invariant |
|---|---|---|
| RV-384 | `PUT runtime-mode` با `registration_mode='required'`؛ و UPDATE مستقیم DB که فقط یکی از دو mode را عوض کند | ۴۰۹ `use_joint_activation`؛ CHECK رد می‌کند. پس عبور از آستانه در `create_user` زیر `required` با loyalty legacy ممکن نیست |
| RV-385 | activation با هر پیش‌شرط شکسته، جدا: کیف‌پول غیر-`enforced`؛ کلید آماده نیست؛ HA روشن؛ رزرو موجود (User و Purchase)؛ کلید managed با پروتکل قدیمی | ۴۰۹ با کد همان پیش‌شرط و فهرست؛ هیچ تغییر |
| RV-386 | activation موفق | یک تراکنش: هر دو mode، `completeness_generation = 1`، epoch اول، baselineها و anchorها |
| RV-387 | crash تزریق‌شده در هر نقطه‌ی تراکنش activation | rollback کامل؛ هر دو mode در مقدار قبلی؛ هیچ baseline یا epoch |
| RV-388 | retry activation | ۲۰۰؛ `completeness_generation` زیاد نمی‌شود |
| RV-389 | deactivate قبل از اولین approval و اولین رویداد loyalty؛ سپس activation دوباره | برگشت هر دو mode؛ epoch نسل اول بسته؛ activation دوم `completeness_generation = 2` با baseline و epoch تازه |
| RV-390 | deactivate بعد از اولین approval `required`؛ و بعد از اولین رویداد loyalty | ۴۰۹ `activation_irreversible` |
| RV-391 | تمدید فوری و activation رزرو در پنجره‌ی rollout: رزرو ساخته‌شده قبل از activation | activation با `loyalty_cutover_blocked` رد؛ بعد از حل و activation، activation رزرو چیزی نمی‌شمارد |
| RV-392 | approval ثبت‌شده در shadow، قبل از activation، `completed` | preview و execute ابطال ۴۰۹ `approval_not_voidable_generation` |
| RV-393 | برای هر مسیر approval `required` (new با و بدون اتصال، existing، هر دو renew، topup): اختلاف `User.balance`، quota و شمارنده‌ها قبل و بعد از mutation | برابر مجموع effectهای manifest؛ هیچ تغییر خارج از manifest |
| RV-394 | approval shadow در جریان هنگام activation | mutation بعدی ۴۰۹ `approval_mode_changed`؛ `cancelled` |
| RV-395 | activation هم‌زمان با یک خرید (دو Session) | ترتیب قفل یکسان؛ خرید یا کاملاً قبل (legacy) یا کاملاً بعد |

### پاداش بدون approval

| کد | سناریو | Invariant |
|---|---|---|
| RV-396 | خرید کیف‌پولی بدون approval از آستانه می‌گذرد | ردیف پاداش با `causation_type='wallet_capture'` و source؛ صفر ردیف `receipt_approval_effects` |
| RV-397 | فروش approvalدار از آستانه می‌گذرد | ردیف پاداش، source، و effectهای `credit:loyalty:{epoch}:{slot}` هر سه |
| RV-398 | ابطال یک approval که هم یک پاداش approvalدار و هم یک پاداش کیف‌پولی را بی‌پشتوانه می‌کند | هرکدام از شاخه‌ی خودش برگشته؛ برای دومی هیچ جست‌وجوی effect |
| RV-399 | پاداش حجمی کیف‌پولی با shortfall | `quota_reversal_shortfalls` با `loyalty_reward_event_id` |
| RV-400 | فروش اعتبار reseller که پاداش می‌سازد، و refund آن (اگر تصمیم ۱۶ بله شد) | `causation_type='admin_credit_spend'`؛ همان قرارداد RV-373 |
| RV-401 | INSERT مستقیم پاداش با `causation_type` و ارجاع ناسازگار | CHECK رد |

### Binding مدرک پرداخت

| کد | سناریو | Invariant |
|---|---|---|
| RV-402 | debit کیف‌پول متعلق به User یا account دیگر | مدرک رد؛ فروش rollback |
| RV-403 | مبلغ debit یا `amount_snapshot` approval متفاوت از مبلغ فروش | رد (validator و CHECK `sale_amount = captured_amount`) |
| RV-404 | debit `released` یا `reversed` | رد |
| RV-405 | approval مربوط به User، Purchase یا tenant دیگر؛ approval نسل قبلی | رد |
| RV-406 | spend اعتبار متعلق به reseller دیگر، یا خارج از همان تراکنش فروش | رد |
| RV-407 | فروش اعتبار reseller با قیمت مشتری متفاوت از هزینه‌ی همکاری | پذیرفته؛ `sale_amount` و `captured_amount` هر دو ثبت و متفاوت |
| RV-408 | استفاده‌ی دوباره و رقابت دو Session روی یک مدرک، برای هر سه نوع، SQLite و MariaDB | دقیقاً یک فروش مدرک می‌گیرد |
| RV-409 | P9b قبل و بعد از P9a | قبل: رد؛ بعد: seed و ۴۲۹ |

---

### نسل activation روی رویداد خرید

| کد | سناریو | Invariant |
|---|---|---|
| RV-410 | بعد از activation، یک خرید واجد که پاداش نمی‌سازد؛ سپس deactivate | ۴۰۹ `activation_irreversible` |
| RV-411 | بعد از activation، یک خرید کیف‌پولی بدون approval؛ سپس deactivate | ۴۰۹ |
| RV-412 | deactivate قبل از هر approval و هر رویداد | epoch باز نسل جاری `closed_at` می‌گیرد؛ هیچ epoch بازی نمی‌ماند |
| RV-413 | activation دوم بعد از RV-412 | دقیقاً یک epoch باز، با `generation = completeness_generation` جاری |
| RV-414 | رویداد خرید با `completeness_generation = 1`؛ پاداشی با `reearn_generation = 2`؛ runtime state در نسل ۲ بدون رویداد | شرط rollback فقط `completeness_generation` رویدادهای خرید را می‌شمارد؛ `reearn_generation` اثری ندارد |
| RV-415 | job تطبیق: دو epoch باز؛ epoch باز با نسل قدیمی؛ epoch باز در `legacy_activation` | هر سه گزارش می‌شوند |
| RV-416 | RV-410 تا RV-413 روی SQLite و MariaDB | نتیجه‌ی یکسان |

### نتیجه‌ی برگشت دو جزء پاداش

| کد | سناریو | Invariant |
|---|---|---|
| RV-417 | پاداش با اعتبار خرج‌شده و حجم نیمه‌مصرف‌شده | `credit_reversal_state='reversed_with_debt'` و `quota_reversal_state='partially_reversed'` در یک تراکنش؛ shortfall فقط برای حجم |
| RV-418 | اعتبار دست‌نخورده، منبع حجم در همان ابطال حذف می‌شود | `fully_reversed` و `resource_deleted`؛ `quota_reversed_bytes` NULL |
| RV-419 | اعتبار lot شخص ثالث خرج‌شده، حجم دست‌نخورده | `reversed_with_writeoff` و `fully_reversed` |
| RV-420 | پاداش فقط اعتبار؛ پاداش فقط حجم | ستون‌های جزء صفر NULL؛ CHECKها برقرار |
| RV-421 | epoch با اعتبار و حجم هر دو صفر؛ و epoch فقط حجم وقتی مقصد حجم وجود ندارد | پاداشی ساخته نمی‌شود؛ INSERT مستقیم با هر دو صفر رد |
| RV-422 | reconciliation یک پاداش با هر دو جزء؛ سپس retry؛ و UPDATE قدم ۳ با rowcount = 0 تزریق‌شده | تا قبل از UPDATE نهایی هیچ ستون پاداش نوشته نشده؛ یک UPDATE همه‌ی هشت فیلد را با `WHERE state = 'active'` می‌نویسد؛ retry پاداش را انتخاب نمی‌کند و هیچ جزئی دوباره برگشته نمی‌شود؛ با rowcount = 0 کل تراکنش rollback و source و quota بدون تغییر |
| RV-423 | پاداش approvalدار با هر دو جزء | effect اعتبار state جزء اعتبار و effect حجم state جزء حجم را دارد |
| RV-424 | INSERT/UPDATE مستقیم ناسازگار روی SQLite و MariaDB: پاداش فعال با state برگشت؛ voided با اعتبار > 0 بدون state اعتبار؛ state حجم با `quota_bytes = 0`؛ `quota_reversed_bytes` با `resource_deleted` | هر چهار رد |

---

## ۲۳. تصمیم‌های باز Product

هرکدام یک پیش‌فرض امن دارد که طراحی با آن کامل است.

| # | موضوع | پیش‌فرض این سند |
|---|---|---|
| ۱ | `tenant_scope_key`: ریشه‌ی درخت ادمین یا مالک مستقیم. همین کلید subject سقف تأیید خودکار هم هست | ریشه‌ی درخت (seller → ادمین والد) |
| ۲ | بدهی باز فقط خرید کیف‌پولی را مسدود کند یا خرید با رسید کارت را هم | هر دو؛ topup همیشه مجاز |
| ۳ | ابطال `topup` روی account قدیمی: حذف کل account یا نه | User و سرویس‌های سالم حذف نمی‌شوند؛ lot اسکمی claw-back، بدهی ساخته، خرید و تمدید قفل، topup تسویه‌کننده مجاز |
| ۴ | پاداش خرج‌شده‌ی معرف: write-off یا بدهی برای معرف | write-off نرمال‌شده در `wallet_writeoffs`؛ بدون بدهی |
| ۵ | بدهی تازه خودکار از lotهای سالم موجود همان identity کسر شود؟ | خیر |
| ۶ | lot سالم باقی‌مانده‌ی account حذف‌شده | دست‌نخورده و غیرقابل‌خرج |
| ۷ | Userهای با `balance < 0` در preflight | cutover متوقف تا تصمیم موردی |
| ۸ | باز شدن ابطال برای ادمین مالک | فقط superadmin |
| ۹ | متن تأیید دوم | — |
| ۱۰ | shortfall پاداش حجمی: فقط ثبت، یا مسدودی/بدهی | فقط ثبت durable |
| ۱۱ | آستانه‌ی زمانی hold قدیمی برای ورود به `wallet_hold_resolution` | ۱۵ دقیقه |
| ۱۲ | مقصد پاداش حجمی | فقط وقتی دقیقاً یک مقصد quota‌دار محدود وجود دارد (User بدون Purchase، یا تک Purchase). چند Purchase یا نامحدود: پاداش حجمی اعمال نمی‌شود، پاداش اعتباری اعمال می‌شود. جایگزین برای منبع نامحدود: حفظ رفتار امروز با برگشت دقیق به صفر (شاخه‌ی `before_was_unlimited` در ۱۴.۱) |
| ۱۳ | اعتماد به کلید نصب managed به‌عنوان proxy approver (۶.۴)، یا challenge یک‌بارمصرف Telegram | اعتماد به کلید managed |
| ۱۴ | تأیید خودکار topup | غیرفعال؛ فعال‌شدنش policy مستقل می‌خواهد |
| ۱۵ | زمان شمارش loyalty. **باید قبل از P9a بسته شود** | زمان پرداخت، برای User و Purchase یکسان؛ activation رزرو چیزی نمی‌شمارد (۱۴.۲). تمدید Purchase و تمدید رزروشده‌ی User نسبت به امروز عوض می‌شوند |
| ۱۶ | فروش پنل با کسر از اعتبار reseller خرید loyalty حساب شود؟ و پرداخت بیرونی integration؟ **باید قبل از P9a بسته شود** | هر دو خیر. امروز هر تمدید/خرید پنل و هر فروش `/api/bot` شمرده می‌شود؛ این پیش‌فرض آن را عوض می‌کند |
| ۱۷ | پاداش loyalty اعطاشده قبل از cutover کیف‌پول که با reconciliation بی‌اعتبار می‌شود | باطل علامت می‌خورد؛ پولی پس گرفته نمی‌شود |
| ۱۸ | policy تغییر آستانه. **باید قبل از P9a بسته شود** | epoch آینده‌نگر: پاداش‌های گذشته معتبر، بدون catch-up، پیشرفت epoch جدید از anchor هر User (مثال: ۳→۱۰ در `N=6` یعنی پاداش بعدی در ۱۶). slot یک epoch بسته بازکسب نمی‌شود |
| ۱۹ | anchor baseline در cutover loyalty. **باید قبل از P9a بسته شود** | `P - (P mod T)`: پیشرفت چرخه‌ی جاری حفظ می‌شود؛ catch-up معوق قدیمی ساخته نمی‌شود |

| ۲۰ | سقف تأیید خودکار زودتر از P9a (جدا از ثبت اجباری effectها) لازم است؟ | خیر: P9b فقط بعد از P9a. اگر Product آن را زودتر بخواهد، یک طراحی جدا لازم است که grant خودکار را بدون کامل‌بودن effect اجباری کند |

موارد ۳ و ۴ پیش‌فرض پیشنهادی‌اند و تا تأیید صریح Product «تصمیم ثابت» نیستند.

دو موضوع دیگر باز نیستند و policy ثابت‌اند (بخش ۱): cutover نیازمند P6 است؛ و ابطال پرداخت کارت pointer فعلی و رخدادهای سالم بعدی را حفظ می‌کند.

---

## ۲۴. خارج از Scope

- backfill `approval_uuid` برای داده‌ی قدیمی.
- ابطال `link`.
- state machine durable provisioning (جدول گام‌ها، reservation ساخت‌یافته، هویت قطعی هر پروتکل، خواندن remote هر client). قرارداد آن با کیف‌پول در ۱۲.۳ کامل است؛ خودش طراحی جداست و P6 و همه‌ی فازهای بعد پشت آن مسدودند.
- تأیید خودکار topup.
- challenge یک‌بارمصرف Telegram برای اثبات کلیک approver.
- coordinator بیرونی و هر راه‌حل split-brain برای HA؛ در نتیجه P6 تا P11 روی نصب HA.
- دفاع در برابر کسی که دسترسی نوشتن مستقیم به دیتابیس دارد (بخش ۰).
- reverse-migration از wallet-lot به مسیر legacy.
- refund جزئی یک debit (برگشت هر allocation فقط کامل است).
- روشن‌کردن `PRAGMA foreign_keys` در SQLite (به‌جای آن: enforcement برنامه‌ای جدول ۴.۱).
- تفکیک سهم پاداش حجمی وقتی منبع رزرو تمدید فعال دارد یا quota آن بعد از پاداش بازنویسی شده (این حالت‌ها در ۱۴.۱ به `blocked` می‌رسند).
- تنظیم دستی شمارنده‌ی کارت.
- import تاریخچه‌ی sqlite محلی بات‌ها برای «مشتری قبلی».
- quota/RADIUS consistency.
