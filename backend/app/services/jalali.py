"""Gregorian -> Jalali (Persian solar / Solar Hijri) date formatting.

The conversion used to live inside telegram_bot/utils.py, which meant only the
Telegram bot could reach it - so every other user-facing date in the project
(quota/expiry notifications, backup captions, the Excel exports) silently kept
printing Gregorian dates at a Persian-speaking audience. It lives here now so
there is exactly one implementation; telegram_bot/utils.py re-exports it under
its original names so existing bot code is unaffected.

A note on time zones, deliberately not hidden: the whole project stores and
computes in UTC (`datetime.utcnow()` throughout). These helpers convert
whatever datetime they are handed, so a UTC timestamp between 20:30 and 24:00
UTC renders as the PREVIOUS Persian day relative to Tehran local time. Fixing
that properly means introducing a real timezone for display, which touches far
more than formatting - it is called out here rather than papered over.
"""
from __future__ import annotations

import datetime as dt

_G_DAYS_IN_MONTH = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
_J_DAYS_IN_MONTH = [31, 31, 31, 31, 31, 31, 30, 30, 30, 30, 30, 29]

JALALI_MONTHS = [
    "فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
    "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند",
]


def gregorian_to_jalali(gy: int, gm: int, gd: int) -> tuple[int, int, int]:
    """The standard algorithm also used by jalaali-js / jalaali-python.
    Implemented locally rather than adding a pip dependency for ~30 lines."""
    gy2 = gy - 1600
    gm2 = gm - 1
    gd2 = gd - 1
    g_day_no = 365 * gy2 + (gy2 + 3) // 4 - (gy2 + 99) // 100 + (gy2 + 399) // 400
    for i in range(gm2):
        g_day_no += _G_DAYS_IN_MONTH[i]
    if gm2 > 1 and ((gy % 4 == 0 and gy % 100 != 0) or gy % 400 == 0):
        g_day_no += 1
    g_day_no += gd2
    j_day_no = g_day_no - 79
    j_np = j_day_no // 12053
    j_day_no %= 12053
    jy = 979 + 33 * j_np + 4 * (j_day_no // 1461)
    j_day_no %= 1461
    if j_day_no >= 366:
        jy += (j_day_no - 1) // 365
        j_day_no = (j_day_no - 1) % 365
    # The `else` belongs to the `for`: it runs only when no break happened,
    # i.e. the date is in Esfand (month 12). The original version of this
    # function seeded `jd = j_day_no + 1` BEFORE the loop and let the
    # fall-through case keep that value - but by then the loop had not yet
    # subtracted the preceding months, so every Esfand date rendered as its
    # day-of-year: "1403/12/366" instead of "1403/12/30". That is roughly
    # Feb 20 - Mar 20 every year, in bot messages and expiry displays.
    for i in range(11):
        if j_day_no < _J_DAYS_IN_MONTH[i]:
            jm = i + 1
            jd = j_day_no + 1
            break
        j_day_no -= _J_DAYS_IN_MONTH[i]
    else:
        jm = 12
        jd = j_day_no + 1
    return jy, jm, jd


def _coerce(value):
    """Accepts a datetime, a date, or an ISO string (with or without a
    trailing Z). Returns None if it cannot be understood, so callers can fall
    back rather than raise on a stray value from an old row."""
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value
    if isinstance(value, dt.date):
        return dt.datetime(value.year, value.month, value.day)
    if isinstance(value, str):
        try:
            return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def fmt_jalali(value, with_time: bool = True, empty: str = "بدون انقضا") -> str:
    """`1405/05/22 14:30`. Falls back to str(value) if it isn't a date at all,
    so a bad value shows something rather than blowing up a bot message."""
    parsed = _coerce(value)
    if parsed is None:
        return empty if value in (None, "") else str(value)
    jy, jm, jd = gregorian_to_jalali(parsed.year, parsed.month, parsed.day)
    date_part = f"{jy:04d}/{jm:02d}/{jd:02d}"
    return f"{date_part} {parsed.strftime('%H:%M')}" if with_time else date_part


def fmt_jalali_long(value, with_time: bool = False, empty: str = "-") -> str:
    """`۲۲ مرداد ۱۴۰۵` - for captions and notifications, where a written-out
    month reads far better than slashes."""
    parsed = _coerce(value)
    if parsed is None:
        return empty if value in (None, "") else str(value)
    jy, jm, jd = gregorian_to_jalali(parsed.year, parsed.month, parsed.day)
    out = f"{jd} {JALALI_MONTHS[jm - 1]} {jy}"
    return f"{out} ساعت {parsed.strftime('%H:%M')}" if with_time else out
