"""Issuing a licence, and renewing one: form -> review -> signed token.

A renewal is NOT a central extension. The token format has no way to push a
new term to a panel: the panel trusts only the token installed in it. So a
renewal signs a successor licence (new id, same server binding), links it to
the old one here, and the new token has to be installed in the customer's
panel. Until then the panel keeps running on the old key, with its old dates.

The validity bases are exactly the ones the token already supports
(scripts/license_tool.py): from now (exp), from installation (vd), until a
date (exp), or none. "From the end of the current term" is only arithmetic
on a known expiry, producing a plain exp - offered only when that expiry is
known from a signed payload.
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

from ui import SCOPE_EFFECTS, SCOPE_LABELS, TEHRAN, badge, code, csrf_field, esc, fa, jdate, notice, page
from views import link

MAX_DAYS = 3650
BASES = ("days", "install", "until", "after", "perpetual")


def plan(values: dict, previous_expiry: Optional[dt.datetime], now: dt.datetime) -> tuple[dict, dict]:
    """Validate the form. Returns (plan, errors); plan is what gets signed."""
    errors: dict[str, str] = {}
    customer = (values.get("customer") or "").strip()
    if not customer:
        errors["customer"] = "نام مشتری لازم است."
    elif len(customer) > 200:
        errors["customer"] = "نام مشتری طولانی است."
    fingerprint = (values.get("fingerprint") or "").strip()
    any_machine = values.get("any_machine") == "yes"
    if any_machine:
        fingerprint = ""
    elif not fingerprint:
        errors["fingerprint"] = "اثر انگشت سرور مشتری لازم است (یا «روی هر سروری» را برای تست انتخاب کنید)."
    elif len(fingerprint) > 64 or not all(c in "0123456789abcdef" for c in fingerprint.lower()):
        errors["fingerprint"] = "اثر انگشت باید همان رشتهٔ هگز نمایش‌داده‌شده در پنل مشتری باشد."
    scope = values.get("scope") or ""
    if scope not in SCOPE_LABELS:
        errors["scope"] = "دامنهٔ قفل را انتخاب کنید."
    basis = values.get("basis") or ""
    days = None
    raw_days = (values.get("amount") or "").strip()
    if basis in ("days", "install", "after"):
        if not raw_days.isdigit() or not 1 <= int(raw_days) <= MAX_DAYS:
            errors["amount"] = fa(f"تعداد روز باید عددی بین ۱ و {MAX_DAYS} باشد.")
        else:
            days = int(raw_days)
    result = {"customer": customer, "fingerprint": fingerprint, "scope": scope, "basis": basis,
              "amount": raw_days, "note": (values.get("note") or "").strip()[:500],
              "until": (values.get("until") or "").strip(), "renews": values.get("renews") or "",
              "any_machine": any_machine, "days": None, "valid_days": None, "expires_at": None}
    if basis not in BASES:
        errors["basis"] = "مبنای اعتبار را انتخاب کنید."
    elif basis == "days" and days:
        result["days"] = days
        result["expires_at"] = now + dt.timedelta(days=days)
    elif basis == "install" and days:
        result["valid_days"] = days
    elif basis == "after":
        if previous_expiry is None or previous_expiry <= now:
            errors["basis"] = "پایان اعتبار فعلی معلوم یا آینده نیست؛ مبنای دیگری انتخاب کنید."
        elif days:
            result["expires_at"] = previous_expiry + dt.timedelta(days=days)
    elif basis == "until":
        try:
            day = dt.date.fromisoformat(result["until"])
            end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(), TEHRAN) - dt.timedelta(seconds=1)
            moment = end.astimezone(dt.timezone.utc).replace(tzinfo=None)
            if moment <= now:
                errors["until"] = "تاریخ پایان باید در آینده باشد."
            else:
                result["expires_at"] = moment
        except ValueError:
            errors["until"] = "تاریخ پایان را انتخاب کنید."
    return result, errors


def _radio(name, value, current, title, text, disabled=False):
    return (f'<label class="opt{" disabled" if disabled else ""}"><input type="radio" name="{name}" value="{value}"'
            f'{" checked" if value == current else ""}{" disabled" if disabled else ""} required>'
            f'<b>{title}</b><span>{text}</span></label>')


def _err(errors, key):
    return f'<p class="field-error">{errors[key]}</p>' if key in errors else ""


def form_page(values: dict, csrf: str, *, previous=None, errors: Optional[dict] = None, key_ready: bool = True,
              fingerprint_source: str = ""):
    errors = errors or {}
    if not key_ready:
        return page("صدور لایسنس", notice(
            "کلید خصوصی امضا روی این سرور پیدا نشد؛ صدور و تمدید ممکن نیست. کلید فعلی باید در "
            "<code>license_server/data/</code> باشد؛ کلید تازه نسازید، چون همهٔ لایسنس‌های قبلی را بی‌اعتبار می‌کند.",
            "danger"), csrf=csrf, active="issue")
    renew = previous is not None
    prev_html = ""
    after_text, after_ok = "فقط وقتی پایان اعتبار فعلی از توکن امضاشده معلوم و در آینده باشد.", False
    if renew:
        install, v = previous["install"], previous["validity"]
        if v.get("expires_at") and v["state"] in ("valid", "expiring"):
            after_ok = True
            after_text = "پایان اعتبار فعلی (" + jdate(v["expires_at"]) + ") + این تعداد روز؛ یک تاریخ پایان ثابت."
        prev_html = f"""<section class="card"><h2>تمدید لایسنس {code(install.license_id)}</h2>
          {notice("تمدید مرکزیِ خودکار وجود ندارد. یک لایسنس جانشین با شناسهٔ جدید صادر و به همین سابقه وصل می‌شود؛ "
                  "توکن آن باید در پنل مشتری نصب شود. تا آن زمان پنل با لایسنس فعلی و تاریخ‌های خودش کار می‌کند. "
                  "لایسنس فعلی باطل نمی‌شود.", "info")}
          <dl class="grid"><dt>مشتری</dt><dd>{esc(install.label) or 'بی‌نام'}</dd>
            <dt>اعتبار فعلی</dt><dd>{_validity_line(v)}</dd>
            <dt>دسترسی فعلی</dt><dd>{"قفل" if install.revoked else "باز"} · {SCOPE_LABELS[install.lock_scope]}</dd></dl>
          {notice("این لایسنس قبلاً جانشین " + code(previous["successor"].license_id) + " دارد؛ تمدید را معمولاً از "
                  "آخرین لایسنس زنجیره انجام دهید تا دو جانشین موازی ساخته نشود.", "warn") if previous["successor"] is not None else ""}
          {notice("این لایسنس اکنون قفل است. لایسنس جانشین باز صادر می‌شود؛ پس از نصب آن پنل باز می‌شود.", "warn") if install.revoked else ""}
        </section>"""
    v = values
    scopes = "".join(_radio("scope", s, v.get("scope"), SCOPE_LABELS[s], SCOPE_EFFECTS[s]) for s in SCOPE_LABELS)
    bases = [
        _radio("basis", "days", v.get("basis"), "از همین حالا", "اعتبار از لحظهٔ صدور شمرده می‌شود (تاریخ پایان ثابت)."),
        _radio("basis", "install", v.get("basis"), "از زمان نصب",
               "شمارش از اولین باری که پنل این کلید را ببیند شروع می‌شود"
               + ("؛ روزهای باقی‌ماندهٔ لایسنس فعلی به آن اضافه نمی‌شود." if renew else ".")),
    ]
    if renew:
        bases.append(_radio("basis", "after", v.get("basis"), "از پایان اعتبار فعلی", after_text, disabled=not after_ok))
    bases += [
        _radio("basis", "until", v.get("basis"), "تا تاریخ مشخص", "تا پایان روز انتخاب‌شده به وقت تهران."),
        _radio("basis", "perpetual", v.get("basis"), "بدون انقضا", "لایسنس تاریخ پایان ندارد."),
    ]
    title = "تمدید لایسنس" if renew else "صدور لایسنس جدید"
    body = f"""<p><a href="{link(previous['install'].license_id) if renew else '/console'}">&rarr; بازگشت</a></p>
      <div class="head"><h1>{title}</h1></div>{prev_html}
      <form method="post" action="/console/issue/review" class="card">{csrf_field(csrf)}
        <input type="hidden" name="renews" value="{esc(v.get('renews'))}">
        <label for="cust">نام مشتری</label>
        <input id="cust" name="customer" value="{esc(v.get('customer'))}" maxlength="200" required>{_err(errors, "customer")}
        <label for="fp">اثر انگشت سرور مشتری</label>
        <input id="fp" name="fingerprint" dir="ltr" value="{esc(v.get('fingerprint'))}" autocomplete="off" spellcheck="false">
        <p class="muted small">{fingerprint_source or "در پنل مشتری: تنظیمات ← کارت لایسنس ← «اثر انگشت این سرور»."}
          لایسنس فقط روی همان سرور پذیرفته می‌شود.</p>{_err(errors, "fingerprint")}
        <label class="opt inline"><input type="checkbox" name="any_machine" value="yes"{" checked" if v.get("any_machine") else ""}>
          روی هر سروری (بدون اتصال؛ فقط برای تست)</label>
        <fieldset><legend>مبنای اعتبار</legend>{"".join(bases)}{_err(errors, "basis")}
          <div class="row"><div><label for="amt">تعداد روز</label>
            <input id="amt" name="amount" type="number" min="1" max="{MAX_DAYS}" inputmode="numeric" value="{esc(v.get('amount'))}">{_err(errors, "amount")}</div>
          <div><label for="until">تاریخ پایان (برای «تا تاریخ مشخص»)</label>
            <input id="until" name="until" type="date" dir="ltr" value="{esc(v.get('until'))}">{_err(errors, "until")}</div></div>
        </fieldset>
        <fieldset><legend>دامنهٔ قفل، اگر روزی قفل شود</legend>{scopes}{_err(errors, "scope")}</fieldset>
        <label for="note">یادداشت داخل توکن (اختیاری؛ برای مشتری هم خواناست)</label>
        <input id="note" name="note" value="{esc(v.get('note'))}" maxlength="500">
        <button class="btn primary">پیش‌نمایش</button>
      </form>"""
    return page(title, body, csrf=csrf, active="issue", status_code=400 if errors else 200)


def _validity_line(v: dict) -> str:
    state = v["state"]
    if state == "unknown":
        return "نامشخص (اطلاعات صدور ثبت نشده)"
    if state == "not_started":
        return fa(f"{v['valid_days']} روز از نصب؛ هنوز فعال‌سازی گزارش نشده")
    if state == "perpetual":
        return "بدون انقضا"
    return ("منقضی از " if state == "expired" else "تا ") + jdate(v["expires_at"])


def _plan_validity(p: dict) -> str:
    if p["basis"] == "install":
        return fa(f"{p['valid_days']} روز از لحظه‌ای که پنل این کلید را برای اولین بار ببیند")
    if p["basis"] == "perpetual":
        return "بدون انقضا"
    return "تا " + jdate(p["expires_at"])


def review_page(p: dict, csrf: str, nonce: str, previous=None):
    fields = "".join(f'<input type="hidden" name="{k}" value="{esc(p[k])}">'
                     for k in ("customer", "fingerprint", "scope", "basis", "amount", "until", "note", "renews"))
    if p["any_machine"]:
        fields += '<input type="hidden" name="any_machine" value="yes">'
    fp = code(p["fingerprint"]) if p["fingerprint"] else badge("روی هر سروری (فقط تست)", "warn")
    warn = ""
    if previous is not None:
        old = previous["install"].fingerprint
        if old and p["fingerprint"] and old != p["fingerprint"]:
            warn = notice("این اثر انگشت با آخرین اثر انگشت گزارش‌شده از پنل فعلی فرق دارد؛ "
                          "لایسنس جدید روی آن پنل پذیرفته نمی‌شود.", "danger")
    renew_line = (f"<dt>ادامهٔ لایسنس</dt><dd>{code(p['renews'])}</dd>" if p["renews"] else "")
    body = f"""<section class="card narrow"><h1>{"پیش‌نمایش تمدید" if p["renews"] else "پیش‌نمایش صدور"}</h1>{warn}
      <dl class="grid"><dt>مشتری</dt><dd>{esc(p["customer"])}</dd>{renew_line}
        <dt>اعتبار</dt><dd><b>{_plan_validity(p)}</b></dd>
        <dt>اتصال به سرور</dt><dd>{fp}</dd>
        <dt>دسترسی اولیه</dt><dd>باز · دامنهٔ قفل: {SCOPE_LABELS[p["scope"]]}</dd>
        <dt>یادداشت</dt><dd>{esc(p["note"]) or "—"}</dd></dl>
      <p class="muted small">پس از امضا، توکن فقط یک‌بار نمایش داده می‌شود و روی این سرور ذخیره نمی‌شود.</p>
      <form method="post" action="/console/issue">{csrf_field(csrf)}{fields}
        <input type="hidden" name="nonce" value="{esc(nonce)}">
        <button class="btn primary" data-once>امضا و صدور</button></form>
      <form method="post" action="/console/issue/edit">{csrf_field(csrf)}{fields}
        <button class="btn ghost">ویرایش</button></form></section>"""
    return page("پیش‌نمایش", body, csrf=csrf, active="issue")


def result_page(token: str, payload: dict, renews: str, csrf: str):
    lid = payload["id"]
    renew_text = ""
    if renews:
        renew_text = notice("لایسنس قبلی " + code(renews) + " باطل نشد و تا تاریخ خودش معتبر است. "
                            "تمدید فقط وقتی اثر دارد که این توکن در پنل مشتری نصب شود.", "info")
    body = f"""<section class="card"><h1>لایسنس صادر شد</h1>
      <p>{esc(payload.get("customer"))} · <a href="{link(lid)}">{code(lid)}</a></p>{renew_text}
      {notice("این توکن فقط همین یک‌بار نمایش داده می‌شود. اگر از دست برود، لایسنس دیگری صادر کنید.", "warn")}
      <textarea id="token" readonly dir="ltr" rows="5" spellcheck="false">{esc(token)}</textarea>
      <button type="button" class="btn primary" data-copy-from="token">کپی توکن</button>
      <h2>نصب در پنل مشتری</h2>
      <ol class="steps">
        <li>در پنل مشتری با حساب سوپرادمین: تنظیمات ← کارت «لایسنس».</li>
        <li>توکن را در کادر کلید بچسبانید و «ذخیرهٔ کلید» را بزنید. پنل امضا و اثر انگشت را همان‌جا بررسی می‌کند.</li>
        <li>اگر پنل قفل است، روی صفحهٔ قفل با نام کاربری و رمز سوپرادمین همین توکن را وارد کنید.</li>
        <li>پس از نصب، پنل با شناسهٔ جدید گزارش می‌دهد و این‌جا «در ارتباط» دیده می‌شود.</li>
      </ol>
      <a class="btn" href="{link(lid)}">جزئیات لایسنس</a></section>"""
    return page("لایسنس صادر شد", body, csrf=csrf, active="issue")


def already_issued_page(license_id: Optional[str], csrf: str):
    text = ("این درخواست قبلاً امضا شده است؛ لایسنس تکراری صادر نشد. توکن فقط در همان پاسخ اول نمایش داده شد."
            if license_id else "این پیش‌نمایش منقضی شده است؛ دوباره از فرم شروع کنید.")
    extra = f'<a class="btn" href="{link(license_id)}">جزئیات لایسنس</a>' if license_id else '<a class="btn" href="/console/issue">فرم صدور</a>'
    return page("صدور", f'<section class="card narrow">{notice(text, "warn")}{extra}</section>',
                csrf=csrf, active="issue", status_code=409)
