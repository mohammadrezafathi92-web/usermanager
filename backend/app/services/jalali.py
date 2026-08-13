"""Gregorian -> Jalali (Persian solar / Solar Hijri) date formatting.

The conversion used to live inside telegram_bot/utils.py, which meant only the
Telegram bot could reach it - so every other user-facing date in the project
(quota/expiry notifications, backup captions, the Excel exports) silently kept
printing Gregorian dates at a Persian-speaking audience. It lives here now so
there is exactly one implementation; telegram_bot/utils.py re-exports it under
its original names so existing bot code is unaffected.

Time zones: the whole project STORES and computes in UTC (`datetime.utcnow()`
throughout) and that does not change - only rendering shifts, by the offset in
PanelSettings.display_utc_offset_minutes (Tehran = 210). Before this existed,
anything timestamped between 20:30 and 24:00 UTC printed the PREVIOUS Persian
day to a reader in Tehran.

The offset is cached in a module global rather than looked up per call: these
formatters run inside bot message rendering and row-by-row spreadsheet export,
where a database round-trip per date would be absurd. main.py primes it at
startup and routers/panel_settings.py refreshes it whenever the setting is
saved - see set_display_offset below.
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


DEFAULT_UTC_OFFSET_MINUTES = 210  # Asia/Tehran, +03:30, no DST since 2022
_display_offset_minutes = DEFAULT_UTC_OFFSET_MINUTES


def set_display_offset(minutes: int | None) -> None:
    """Called once at startup and again whenever the panel setting changes."""
    global _display_offset_minutes
    if minutes is None:
        return
    try:
        _display_offset_minutes = int(minutes)
    except (TypeError, ValueError):
        pass


def get_display_offset() -> int:
    return _display_offset_minutes


def _coerce(value):
    """Accepts a datetime, a date, or an ISO string (with or without a
    trailing Z), and returns it shifted into the display timezone. Returns
    None if it cannot be understood, so callers can fall back rather than
    raise on a stray value from an old row.

    A naive datetime is assumed to be UTC, because that is what every writer
    in this project produces. A value that already carries a timezone is
    converted properly rather than blindly shifted."""
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        parsed = value
    elif isinstance(value, dt.date):
        # A bare date has no time to shift - moving it would change the day.
        return dt.datetime(value.year, value.month, value.day)
    elif isinstance(value, str):
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None

    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return parsed + dt.timedelta(minutes=_display_offset_minutes)


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
