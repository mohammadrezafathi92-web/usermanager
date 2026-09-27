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


def jalali_to_gregorian(jy: int, jm: int, jd: int) -> dt.date:
    """The inverse of gregorian_to_jalali - finds the Gregorian date for a
    given Jalali (jy, jm, jd).

    Rather than hand-deriving the exact algebraic inverse of that function's
    33-year-cycle arithmetic (easy to get subtly wrong right at a leap-year
    edge - precisely where a month-boundary bug would hide), this seeds a
    close Gregorian guess and walks it to the exact day using
    gregorian_to_jalali ITSELF as the source of truth. That guarantees the
    two functions can never disagree with each other, which matters more
    here than shaving off a few iterations: jalali_month_bounds() below
    exists specifically to fix a month figure that silently used the wrong
    calendar (see routers/dashboard.py's sales_month) - an inverse that
    could drift from the forward conversion would just move the same class
    of bug to a different day of the year instead of fixing it."""
    guess = dt.date(jy + 621, 3, 20) + dt.timedelta(days=(jm - 1) * 31 + (jd - 1))
    for _ in range(400):  # generous bound; real corrections are a handful of days
        gy2, gm2, gd2 = gregorian_to_jalali(guess.year, guess.month, guess.day)
        if (gy2, gm2, gd2) == (jy, jm, jd):
            return guess
        guess += dt.timedelta(days=1 if (gy2, gm2, gd2) < (jy, jm, jd) else -1)
    raise ValueError(f"could not resolve Jalali date {jy}-{jm}-{jd} to a Gregorian one")


def jalali_month_bounds(local_dt: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    """(this Jalali month's start, previous Jalali month's start), as naive
    local-midnight datetimes - given `local_dt` already shifted into the
    display timezone (see _coerce's convention).

    Exists because "this month"/"previous month" figures (routers/
    dashboard.py's sales_month/sales_prev_month) used to reset on the 1st
    of the GREGORIAN month (plain datetime.replace(day=1)) in a panel whose
    every other date is Jalali - so on Mehr 1 (~Sep 23, a new Persian month)
    "این ماه" kept showing Shahrivar's total, and only reset days later on
    Oct 1. Reported as "الان که رفتیم توی مهر ماه ریست نشده"."""
    jy, jm, jd = gregorian_to_jalali(local_dt.year, local_dt.month, local_dt.day)
    this_month_start = jalali_to_gregorian(jy, jm, 1)
    prev_jy, prev_jm = (jy - 1, 12) if jm == 1 else (jy, jm - 1)
    prev_month_start = jalali_to_gregorian(prev_jy, prev_jm, 1)
    return (
        dt.datetime(this_month_start.year, this_month_start.month, this_month_start.day),
        dt.datetime(prev_month_start.year, prev_month_start.month, prev_month_start.day),
    )
