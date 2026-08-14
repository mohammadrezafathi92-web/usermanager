"""Seeds the «تبلیغات» section with a ready-made rotation of channel adverts
(the same copy as docs/ad-copy.md, in importable form).

Run it from the project directory on the server:

    docker compose exec backend python -m app.scripts.seed_ads

Safe to re-run: an advert whose title already exists is skipped, so it never
duplicates or overwrites anything you have since edited by hand. Pass
--replace to delete and re-create the seeded titles instead of skipping.

Two flags decide what the placeholders resolve to:

    --package-id N        which package the adverts advertise
    --discount-code-id N  which code the two discount adverts use

Both are optional. Without --package-id the newest enabled package owned by
the target admin is used, because an advert with no package renders every
figure as an em dash - a channel post reading "فقط — تومان" is worse than no
post at all. If there is no usable package, nothing is seeded and the reason
is printed.

The two discount adverts are created DISABLED when no code is given, rather
than skipped: they are ready to switch on the moment a code exists, but can
never publish a dash where the code should be in the meantime.
"""
from __future__ import annotations

import argparse

from .. import models
from ..database import SessionLocal

# (title, needs_discount_code, body)
ADS: list[tuple[str, bool, str]] = [
    ("معرفی ساده", False, """🚀 پکیج {package}

📦 {quota}
⏳ {days} روز
💰 {price} تومان

تحویل آنی و خودکار، بدون معطلی.
سفارش: {bot}"""),

    ("چند پروتکل", False, """⚡️ یک اشتراک، شش پروتکل

WireGuard · OpenVPN · V2Ray · L2TP · IKEv2 · SSTP
هر کدام جواب نداد، سراغ بعدی برو — بدون خرید دوباره.

📦 {package} — {quota} / {days} روز
💰 {price} تومان

{bot}"""),

    ("کد تخفیف با مهلت", True, """🎁 کد تخفیف فعال شد

کد: {code}
⏰ فقط تا {code_expires}

روی پکیج {package} ({quota} · {days} روز)
قیمت پایه: {price} تومان

کد را موقع خرید در ربات وارد کن 👇
{bot}"""),

    ("چند دستگاه", False, """📱💻 روی گوشی، لپ‌تاپ و تبلت — هم‌زمان

با یک اشتراک {package} همه‌ی دستگاه‌هایت وصل می‌شوند.

📦 {quota} | ⏳ {days} روز | 💰 {price} تومان

{bot}"""),

    ("تمدید بدون دردسر", False, """🔄 تمدید بدون دردسر

یوزر و پسوردت همان می‌ماند.
کانفیگ جدید نمی‌گیری، چیزی را دوباره تنظیم نمی‌کنی —
فقط حجم و زمانت تازه می‌شود.

{package} · {quota} · {days} روز · {price} تومان
{bot}"""),

    ("پشتیبانی", False, """🛟 پشتیبانی واقعی، نه ربات جواب‌های آماده

سؤالی داشتی، مشکلی پیش آمد — همین‌جا در ربات بپرس.

📦 {package} — {quota} / {days} روز
💰 {price} تومان

{bot}"""),

    ("شروع سریع", False, """⏱ سه دقیقه تا اتصال

۱. وارد ربات شو
۲. پکیج {package} را انتخاب کن
۳. کانفیگ را بگیر و وصل شو

{quota} · {days} روز · {price} تومان
{bot}"""),

    ("حساب کن", False, """💡 حساب کن ببین

پکیج {package}
{quota} حجم برای {days} روز
فقط {price} تومان

روزی چند تومان می‌شود؟ خودت حساب کن 🙂
{bot}"""),

    ("پیشنهاد هفته", True, """🔥 پیشنهاد این هفته

{package} — {quota} / {days} روز

کد تخفیف: {code}
مهلت: {code_expires}

هر وقت خواستی، همین کد را در ربات وارد کن.
{bot}"""),

    ("گزارش مصرف", False, """📊 همیشه بدان چقدر مانده

حجم مصرفی و تاریخ انقضای سرویست را
هر لحظه داخل ربات ببین — بدون تماس با کسی.

{package} · {quota} · {days} روز · {price} تومان
{bot}"""),

    ("کیفیت اتصال", False, """🌐 اتصال پایدار برای کار و تماس

مناسب جلسه‌ی آنلاین، آپلود و دانلود حجیم.

📦 {package}
{quota} · {days} روز · {price} تومان

{bot}"""),

    ("یادآوری تمدید", False, """سرویست رو به تمام شدن است؟

قبل از قطع شدن تمدید کن:
{package} — {quota} / {days} روز — {price} تومان

{bot}"""),
]


def _target_admin(db, admin_id: int | None) -> models.AdminUser | None:
    if admin_id:
        return db.get(models.AdminUser, admin_id)
    return (
        db.query(models.AdminUser)
        .filter(models.AdminUser.is_superadmin == True)  # noqa: E712
        .order_by(models.AdminUser.id)
        .first()
    )


def _pick_package(db, admin: models.AdminUser, package_id: int | None) -> models.Package | None:
    if package_id:
        return db.get(models.Package, package_id)
    q = db.query(models.Package).filter(models.Package.enabled == True)  # noqa: E712
    # An Admin's own packages first; fall back to unowned/global ones so a
    # fresh install with only global packages still seeds.
    owned = q.filter(models.Package.owner_admin_id == admin.id).order_by(models.Package.id.desc()).first()
    return owned or q.order_by(models.Package.id.desc()).first()


def seed(admin_id: int | None, package_id: int | None, discount_code_id: int | None, replace: bool) -> None:
    db = SessionLocal()
    try:
        admin = _target_admin(db, admin_id)
        if admin is None:
            print("ادمین پیدا نشد - با --owner-admin-id یک شناسه بده")
            return

        package = _pick_package(db, admin, package_id)
        if package is None:
            print(
                "هیچ پکیج فعالی پیدا نشد. اول یک پکیج بساز، یا با --package-id شناسه‌اش را بده.\n"
                "بدون پکیج، متن تبلیغ به‌جای قیمت و حجم خط تیره نشان می‌دهد."
            )
            return

        code = db.get(models.DiscountCode, discount_code_id) if discount_code_id else None
        if discount_code_id and code is None:
            print(f"کد تخفیف با شناسه {discount_code_id} پیدا نشد - تبلیغ‌های کددار غیرفعال ساخته می‌شوند")

        channel = db.query(models.AdChannel).filter(models.AdChannel.owner_admin_id == admin.id).first()
        if channel is None:
            channel = models.AdChannel(owner_admin_id=admin.id)
            db.add(channel)
            db.flush()

        existing = {
            p.title: p
            for p in db.query(models.AdPost).filter(models.AdPost.channel_id == channel.id).all()
        }

        added = skipped = replaced = disabled = 0
        for order, (title, needs_code, body) in enumerate(ADS):
            if title in existing:
                if not replace:
                    skipped += 1
                    continue
                db.delete(existing[title])
                db.flush()
                replaced += 1

            enabled = True
            if needs_code and code is None:
                enabled = False
                disabled += 1

            db.add(models.AdPost(
                channel_id=channel.id,
                title=title,
                body=body,
                package_id=package.id,
                discount_code_id=code.id if (needs_code and code) else None,
                sort_order=order,
                enabled=enabled,
            ))
            added += 1

        db.commit()
        print(f"ادمین: {admin.username}   پکیج: {package.name}   کد تخفیف: {code.code if code else '-'}")
        print(f"افزوده شد: {added}   جایگزین شد: {replaced}   رد شد (از قبل بود): {skipped}")
        if disabled:
            print(
                f"{disabled} تبلیغ کددار غیرفعال ساخته شد. یک کد تخفیف بساز و دوباره با\n"
                "--discount-code-id N --replace اجرا کن، یا از خود پنل فعالشان کن."
            )
        print("حالا در پنل: تبلیغات ← آیدی کانال را بگذار و ربات را ادمین کانال کن.")
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="افزودن تبلیغ‌های آماده به بخش تبلیغات")
    parser.add_argument("--owner-admin-id", type=int, default=None,
                        help="برای کدام ادمین (پیش‌فرض: سوپرادمین)")
    parser.add_argument("--package-id", type=int, default=None,
                        help="کدام پکیج تبلیغ شود (پیش‌فرض: آخرین پکیج فعال)")
    parser.add_argument("--discount-code-id", type=int, default=None,
                        help="کد تخفیف برای دو تبلیغ کددار")
    parser.add_argument("--replace", action="store_true",
                        help="تبلیغ‌های هم‌نام را پاک و دوباره بساز")
    args = parser.parse_args()
    seed(args.owner_admin_id, args.package_id, args.discount_code_id, args.replace)


if __name__ == "__main__":
    main()
