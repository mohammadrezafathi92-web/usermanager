"""Console pages: login, list, details, access change, events, password.
Issuing and renewing live in issue_views.py."""
from __future__ import annotations

import datetime as dt
from urllib.parse import quote

import ledger
from ui import (SCOPE_LABELS, ago, badge, code, csrf_field, esc, fa, jdate,
                notice, page)

DONE = {
    "access": ("تغییر دسترسی ثبت شد. پنل در heartbeat بعدی آن را دریافت می‌کند.", "ok"),
    "nochange": ("تغییری لازم نبود؛ وضعیت از قبل همین بود.", "info"),
    "label": ("نام و یادداشت ذخیره شد.", "ok"),
    "imported": ("اطلاعات توکن با امضای همین سرور تأیید و ثبت شد.", "ok"),
    "forgotten": ("ردیف از فهرست برداشته شد. سابقهٔ صدور و رویدادها حفظ شده است.", "ok"),
    "password": ("رمز کنسول عوض شد. نشست‌های دیگر بسته شدند.", "ok"),
}

ACTIONS = {
    "login": "ورود", "login_failed": "ورود ناموفق", "logout": "خروج",
    "password_changed": "تغییر رمز کنسول", "access_changed": "تغییر دسترسی",
    "label_changed": "تغییر نام/یادداشت", "issued": "صدور لایسنس", "renewed": "صدور به‌عنوان تمدید",
    "renewal_issued": "صدور لایسنس جانشین",
    "imported": "ثبت اطلاعات توکن", "forgotten": "برداشتن از فهرست",
}


def link(license_id: str) -> str:
    return "/console/license?id=" + quote(license_id, safe="")


def done_notice(done: str) -> str:
    if done in DONE:
        text, tone = DONE[done]
        return notice(text, tone)
    return ""


# --------------------------------------------------------------------------
# the three facts, in words
# --------------------------------------------------------------------------
def validity_view(v: dict) -> tuple[str, str]:
    state = v["state"]
    if state == "unknown":
        return badge("نامشخص", "muted"), "اطلاعات صدور این لایسنس در این سرور ثبت نشده است."
    if state == "not_started":
        return (badge("شروع‌نشده", "info"),
                fa(f"{v['valid_days']} روز از زمان نصب؛ پنل هنوز تاریخ فعال‌سازی را گزارش نکرده است."))
    if state == "perpetual":
        return badge("بدون تاریخ انقضا", "ok"), "این لایسنس تاریخ پایان ندارد."
    if state == "expired":
        return badge("منقضی", "danger"), "پایان اعتبار: " + jdate(v["expires_at"])
    tone = "warn" if state == "expiring" else "ok"
    label = "رو به پایان" if state == "expiring" else "معتبر"
    return badge(label, tone), fa(f"{v['days_left']} روز مانده") + " · تا " + jdate(v["expires_at"])


def access_view(install) -> tuple[str, str]:
    scope = SCOPE_LABELS.get(install.lock_scope, install.lock_scope)
    if install.revoked:
        return badge("قفل", "danger"), "دامنه: " + esc(scope)
    return badge("باز", "ok"), "دامنه در صورت قفل: " + esc(scope)


def contact_view(c: dict, install) -> tuple[str, str]:
    state = c["state"]
    if state == "never":
        return badge("هنوز گزارش نداده", "muted"), "پنل با این لایسنس هنوز heartbeat نفرستاده است."
    tone, label = {"recent": ("ok", "در ارتباط"), "late": ("warn", "با تأخیر"),
                   "silent": ("danger", "بی‌ارتباط")}[state]
    ip = f" · {code(install.last_ip)}" if install.last_ip else ""
    return badge(label, tone), ago(c["silent"]) + ip


def delivery_view(d: dict) -> str:
    if d["state"] == "delivered":
        return "پنل وضعیت فعلی را دریافت کرده است (heartbeat پس از آخرین تغییر)."
    if d["state"] == "pending":
        return "آخرین تغییر هنوز به پنل نرسیده؛ در heartbeat بعدی اعمال می‌شود."
    return "زمان آخرین تغییر دسترسی ثبت نشده است؛ رسیدن آن به پنل نامشخص است."


# --------------------------------------------------------------------------
# login
# --------------------------------------------------------------------------
def login_page(error: str = ""):
    messages = {"wrong": "رمز اشتباه است.", "blocked": "تلاش ناموفق زیاد بود. چند دقیقه بعد دوباره امتحان کنید.",
                "expired": "نشست شما تمام شده است؛ دوباره وارد شوید.",
                "origin": "درخواست از این صفحه نیامده بود؛ دوباره امتحان کنید."}
    msg = notice(messages[error], "danger") if error in messages else ""
    return page("ورود", f"""
      <section class="login card">
        <h1>کنسول لایسنس</h1>
        <p class="muted">مدیریت لایسنس پنل‌های usermanager</p>
        {msg}
        <form method="post" action="/console/login">
          <label for="password">رمز عبور</label>
          <input id="password" name="password" type="password" autocomplete="current-password" required autofocus>
          <button class="btn primary block">ورود</button>
        </form>
      </section>""")


# --------------------------------------------------------------------------
# the list
# --------------------------------------------------------------------------
def _select(name: str, current: str, options) -> str:
    opts = "".join(f'<option value="{esc(v)}"{" selected" if v == current else ""}>{label}</option>'
                   for v, label in options)
    return f'<select name="{name}" aria-label="{name}">{opts}</select>'


def list_page(rows, all_counts, filters: dict, csrf: str, done: str = ""):
    tiles = [("total", "همه", ""), ("locked", "قفل‌شده", "access=locked"),
             ("attention", "منقضی یا رو به پایان", "valid=attention"),
             ("unknown", "اعتبار نامشخص", "valid=unknown"),
             ("silent", "بی‌ارتباط یا بدون گزارش", "contact=quiet"),
             ("moved", "تغییر اثر انگشت", "moved=1")]
    tile_html = "".join(
        f'<a class="tile" href="/console{"?" + q if q else ""}"><b>{fa(all_counts[k])}</b><span>{label}</span></a>'
        for k, label, q in tiles)
    filter_form = f"""
      <form class="filters" method="get" action="/console" role="search">
        <input type="search" name="q" value="{esc(filters.get('q'))}" placeholder="جست‌وجو: نام، شناسه، اثر انگشت، IP">
        {_select("access", filters.get("access", ""), (("", "دسترسی: همه"), ("open", "باز"), ("locked", "قفل")))}
        {_select("valid", filters.get("valid", ""), (("", "اعتبار: همه"), ("valid", "معتبر"),
                 ("attention", "منقضی/رو به پایان"), ("expired", "منقضی"), ("not_started", "شروع‌نشده"),
                 ("perpetual", "بدون انقضا"), ("unknown", "نامشخص")))}
        {_select("contact", filters.get("contact", ""), (("", "ارتباط: همه"), ("recent", "در ارتباط"),
                 ("late", "با تأخیر"), ("silent", "بی‌ارتباط"), ("never", "هنوز گزارش نداده")))}
        <button class="btn">اعمال</button>
        <a class="btn ghost" href="/console">پاک‌کردن</a>
      </form>"""
    body_rows = []
    for row in rows:
        install = row["install"]
        v_badge, v_text = validity_view(row["validity"])
        a_badge, a_text = access_view(install)
        c_badge, c_text = contact_view(row["contact"], install)
        flags = []
        if row["moved"]:
            flags.append(badge("اثر انگشت عوض شد", "danger"))
        if row["issued"] is not None and row["issued"].renews_license_id:
            flags.append(badge("تمدیدِ " + code(row["issued"].renews_license_id), "info"))
        if row["successor"] is not None:
            flags.append(badge("جایگزین شد" if row["replaced"] else "جانشین صادر شده، هنوز نصب نشده",
                               "muted" if row["replaced"] else "info"))
        name = esc(install.label) or '<i class="muted">بی‌نام</i>'
        body_rows.append(f"""
          <tr{' class="dim"' if row["replaced"] else ""}>
            <td data-label="مشتری"><a class="name" href="{link(install.license_id)}">{name}</a>
                <div>{code(install.license_id)}</div><div class="flags">{"".join(flags)}</div></td>
            <td data-label="اعتبار">{v_badge}<div class="sub">{v_text}</div></td>
            <td data-label="دسترسی">{a_badge}<div class="sub">{a_text}</div></td>
            <td data-label="آخرین ارتباط">{c_badge}<div class="sub">{c_text}</div></td>
            <td data-label="نسخه">{esc(install.panel_version) or '<span class="muted">نامشخص</span>'}</td>
            <td class="go"><a class="btn small" href="{link(install.license_id)}">جزئیات</a></td>
          </tr>""")
    empty = ('<tr><td colspan="6" class="empty">موردی با این فیلتر پیدا نشد.</td></tr>'
             if all_counts["total"] else
             '<tr><td colspan="6" class="empty">هنوز لایسنسی صادر نشده و پنلی گزارش نداده است.</td></tr>')
    body = f"""
      <div class="head"><h1>لایسنس‌ها</h1><a class="btn primary" href="/console/issue">+ صدور لایسنس جدید</a></div>
      {done_notice(done)}
      <div class="tiles">{tile_html}</div>
      {filter_form}
      <p class="muted small">{fa(len(rows))} مورد نمایش داده شده. «اعتبار» از توکن امضاشده می‌آید؛
        «دسترسی» تصمیم شماست؛ «آخرین ارتباط» زمان heartbeat پنل است. این سه از هم مستقل‌اند.</p>
      <table class="list"><thead><tr><th>مشتری</th><th>اعتبار</th><th>دسترسی</th>
        <th>آخرین ارتباط</th><th>نسخه</th><th></th></tr></thead>
        <tbody>{"".join(body_rows) or empty}</tbody></table>"""
    return page("لایسنس‌ها", body, csrf=csrf, active="list")


# --------------------------------------------------------------------------
# events
# --------------------------------------------------------------------------
def _detail_text(event) -> str:
    detail = ledger.event_detail(event)
    parts = []
    if "before" in detail and "after" in detail:
        b, a = detail["before"], detail["after"]
        for key, label in (("revoked", "قفل"), ("lock_scope", "دامنه"), ("label", "نام")):
            if key in b and b.get(key) != a.get(key):
                def show(x):
                    if key == "revoked":
                        return "قفل" if x else "باز"
                    if key == "lock_scope":
                        return SCOPE_LABELS.get(x, x)
                    return x or "—"
                parts.append(f"{label}: {esc(show(b.get(key)))} ← {esc(show(a.get(key)))}")
        if b.get("note") != a.get("note"):
            parts.append("یادداشت تغییر کرد")
    if detail.get("successor"):
        parts.append("جانشین: " + code(detail["successor"]))
    if detail.get("renews"):
        parts.append("ادامهٔ " + code(detail["renews"]))
    if detail.get("expires_at"):
        parts.append("پایان: " + jdate(dt.datetime.fromisoformat(detail["expires_at"])))
    if detail.get("valid_days"):
        parts.append(fa(f"{detail['valid_days']} روز از نصب"))
    if detail.get("basis") == "perpetual":
        parts.append("بدون انقضا")
    if event.reason:
        parts.append("دلیل: " + esc(event.reason))
    return " · ".join(parts)


def events_table(rows, show_license: bool = True) -> str:
    if not rows:
        return '<p class="muted">رویدادی ثبت نشده است.</p>'
    items = []
    for e in rows:
        lic = f'<a href="{link(e.license_id)}">{code(e.license_id)}</a>' if (show_license and e.license_id) else ""
        items.append(f"""<tr><td data-label="زمان">{jdate(e.at)}</td>
          <td data-label="رویداد">{ACTIONS.get(e.action, esc(e.action))}</td>
          {f'<td data-label="لایسنس">{lic}</td>' if show_license else ''}
          <td data-label="جزئیات">{_detail_text(e)}</td>
          <td data-label="IP">{code(e.ip) if e.ip else ''}</td></tr>""")
    head = "<th>زمان</th><th>رویداد</th>" + ("<th>لایسنس</th>" if show_license else "") + "<th>جزئیات</th><th>IP</th>"
    return f'<table class="list events"><thead><tr>{head}</tr></thead><tbody>{"".join(items)}</tbody></table>'


def events_page(rows, csrf: str):
    return page("رویدادها", f"""<div class="head"><h1>رویدادهای کنسول</h1></div>
      <p class="muted small">هر ورود، تغییر دسترسی، صدور، تمدید و حذف از فهرست اینجا ثبت می‌شود.
        توکن‌ها و رمزها هرگز ثبت نمی‌شوند.</p>{events_table(rows)}""", csrf=csrf, active="events")


# --------------------------------------------------------------------------
# password
# --------------------------------------------------------------------------
def password_page(csrf: str, error: str = "", done: str = ""):
    errors = {"current": "رمز فعلی درست نیست.", "short": "رمز جدید باید دست‌کم ۱۲ نویسه باشد.",
              "mismatch": "تکرار رمز جدید یکسان نیست.", "same": "رمز جدید با رمز فعلی یکی است."}
    msg = notice(errors[error], "danger") if error in errors else done_notice(done)
    return page("رمز کنسول", f"""<div class="head"><h1>تغییر رمز کنسول</h1></div>{msg}
      <form method="post" action="/console/password" class="card narrow">{csrf_field(csrf)}
        <label for="cur">رمز فعلی</label><input id="cur" name="current" type="password" autocomplete="current-password" required>
        <label for="new">رمز جدید (دست‌کم ۱۲ نویسه)</label><input id="new" name="new" type="password" autocomplete="new-password" minlength="12" required>
        <label for="rep">تکرار رمز جدید</label><input id="rep" name="repeat" type="password" autocomplete="new-password" minlength="12" required>
        <p class="muted small">پس از تغییر، همهٔ نشست‌های دیگر بسته می‌شوند. کلید امضا و داده‌ها دست نمی‌خورند.</p>
        <button class="btn primary">تغییر رمز</button>
      </form>""", csrf=csrf, active="password", status_code=400 if error else 200)

