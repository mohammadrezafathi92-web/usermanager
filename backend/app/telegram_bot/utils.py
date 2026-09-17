import datetime as dt

STATUS_LABELS = {
    "active": "🟢 فعال",
    "disabled": "🔴 غیرفعال",
    "quota_exceeded": "🟠 اتمام حجم",
    "expired": "⚫️ منقضی",
}


def fmt_bytes(n: int | None) -> str:
    if not n:
        return "0 B"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


# The conversion itself now lives in services/jalali.py so the rest of the
# backend (notifications, backup captions, Excel exports) can use it too - it
# was previously reachable only from here, which is exactly why those places
# were still printing Gregorian dates.
from ..services.jalali import fmt_jalali, gregorian_to_jalali as _gregorian_to_jalali  # noqa: F401


def fmt_date(value) -> str:
    """Kept as an alias of the Jalali formatter rather than deleted: this is a
    Persian-facing bot, so there is no case where a Gregorian date is the
    right answer, and leaving a Gregorian-formatting function named `fmt_date`
    around is an invitation to reintroduce one."""
    return fmt_jalali(value)


def fmt_date_jalali(value, with_time: bool = True) -> str:
    return fmt_jalali(value, with_time=with_time)


def fa_digits(value) -> str:
    """Persian digits. Latin numerals next to Persian text read as a
    different typeface on most phones, which is the single biggest reason
    the bot's messages looked unfinished next to a competitor's."""
    return str(value).translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))


def fmt_toman(amount) -> str:
    amount = int(amount or 0)
    return "رایگان" if amount <= 0 else f"{fa_digits(format(amount, ','))} تومان"


def fmt_gb(gb) -> str:
    gb = float(gb or 0)
    if gb <= 0:
        return "نامحدود"
    if gb < 1:
        return f"{fa_digits(int(round(gb * 1024)))} مگابایت"
    text = f"{gb:g}"
    return f"{fa_digits(text)} گیگابایت"


def fmt_days(days) -> str:
    days = int(days or 0)
    return "بدون انقضا" if days <= 0 else f"{fa_digits(days)} روز"


def package_card(p: dict, index: int | None = None) -> str:
    """One package, rendered as a card rather than a line.

    Requested 2026-09-13 after comparing with a competitor's bot: "کارت پلن
    با آیکون و توضیح و لیست تخفیف‌ها، و پیام‌های مرتب‌تر". The picker used to
    be a bare "یک پکیج انتخاب کنید:" over a column of buttons reading
    "name · price" - so everything a customer needs to CHOOSE (how much
    volume, for how long, for how many devices, what it actually is) was
    either crammed into the package's name or not shown at all.

    Only the fields that are set are printed. A card padded with "مدت:
    نامحدود / توضیح: -" is noisier than a short one, and the quota and
    duration lines carry real meaning when absent anyway ("نامحدود",
    "بدون انقضا") - so those two are always shown, and the rest appear only
    when the admin filled them in.
    """
    lines = []
    head = (p.get("name") or "").strip() or "پکیج"
    if index is not None:
        head = f"{fa_digits(index)}. {head}"
    lines.append(f"📦 <b>{head}</b>")
    lines.append(f"💰 {fmt_toman(p.get('price'))}")
    lines.append(f"📊 حجم: {fmt_gb(p.get('quota_gb'))}")
    lines.append(f"⏳ مدت: {fmt_days(p.get('duration_days'))}")

    sessions = int(p.get("max_concurrent_sessions") or 0)
    if sessions > 0:
        lines.append(f"👥 {fa_digits(sessions)} کاربر همزمان")

    services = p.get("connections") or []
    if services:
        # What the customer actually receives. A package bundling WireGuard
        # + OpenVPN + Xray looked identical to a single-protocol one.
        names = []
        for c in services:
            label = PROTOCOL_NAMES.get(c.get("protocol"), c.get("protocol"))
            if label and label not in names:
                names.append(label)
        if names:
            lines.append("🔌 " + " · ".join(names))

    if p.get("one_time_per_user"):
        lines.append("🎁 فقط یک‌بار قابل خرید")

    description = (p.get("description") or "").strip()
    if description:
        lines.append("")
        lines.append(description)
    return "\n".join(lines)


PROTOCOL_NAMES = {
    "wireguard": "WireGuard",
    "openvpn": "OpenVPN",
    "l2tp": "L2TP",
    "ikev2": "IKEv2",
    "sstp": "SSTP",
    "pptp": "PPTP",
    "xray": "V2Ray",
}

CARD_SEPARATOR = "\n\n➖➖➖➖➖➖➖➖➖➖\n\n"


def packages_message(packages: list[dict], title: str = "🛍 <b>پلن‌های موجود</b>") -> str:
    """The cards, stacked, under one heading - the message the picker
    buttons hang below."""
    body = CARD_SEPARATOR.join(
        package_card(p, index=i) for i, p in enumerate(packages, start=1)
    )
    return f"{title}\n\n{body}"
