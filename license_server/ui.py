"""HTML building blocks for the console: escaping, Persian dates and digits,
the page shell. No template engine - every value passes through esc()."""
from __future__ import annotations

import datetime as dt
from typing import Optional

from fastapi.responses import HTMLResponse

# Iran has had no daylight saving since 2022.
TEHRAN = dt.timezone(dt.timedelta(hours=3, minutes=30))
_FA = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def esc(text) -> str:
    if text is None or text == "":
        return ""
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#39;"))


def fa(value) -> str:
    return esc(value).translate(_FA)


def _g2j(gy: int, gm: int, gd: int) -> tuple[int, int, int]:
    g_d_m = (0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334)
    gy2 = gy + 1 if gm > 2 else gy
    days = (355666 + 365 * gy + (gy2 + 3) // 4 - (gy2 + 99) // 100 + (gy2 + 399) // 400
            + gd + g_d_m[gm - 1])
    jy = -1595 + 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        return jy, 1 + days // 31, 1 + days % 31
    return jy, 7 + (days - 186) // 30, 1 + (days - 186) % 30


def local(moment: dt.datetime) -> dt.datetime:
    """Stored times are naive UTC."""
    return moment.replace(tzinfo=dt.timezone.utc).astimezone(TEHRAN)


def jdate(moment: Optional[dt.datetime], with_time: bool = True) -> str:
    if moment is None:
        return ""
    t = local(moment)
    y, m, d = _g2j(t.year, t.month, t.day)
    text = f"{y}/{m:02d}/{d:02d}"
    if with_time:
        text += f" ساعت {t.hour:02d}:{t.minute:02d}"
    return fa(text)


def ago(delta: dt.timedelta) -> str:
    secs = int(delta.total_seconds())
    if secs < 60:
        return "چند لحظه پیش"
    if secs < 3600:
        return fa(f"{secs // 60} دقیقه پیش")
    if secs < 86400:
        return fa(f"{secs // 3600} ساعت پیش")
    return fa(f"{secs // 86400} روز پیش")


def badge(text: str, tone: str = "muted") -> str:
    return f'<span class="badge {tone}">{text}</span>'


def code(text) -> str:
    return f'<code dir="ltr">{esc(text)}</code>'


def csrf_field(csrf: str) -> str:
    return f'<input type="hidden" name="csrf" value="{esc(csrf)}">'


def notice(text: str, tone: str = "info") -> str:
    return f'<div class="notice {tone}" role="status">{text}</div>'


def page(title: str, body: str, *, csrf: Optional[str] = None, active: str = "",
         status_code: int = 200) -> HTMLResponse:
    nav = ""
    if csrf is not None:
        links = (("list", "/console", "فهرست لایسنس‌ها"), ("issue", "/console/issue", "صدور لایسنس"),
                 ("events", "/console/events", "رویدادها"), ("password", "/console/password", "رمز کنسول"))
        items = "".join(
            f'<a href="{href}"{" aria-current=page" if key == active else ""}>{label}</a>'
            for key, href, label in links)
        nav = f"""<header class="top"><div class="wrap top-in">
          <a class="brand" href="/console">کنسول لایسنس</a>
          <nav>{items}</nav>
          <form method="post" action="/console/logout" class="logout">{csrf_field(csrf)}
            <button class="btn ghost small">خروج</button></form>
        </div></header>"""
    return HTMLResponse(f"""<!doctype html><html lang="fa" dir="rtl"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="same-origin"><title>{esc(title)} · کنسول لایسنس</title>
<link rel="icon" href="data:,"><link rel="stylesheet" href="/console/static/console.css">
<script src="/console/static/console.js" defer></script></head>
<body>{nav}<main class="wrap">{body}</main></body></html>""", status_code=status_code)


SCOPE_LABELS = {
    "panel_only": "فقط پنل",
    "panel_and_bot": "پنل و ربات",
    "everything": "همه",
}
SCOPE_EFFECTS = {
    "panel_only": "ورود به پنل مدیریت بسته می‌شود. ربات فروش و VPN کاربران نهایی کار می‌کنند.",
    "panel_and_bot": "پنل مدیریت و فروش ربات بسته می‌شوند. VPN کاربران نهایی کار می‌کند.",
    "everything": "پنل، ربات و اتصال VPN کاربران نهایی قطع می‌شوند. پس از بازکردن یا تمدید، "
                  "فقط سرویس‌های واجد شرایط خودکار برمی‌گردند.",
}
