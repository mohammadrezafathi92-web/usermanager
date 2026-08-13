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
