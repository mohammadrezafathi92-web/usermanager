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


def fmt_date(value) -> str:
    if not value:
        return "بدون انقضا"
    if isinstance(value, str):
        try:
            value = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    return value.strftime("%Y-%m-%d %H:%M")


def _gregorian_to_jalali(gy: int, gm: int, gd: int) -> tuple[int, int, int]:
    """Standard Gregorian->Jalali (Persian/Solar Hijri) calendar conversion -
    the same well-known algorithm used by jalaali-js/jalaali-python and
    similar libraries. Implemented locally (no extra pip dependency) since
    this is the only place in the project that needs it so far - see
    fmt_date_jalali below."""
    g_days_in_month = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    j_days_in_month = [31, 31, 31, 31, 31, 31, 30, 30, 30, 30, 30, 29]
    gy2 = gy - 1600
    gm2 = gm - 1
    gd2 = gd - 1
    g_day_no = 365 * gy2 + (gy2 + 3) // 4 - (gy2 + 99) // 100 + (gy2 + 399) // 400
    for i in range(gm2):
        g_day_no += g_days_in_month[i]
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
    jm = 12
    jd = j_day_no + 1
    for i in range(11):
        if j_day_no < j_days_in_month[i]:
            jm = i + 1
            jd = j_day_no + 1
            break
        j_day_no -= j_days_in_month[i]
    return jy, jm, jd


def fmt_date_jalali(value, with_time: bool = True) -> str:
    """Same input handling as fmt_date, but renders the date portion in the
    Jalali (Persian solar) calendar - used in customer-facing usage/expiry
    displays (see keyboards.py's standalone_usage_text/_account_text)."""
    if not value:
        return "بدون انقضا"
    if isinstance(value, str):
        try:
            value = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    jy, jm, jd = _gregorian_to_jalali(value.year, value.month, value.day)
    date_part = f"{jy:04d}/{jm:02d}/{jd:02d}"
    return f"{date_part} {value.strftime('%H:%M')}" if with_time else date_part
