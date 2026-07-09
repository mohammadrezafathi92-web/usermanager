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
