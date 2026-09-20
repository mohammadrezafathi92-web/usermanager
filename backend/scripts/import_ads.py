#!/usr/bin/env python3
"""Load a set of ready-written adverts into an admin's rotation.

    docker compose exec backend python scripts/import_ads.py --list
    docker compose exec backend python scripts/import_ads.py --admin 1 --dry-run
    docker compose exec backend python scripts/import_ads.py --admin 1 --replace
    docker compose exec backend python scripts/import_ads.py --all --replace

The texts live in docs/ad-texts.md, written for a human to read and edit;
this script holds the same texts in a form the database can take. They are
kept in one place here rather than parsed out of the markdown, because a
parser that breaks on a heading would fail at the worst moment - while
someone is replacing their whole rotation on a live channel.

Why a script and not a button: importing adverts is something an admin does
once, and a panel screen for it would be a page to build, translate,
permission-check and maintain for an action nobody repeats. A script that
prints exactly what it will do, refuses to guess, and can be run again
safely is the cheaper honest answer.

Nothing is destroyed without being asked for. --replace deletes the
channel's existing posts, and says how many first; without it the new ones
are simply added after whatever is already there.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal  # noqa: E402
from app import models  # noqa: E402


# (title, body). The title is an admin-facing label only - never sent -
# so it says what the advert is FOR, which is what you need when picking
# one out of a list of two dozen.
#
# Placeholders ({package}, {price}, {bot}, {code}...) are filled by
# services/ads.py at send time. An advert using {package} or {price} needs
# a package attached to it in the panel, or those render as a dash - the
# ones that need one are marked in their title.
ADS: list[tuple[str, str]] = [
    ("تست رایگان ۱ - اول امتحان کن",
     "🎁 اول امتحان کن، بعد پول بده.\n\n"
     "تست رایگان ما نه شماره کارت می‌خواهد، نه پیش‌پرداخت، نه ثبت‌نام طولانی.\n"
     "وارد {bot} شو، فروشگاه را باز کن، روی «دریافت رایگان» بزن.\n\n"
     "سرویست همان لحظه ساخته می‌شود."),

    ("تست رایگان ۲ - سی ثانیه",
     "⚡️ سی ثانیه تا اولین اتصال\n\n"
     "۱. {bot} را باز کن\n"
     "۲. دکمه‌ی «فروشگاه» پایین صفحه\n"
     "۳. «دریافت رایگان»\n\n"
     "تمام. نه فرم، نه انتظار، نه تماس با پشتیبانی."),

    ("تست رایگان ۳ - چرا می‌دهیم",
     "چرا تست رایگان می‌دهیم؟\n\n"
     "چون می‌دانیم بعد از امتحان کردن برمی‌گردی.\n"
     "سرعت و پایداری‌مان را از حرفِ ما نپذیر - خودت ببین.\n\n"
     "🎁 تست رایگان در فروشگاه ربات: {bot}"),

    ("تست رایگان ۴ - بدون شرط",
     "🎁 تست رایگان، بدون هیچ شرطی\n\n"
     "بدون کارت بانکی. بدون شماره موبایل. بدون «اول شارژ کن».\n"
     "یک دکمه در مینی‌اپ، و سرویس در دستت.\n\n"
     "{bot}"),

    ("تست رایگان ۵ - ظرفیت روزانه (فقط اگر سقف تنظیم شده)",
     "تست رایگان ما محدود است - نه برای اینکه شما را عجول کنیم،\n"
     "بلکه چون ظرفیت روزانه دارد و واقعاً تمام می‌شود.\n\n"
     "اگر امروز می‌بینی‌اش، یعنی هنوز جا هست. 🎁\n\n"
     "{bot}"),

    ("مینی‌اپ ۱ - داخل تلگرام",
     "📱 فروشگاه ما داخل خود تلگرام است.\n\n"
     "نه سایت، نه لینک پرداخت مشکوک، نه اپلیکیشن جداگانه.\n"
     "همان تلگرامی که باز است: {bot} ← فروشگاه\n\n"
     "پلن‌ها، کیف پول، سرویس‌های فعال، تمدید - همه یک‌جا."),

    ("مینی‌اپ ۲ - دیگر چت لازم نیست",
     "دیگر لازم نیست با ربات چت کنی.\n\n"
     "مینی‌اپ ما یک فروشگاه واقعی است: پلن‌ها را کنار هم می‌بینی،\n"
     "مقایسه می‌کنی، یکی را می‌زنی، تمام.\n\n"
     "🛍 {bot}"),

    ("مینی‌اپ ۳ - همه در یک صفحه",
     "📱 هر چیزی که لازم داری، در یک صفحه:\n\n"
     "• پلن‌های فعال و قیمتشان\n"
     "• حجم باقی‌مانده و تاریخ انقضای سرویست\n"
     "• کیف پول و افزایش اعتبار\n"
     "• 🎁 تست رایگان\n\n"
     "فروشگاه را از {bot} باز کن."),

    ("مینی‌اپ ۴ - سه سؤال همیشگی",
     "سه تا سؤالی که همیشه می‌پرسید، حالا خودت جوابش را می‌بینی:\n\n"
     "«چقدر حجمم مانده؟»  «کی تمام می‌شود؟»  «چطور تمدید کنم؟»\n\n"
     "📱 فروشگاه ← سرویس‌های من. همین.\n\n"
     "{bot}"),

    ("مینی‌اپ ۵ - فروشگاه جدید",
     "🛍 فروشگاه جدید ما بالا آمد.\n\n"
     "طراحی تازه، پلن‌ها دسته‌بندی‌شده، خرید با دو تپ.\n"
     "و برای کسانی که هنوز مشتری ما نیستند - 🎁 تست رایگان بالای صفحه.\n\n"
     "{bot}"),

    ("پکیج ۱ - ساده (پکیج لازم دارد)",
     "📦 {package}\n\n"
     "{quota} - {days} روز\n"
     "💰 {price} تومان\n\n"
     "خرید با دو تپ در فروشگاه {bot}\n"
     "و اگر هنوز مطمئن نیستی، 🎁 تست رایگان همان بالاست."),

    ("پکیج ۲ - یک‌خطی (پکیج لازم دارد)",
     "{package} | {quota} | {days} روز\n\n"
     "قیمت: {price} تومان\n\n"
     "بدون واسطه، بدون انتظار - مستقیم از فروشگاه داخل تلگرام.\n"
     "🎁 تست رایگان هم داریم، اول آن را بگیر.\n\n"
     "{bot}"),

    ("پکیج ۳ - حساب ساده (پکیج لازم دارد)",
     "💡 حساب ساده:\n\n"
     "{quota} برای {days} روز = {price} تومان\n\n"
     "اگر ماهی چند بار قطعی اینترنت کارَت را عقب می‌اندازد،\n"
     "این عدد از یک بار عقب افتادن کمتر است.\n\n"
     "📱 {bot} ← فروشگاه"),

    ("پکیج ۴ - موجود شد (پکیج لازم دارد)",
     "📦 {package} دوباره موجود شد.\n\n"
     "{quota} · {days} روز · {price} تومان\n\n"
     "فروشگاه: {bot}\n"
     "اول تست رایگان، بعد تصمیم. 🎁"),

    ("تخفیف ۱ - کد (کد تخفیف لازم دارد)",
     "🎟 کد تخفیف: {code}\n\n"
     "تا {code_expires} معتبر است.\n"
     "موقع خرید در فروشگاه مینی‌اپ واردش کن.\n\n"
     "{bot}"),

    ("تخفیف ۲ - کد + تست (کد تخفیف لازم دارد)",
     "🎟 {code}\n\n"
     "یک کد، تا {code_expires}.\n"
     "فروشگاه را باز کن، پلن را انتخاب کن، کد را بزن.\n\n"
     "و اگر مشتری جدیدی، 🎁 تست رایگان هم سر جایش است.\n\n"
     "{bot}"),

    ("کوتاه ۱", "🎁 تست رایگان + 📱 فروشگاه داخل تلگرام\n\n{bot}"),

    ("کوتاه ۲", "اول تست کن. بعد بخر. هر دو در یک جا.\n\n📱 {bot}"),

    ("کوتاه ۳",
     "بدون سایت. بدون اپ. بدون دردسر.\n"
     "فروشگاه ما داخل تلگرام است - و تست رایگان دارد. 🎁\n\n"
     "{bot}"),

    ("کوتاه ۴",
     "🎁 رایگان امتحانش کن.\n"
     "📱 داخل تلگرام بخرش.\n"
     "⚡️ همان لحظه وصل شو.\n\n"
     "{bot}"),

    ("زاویه ۱ - سؤال صادقانه",
     "یک سؤال صادقانه:\n\n"
     "چند بار سرویس خریدی که روز سوم قطع شد و پشتیبانی جواب نداد؟\n\n"
     "ما به‌جای قول دادن، تست رایگان می‌دهیم. 🎁\n"
     "امتحان کن، اگر خوب بود بمان.\n\n"
     "📱 {bot}"),

    ("زاویه ۲ - برای دوستت بفرست",
     "🎁 برای کسانی که هنوز از ما نخریده‌اند\n\n"
     "تست رایگان فقط یک‌بار و فقط برای مشتری جدید است.\n"
     "اگر قبلاً از ما خرید کرده‌ای، این پیام برای دوستت است - برایش بفرست.\n\n"
     "📱 {bot}"),

    ("زاویه ۳ - دعوت دوستان",
     "👥 دوستت را دعوت کن، هر دو سود می‌کنید.\n\n"
     "کد دعوتت را از فروشگاه بردار: {bot} ← فروشگاه\n"
     "او تخفیف می‌گیرد، تو اعتبار.\n\n"
     "🎁 و اگر تازه‌وارد است، اول تست رایگان را بگیرد."),

    ("زاویه ۴ - دسته‌بندی پلن‌ها",
     "📱 فروشگاه باز است، همین الان.\n\n"
     "پلن‌ها دسته‌بندی شده‌اند: تک‌کاربره، خانوادگی، و مخصوص روزهای سخت.\n"
     "هر کدام را بزنی، مشخصاتش را می‌بینی.\n\n"
     "🎁 تست رایگان هم بالای صفحه است.\n\n"
     "{bot}"),
]

TRIAL_BUTTON = "🎁 دریافت تست رایگان"
BUY_BUTTON = "🛒 خرید و اطلاعات بیشتر"


def _install(db, channel, replace: bool) -> tuple[int, int]:
    """Puts the adverts on one channel. Returns (deleted, added)."""
    deleted = 0
    if replace:
        deleted = db.query(models.AdPost).filter(
            models.AdPost.channel_id == channel.id).delete(synchronize_session=False)
    base = 0 if replace else (
        db.query(models.AdPost).filter(models.AdPost.channel_id == channel.id).count()
    )
    for index, (title, body) in enumerate(ADS):
        db.add(models.AdPost(
            channel_id=channel.id,
            title=title,
            body=body,
            button_text=TRIAL_BUTTON if "تست رایگان" in title or "کوتاه" in title else BUY_BUTTON,
            enabled=True,
            sort_order=base + index,
        ))
    return deleted, len(ADS)


def _apply_to_all(db, *, replace: bool, dry_run: bool) -> int:
    """Every channel on the panel, including the resellers' own.

    Prints the full account of what it will do BEFORE doing any of it, and
    per channel rather than as one total. This wipes other people's
    advertising - a reseller who wrote their own posts loses them - so the
    one thing this must never be is quiet about its scope.
    """
    channels = db.query(models.AdChannel).order_by(models.AdChannel.owner_admin_id).all()
    if not channels:
        print("هیچ کانال تبلیغاتی وجود ندارد.")
        return 1

    print(f"{len(channels)} کانال پیدا شد:\n")
    total_existing = 0
    for channel in channels:
        admin = db.get(models.AdminUser, channel.owner_admin_id)
        count = db.query(models.AdPost).filter(
            models.AdPost.channel_id == channel.id).count()
        total_existing += count
        print(f"  ادمین {channel.owner_admin_id:<4} {(admin.username if admin else '?'):<16} "
              f"{count} تبلیغ فعلی")

    print()
    if replace:
        print(f"⚠️  مجموعاً {total_existing} تبلیغ موجود حذف می‌شود - شامل تبلیغ‌هایی که")
        print("   نماینده‌ها خودشان نوشته‌اند - و برای هر کانال "
              f"{len(ADS)} تبلیغ جدید گذاشته می‌شود.")
    else:
        print(f"{len(ADS)} تبلیغ جدید به هر کانال اضافه می‌شود؛ تبلیغ‌های فعلی می‌مانند.")
        print("برای پاک کردن قبلی‌ها --replace را اضافه کن.")

    if dry_run:
        print("\n--dry-run: چیزی نوشته نشد.")
        return 0

    deleted_total = added_total = 0
    for channel in channels:
        deleted, added = _install(db, channel, replace)
        deleted_total += deleted
        added_total += added
    db.commit()

    print(f"\n✅ {deleted_total} تبلیغ حذف شد، {added_total} تبلیغ روی {len(channels)} کانال اضافه شد.")
    print("تبلیغ‌هایی که به پکیج یا کد تخفیف نیاز دارند در عنوانشان نوشته شده -")
    print("هر ادمین باید در پنل خودش برایشان یکی انتخاب کند، وگرنه به‌جای قیمت")
    print("خط تیره فرستاده می‌شود.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--admin", type=int,
                        help="ادمین صاحب کانال تبلیغات (شناسه‌ی عددی)")
    parser.add_argument("--all", action="store_true",
                        help="روی همه‌ی کانال‌ها اعمال شود، نه فقط یکی")
    parser.add_argument("--replace", action="store_true",
                        help="تبلیغ‌های فعلی این کانال حذف و با این‌ها جایگزین شوند")
    parser.add_argument("--dry-run", action="store_true",
                        help="فقط نشان بده چه می‌شود، چیزی ننویس")
    parser.add_argument("--list", action="store_true",
                        help="کانال‌های موجود را فهرست کن و خارج شو")
    args = parser.parse_args()

    if args.all and args.admin:
        print("یا --all یا --admin، نه هر دو.")
        return 1

    db = SessionLocal()
    try:
        if args.all:
            return _apply_to_all(db, replace=args.replace, dry_run=args.dry_run)

        if args.list or not args.admin:
            channels = db.query(models.AdChannel).all()
            if not channels:
                print("هیچ کانال تبلیغاتی ساخته نشده. اول از صفحه‌ی «تبلیغات» پنل یک کانال بساز.")
                return 1
            print("کانال‌های تبلیغاتی:\n")
            for channel in channels:
                admin = db.get(models.AdminUser, channel.owner_admin_id)
                posts = db.query(models.AdPost).filter(
                    models.AdPost.channel_id == channel.id).count()
                print(f"  --admin {channel.owner_admin_id:<4} "
                      f"{(admin.username if admin else '?'):<16} "
                      f"chat_id={channel.chat_id or '-':<20} "
                      f"{posts} تبلیغ")
            if not args.admin:
                print("\nیکی را با --admin انتخاب کن، یا --all برای همه.")
            return 0

        channel = (
            db.query(models.AdChannel)
            .filter(models.AdChannel.owner_admin_id == args.admin)
            .first()
        )
        if not channel:
            print(f"برای ادمین {args.admin} کانال تبلیغاتی وجود ندارد.")
            print("اول از صفحه‌ی «تبلیغات» پنل کانالش را بساز، بعد دوباره اجرا کن.")
            return 1

        existing = db.query(models.AdPost).filter(
            models.AdPost.channel_id == channel.id).count()

        print(f"کانال ادمین {args.admin} - الان {existing} تبلیغ دارد.")
        if args.replace:
            print(f"با --replace: آن {existing} تا حذف و {len(ADS)} تای جدید جایگزین می‌شود.")
        else:
            print(f"{len(ADS)} تبلیغ جدید به آن‌ها اضافه می‌شود (مجموع {existing + len(ADS)}).")
            print("اگر می‌خواهی قبلی‌ها پاک شوند، --replace را اضافه کن.")

        if args.dry_run:
            print("\n--dry-run: چیزی نوشته نشد. عنوان‌هایی که ساخته می‌شدند:\n")
            for index, (title, _) in enumerate(ADS, 1):
                print(f"  {index:>2}. {title}")
            return 0

        deleted, added = _install(db, channel, args.replace)
        if deleted:
            print(f"{deleted} تبلیغ قبلی حذف شد.")
        db.commit()
        print(f"\n✅ {len(ADS)} تبلیغ اضافه شد.")
        print("در پنل، بخش «تبلیغات» را باز کن: آن‌هایی که به پکیج یا کد تخفیف")
        print("نیاز دارند در عنوانشان نوشته شده - برایشان یکی انتخاب کن،")
        print("وگرنه به‌جای قیمت خط تیره فرستاده می‌شود.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
