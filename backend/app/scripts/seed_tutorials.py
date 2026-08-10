"""Seeds the "📚 آموزش" section with a ready-made set of connection guides
(V2Ray / OpenVPN / WireGuard / L2TP + an FAQ), each with its app download
link attached - see docs/tutorials-content.md for the same content in
copy-paste form.

Run it (from the project directory on the server):

    docker compose exec backend python -m app.scripts.seed_tutorials

Safe to re-run: a tutorial whose title already exists is skipped, so this
never duplicates or overwrites anything you've since edited by hand. Pass
--owner-admin-id N to seed into one specific level-2 Admin's own tutorial
list instead of the superadmin's (see models.Tutorial.owner_admin_id);
pass --replace to delete and re-create the seeded titles instead of
skipping them.

Photos are deliberately NOT seeded - real screenshots of the actual app
versions your customers install are far more useful than stock images, and
those you upload yourself from the panel's آموزش page.
"""
from __future__ import annotations

import argparse

from ..database import SessionLocal
from .. import models

BRAND = "NetCip"


TUTORIALS: list[dict] = [
    {
        "title": "نصب و اتصال V2Ray روی اندروید",
        "software": [("V2rayNG (اندروید)", "https://github.com/2dust/v2rayNG/releases")],
        "text": f"""📱 اتصال V2Ray روی گوشی اندروید — {BRAND}

۱) اپلیکیشن V2rayNG را نصب کنید (لینک دانلود پایین همین صفحه).

۲) از ربات، «اکانت من» را باز کنید و روی سرویس V2Ray/Xray بزنید. یک لینک که با vless:// شروع می‌شود و یک QR برایتان می‌آید.

۳) وارد V2rayNG شوید و روی + گوشه‌ی بالا-راست بزنید:
   • اگر لینک را کپی کرده‌اید: «Import config from Clipboard»
   • اگر QR دارید: «Scan QR code»

۴) کانفیگ در لیست ظاهر می‌شود. یک بار روی آن بزنید تا انتخاب شود.

۵) دکمه‌ی گرد پایین صفحه (V) را بزنید. اولین بار اندروید اجازه‌ی VPN می‌خواهد؛ «OK» را بزنید.

۶) وقتی دکمه سبز شد یعنی متصل هستید. برای تست، در مرورگر یک سایت خارجی باز کنید.

❌ قطع اتصال: دوباره همان دکمه‌ی گرد را بزنید.

💡 نکته: اگر چند سرویس دارید، از منوی «تست سرعت» داخل V2rayNG می‌توانید بهترین را انتخاب کنید.""",
    },
    {
        "title": "نصب و اتصال V2Ray روی آیفون",
        "software": [("Streisand (آیفون)", "https://apps.apple.com/app/streisand/id6450534064")],
        "text": f"""🍎 اتصال V2Ray روی آیفون و آیپد — {BRAND}

۱) از App Store یکی از این‌ها را نصب کنید: Streisand یا V2Box یا Shadowrocket.
   (اگر اپ در استور ایران دیده نمی‌شود، باید اکانت App Store با کشور دیگری بسازید.)

۲) از ربات، «اکانت من» → سرویس V2Ray/Xray را بزنید و لینک vless:// را کپی کنید.

۳) وارد اپلیکیشن شوید و + یا Add را بزنید، سپس «Import from clipboard» یا «Paste from Clipboard» را انتخاب کنید.

۴) کانفیگ اضافه می‌شود. روی آن بزنید تا تیک بخورد.

۵) کلید اتصال بالای صفحه را روشن کنید. اولین بار پیام «Allow / اجازه» می‌آید؛ تایید کنید و رمز یا Face ID گوشی را وارد کنید.

۶) سبز شدن وضعیت یعنی متصل هستید.

💡 لینک اشتراک (Subscription): اگر چند سرویس V2Ray دارید، به‌جای وارد کردن تک‌تک، از «اکانت من» لینک اشتراک را بگیرید و در اپ از بخش Subscription اضافه کنید؛ سرویس‌های جدیدتان خودکار اضافه می‌شوند.""",
    },
    {
        "title": "نصب و اتصال V2Ray روی ویندوز",
        "software": [("v2rayN (ویندوز)", "https://github.com/2dust/v2rayN/releases")],
        "text": f"""💻 اتصال V2Ray روی ویندوز — {BRAND}

۱) فایل v2rayN را دانلود و از حالت فشرده خارج کنید (لینک پایین صفحه). آنتی‌ویروس ممکن است هشدار بدهد؛ این طبیعی است.

۲) فایل v2rayN.exe را اجرا کنید. آیکون برنامه در گوشه‌ی پایین-راست ویندوز (کنار ساعت) ظاهر می‌شود.

۳) لینک vless:// خود را از ربات کپی کنید.

۴) در برنامه کلیدهای Ctrl + V را بزنید (یا از منوی سرورها → «افزودن از کلیپ‌بورد»). کانفیگ به لیست اضافه می‌شود.

۵) روی کانفیگ کلیک کنید و کلید Enter را بزنید تا فعال شود.

۶) روی آیکون برنامه کنار ساعت راست‌کلیک کنید → System Proxy → «Set system proxy» را انتخاب کنید.

۷) حالا مرورگر شما از VPN عبور می‌کند.

❌ قطع: همان منو → System Proxy → Clear system proxy.""",
    },
    {
        "title": "نصب و اتصال OpenVPN روی اندروید",
        "software": [("OpenVPN Connect (اندروید)", "https://play.google.com/store/apps/details?id=net.openvpn.openvpn")],
        "text": f"""🛡 اتصال OpenVPN روی اندروید — {BRAND}

۱) اپلیکیشن «OpenVPN Connect» را نصب کنید (لینک پایین صفحه).

۲) از ربات، «اکانت من» → سرویس OpenVPN را بزنید. یک فایل با پسوند ovpn. برایتان ارسال می‌شود.

۳) روی همان فایل در تلگرام بزنید و «Share / اشتراک‌گذاری» → OpenVPN Connect را انتخاب کنید.
   (یا اول فایل را ذخیره کنید، بعد در اپ از بخش FILE آن را باز کنید.)

۴) دکمه‌ی IMPORT و سپس ADD را بزنید.

۵) کلید کنار نام سرویس را روشن کنید. اولین بار اجازه‌ی VPN را تایید کنید.

۶) وقتی رنگ صفحه سبز شد و زمان اتصال شروع به شمردن کرد، وصل هستید.

💡 نام کاربری و رمز عبور داخل خود فایل قرار دارد و نیازی به وارد کردن دستی نیست.""",
    },
    {
        "title": "نصب و اتصال OpenVPN روی آیفون",
        "software": [("OpenVPN Connect (آیفون)", "https://apps.apple.com/app/openvpn-connect/id590379981")],
        "text": f"""🍎 اتصال OpenVPN روی آیفون — {BRAND}

۱) اپلیکیشن «OpenVPN Connect» را از App Store نصب کنید.

۲) از ربات، سرویس OpenVPN را باز کنید و فایل ovpn. را دریافت کنید.

۳) روی فایل در تلگرام بزنید → آیکون اشتراک‌گذاری → «Copy to OpenVPN» را انتخاب کنید.

۴) در اپ، ADD و سپس ALLOW را بزنید تا پروفایل VPN اضافه شود.

۵) کلید کنار پروفایل را روشن کنید و رمز یا Face ID را تایید کنید.

۶) نمایش CONNECTED یعنی اتصال برقرار است.""",
    },
    {
        "title": "نصب و اتصال OpenVPN روی ویندوز",
        "software": [("OpenVPN GUI (ویندوز)", "https://openvpn.net/community-downloads/")],
        "text": f"""💻 اتصال OpenVPN روی ویندوز — {BRAND}

۱) نرم‌افزار OpenVPN GUI را نصب کنید (لینک پایین صفحه) و یک بار سیستم را ری‌استارت کنید.

۲) فایل ovpn. را از ربات دریافت و ذخیره کنید.

۳) روی آیکون OpenVPN کنار ساعت راست‌کلیک کنید → Import → Import file و فایل را انتخاب کنید.

۴) دوباره راست‌کلیک → Connect.

۵) وقتی آیکون سبز شد، متصل هستید.

⚠️ اگر پیام دسترسی ادمین آمد، برنامه را با «Run as administrator» اجرا کنید.""",
    },
    {
        "title": "نصب و اتصال WireGuard",
        "software": [("WireGuard (همه سیستم‌عامل‌ها)", "https://www.wireguard.com/install/")],
        "text": f"""🔒 اتصال WireGuard — {BRAND}

۱) اپلیکیشن WireGuard را نصب کنید (لینک پایین صفحه — برای اندروید، آیفون و ویندوز موجود است).

۲) از ربات، «اکانت من» → سرویس WireGuard را بزنید. یک فایل conf. و یک QR برایتان می‌آید.

۳) در اپ روی + بزنید:
   • ساده‌ترین راه در گوشی: «Scan from QR code» و اسکن همان QR
   • یا: «Import from file» و انتخاب فایل conf.

۴) نام دلخواهی بگذارید و ذخیره کنید.

۵) کلید کنار تونل را روشن کنید و اجازه‌ی VPN را تایید کنید.

۶) شروع شمردن اعداد ارسال و دریافت یعنی اتصال برقرار است.

💡 WireGuard سریع‌ترین گزینه‌ی ماست و مصرف باتری کمتری دارد.""",
    },
    {
        "title": "اتصال L2TP روی اندروید (بدون نصب برنامه)",
        "software": [],
        "text": f"""🌐 اتصال L2TP روی اندروید — بدون نصب هیچ برنامه‌ای — {BRAND}

۱) از ربات، «اکانت من» → سرویس L2TP/IPsec را بزنید و این چهار مورد را یادداشت کنید:
   آدرس سرور، نام کاربری، رمز عبور، کلید IPsec (PSK)

۲) به تنظیمات گوشی بروید:
   Settings → Network & internet → VPN → +

۳) اطلاعات را وارد کنید:
   • Name: یک نام دلخواه
   • Type: L2TP/IPSec PSK
   • Server address: آدرس سرور
   • IPSec pre-shared key: کلید IPsec
   • Username و Password: همان‌ها که در ربات آمده

۴) ذخیره کنید و روی نام آن بزنید → Connect.

⚠️ در برخی گوشی‌های جدید (اندروید ۱۲ به بالا) گزینه‌ی L2TP حذف شده است. در آن صورت از WireGuard یا OpenVPN استفاده کنید.""",
    },
    {
        "title": "اتصال L2TP روی ویندوز (بدون نصب برنامه)",
        "software": [],
        "text": f"""💻 اتصال L2TP روی ویندوز — بدون نصب هیچ برنامه‌ای — {BRAND}

۱) اطلاعات سرویس L2TP را از ربات بگیرید (آدرس سرور، نام کاربری، رمز، کلید IPsec).

۲) Settings → Network & Internet → VPN → Add VPN

۳) این‌طور پر کنید:
   • VPN provider: Windows (built-in)
   • Connection name: نام دلخواه
   • Server name or address: آدرس سرور
   • VPN type: L2TP/IPsec with pre-shared key
   • Pre-shared key: کلید IPsec
   • Type of sign-in info: User name and password
   • User name / Password: همان‌ها که در ربات آمده

۴) Save و سپس Connect.

⚠️ اگر خطای 809 گرفتید: این تنظیم ویندوز را انجام دهید و ری‌استارت کنید —
   کلید ویندوز + R → regedit →
   HKEY_LOCAL_MACHINE\\SYSTEM\\CurrentControlSet\\Services\\PolicyAgent
   یک مقدار جدید از نوع DWORD با نام AssumeUDPEncapsulationContextOnSendRule و مقدار 2 بسازید.""",
    },
    {
        "title": "سوالات پرتکرار و رفع اشکال",
        "software": [],
        "text": f"""❓ سوالات پرتکرار — {BRAND}

🔸 وصل می‌شوم ولی اینترنت ندارم
یک بار قطع و وصل کنید. اگر باز هم نشد، سرویس دیگری از «اکانت من» را امتحان کنید (مثلاً به‌جای V2Ray از WireGuard).

🔸 اصلاً وصل نمی‌شود
اول اینترنت خودتان را بدون VPN چک کنید. بعد مطمئن شوید حجم و اعتبار سرویس تمام نشده باشد — از «مصرف سرویس‌ها» ببینید.

🔸 حجمم تمام شده
از «تمدید سرویس» همان سرویس را تمدید کنید. تمدید روی همان کانفیگ قبلی اعمال می‌شود و نیازی به کانفیگ جدید ندارید.

🔸 چند دستگاه می‌توانم وصل کنم؟
بستگی به پکیج شما دارد. اگر بیشتر از حد مجاز وصل شوید، اتصال‌ها قطع می‌شوند.

🔸 کانفیگم را پاک کردم
از «اکانت من» دوباره همان سرویس را باز کنید؛ کانفیگ مجدداً برایتان ارسال می‌شود.

🔸 سرعتم کم است
سرویس‌های مختلف را امتحان کنید. WireGuard معمولاً سریع‌ترین است. ساعات شلوغی شب هم روی سرعت اثر دارد.

🔸 مشکلم حل نشد
از منوی «پشتیبانی» پیام بدهید و بنویسید: نام سرویس، سیستم‌عامل، و متن دقیق خطا.""",
    },
]


def seed(owner_admin_id: int | None = None, replace: bool = False) -> None:
    db = SessionLocal()
    created = skipped = 0
    try:
        for i, item in enumerate(TUTORIALS):
            existing = (
                db.query(models.Tutorial)
                .filter(
                    models.Tutorial.title == item["title"],
                    models.Tutorial.owner_admin_id.is_(None)
                    if owner_admin_id is None
                    else models.Tutorial.owner_admin_id == owner_admin_id,
                )
                .first()
            )
            if existing:
                if not replace:
                    skipped += 1
                    print(f"– رد شد (از قبل هست): {item['title']}")
                    continue
                db.delete(existing)
                db.flush()

            tutorial = models.Tutorial(
                title=item["title"],
                text=item["text"],
                enabled=True,
                sort_order=i,
                owner_admin_id=owner_admin_id,
            )
            db.add(tutorial)
            db.flush()  # assigns tutorial.id for the software rows below
            for j, (name, url) in enumerate(item["software"]):
                db.add(models.TutorialSoftware(
                    tutorial_id=tutorial.id, name=name, url=url, sort_order=j,
                ))
            created += 1
            print(f"✓ اضافه شد: {item['title']}")
        db.commit()
    finally:
        db.close()
    print(f"\nتمام شد — {created} آموزش اضافه شد، {skipped} مورد رد شد.")
    print("عکس‌ها را از پنل، بخش «آموزش‌ها»، روی هر آموزش آپلود کنید.")


def main() -> None:
    parser = argparse.ArgumentParser(description="افزودن آموزش‌های آماده به بخش آموزش")
    parser.add_argument(
        "--owner-admin-id", type=int, default=None,
        help="شناسه ادمین سطح ۲ که آموزش‌ها به لیست او اضافه شود (پیش‌فرض: لیست ادمین اصلی)",
    )
    parser.add_argument(
        "--replace", action="store_true",
        help="آموزش‌های هم‌نام موجود را حذف و دوباره بساز (پیش‌فرض: رد کردن)",
    )
    args = parser.parse_args()
    seed(owner_admin_id=args.owner_admin_id, replace=args.replace)


if __name__ == "__main__":
    main()
