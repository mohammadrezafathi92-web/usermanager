"""One licence: its facts, its history, and the confirmation steps for
changing who it locks."""
from __future__ import annotations

from urllib.parse import quote

import ledger
from ui import SCOPE_EFFECTS, SCOPE_LABELS, badge, code, csrf_field, esc, fa, jdate, notice, page
from views import access_view, contact_view, delivery_view, done_notice, events_table, link, usage_view, validity_view


def _fact(title: str, badge_html: str, text: str, extra: str = "") -> str:
    return f'<div class="fact card"><h3>{title}</h3>{badge_html}<p>{text}</p>{extra}</div>'


def _issued_block(issued, install, csrf: str) -> str:
    if issued is None:
        return f"""<section class="card"><h2>اطلاعات صدور</h2>
          <p>این لایسنس پیش از ثبت سوابق در این کنسول یا بیرون از آن (ابزار خط فرمان) صادر شده است؛
            تاریخ اعتبار، مبنای شروع و اتصال آن به سرور اینجا <b>نامشخص</b> است و حدس زده نمی‌شود.</p>
          <p class="muted small">اگر توکن این لایسنس را دارید، آن را اینجا وارد کنید. امضای آن با کلید همین سرور
            بررسی می‌شود و فقط اطلاعات داخل توکن ثبت می‌شود؛ خود توکن ذخیره نمی‌شود.</p>
          <form method="post" action="/console/license/import">{csrf_field(csrf)}
            <input type="hidden" name="license_id" value="{esc(install.license_id)}">
            <label for="tok">توکن لایسنس (NETCIP1…)</label>
            <textarea id="tok" name="token" dir="ltr" rows="3" required autocomplete="off" spellcheck="false"></textarea>
            <button class="btn">بررسی و ثبت اطلاعات توکن</button>
          </form></section>"""
    if issued.expires_at:
        basis = "تاریخ پایان ثابت: " + jdate(issued.expires_at)
    elif issued.valid_days:
        basis = fa(f"{issued.valid_days} روز از زمان نصب روی پنل")
        if install is not None and install.activated_at:
            basis += " · فعال‌سازی گزارش‌شده: " + jdate(install.activated_at)
    else:
        basis = "بدون تاریخ انقضا"
    fp = code(issued.fingerprint) if issued.fingerprint else badge("روی هر سروری (فقط تست)", "warn")
    mismatch = ""
    if issued.fingerprint and install is not None and install.fingerprint and install.fingerprint != issued.fingerprint:
        mismatch = notice("اثر انگشتی که پنل گزارش می‌دهد با اثر انگشت داخل لایسنس یکی نیست؛ "
                          "پنل این لایسنس را برای «سرور دیگر» رد می‌کند.", "danger")
    source = "صادرشده در همین کنسول" if issued.source == "console" else "ثبت‌شده از توکن (امضا تأیید شد)"
    return f"""<section class="card"><h2>اطلاعات صدور</h2>{mismatch}
      <dl class="grid">
        <dt>نام داخل توکن</dt><dd>{esc(issued.customer) or '—'}</dd>
        <dt>تاریخ صدور</dt><dd>{jdate(issued.issued_at)}</dd>
        <dt>مبنای اعتبار</dt><dd>{basis}</dd>
        <dt>اتصال به سرور</dt><dd>{fp}</dd>
        <dt>منبع این اطلاعات</dt><dd>{source}</dd>
      </dl></section>"""


def _chain_block(db, install, chain) -> str:
    if len(chain) <= 1:
        return ""
    items = []
    for lid in chain:
        issued = ledger.get_issued(db, lid)
        when = jdate(issued.issued_at, with_time=False) if issued else "نامشخص"
        here = ' aria-current="true" class="here"' if lid == install.license_id else ""
        items.append(f'<li{here}><a href="{link(lid)}">{code(lid)}</a> <span class="muted">صدور: {when}</span></li>')
    return f"""<section class="card"><h2>تاریخچهٔ تمدید</h2>
      <p class="muted small">هر تمدید یک لایسنس جانشین با شناسهٔ جدید است که به لایسنس قبلی وصل شده.</p>
      <ol class="chain">{"".join(items)}</ol></section>"""


def _usage_block(u: dict) -> str:
    """The aggregate counts and exactly what each one counts."""
    head, sub = usage_view(u)
    if u["state"] == "unknown":
        body = f"<p>{head} {sub}</p>"
    else:
        sampled = jdate(u["sampled_at"]) if u.get("sampled_at") else "نامشخص"
        body = f"""<p>{head}</p><dl class="grid">
          <dt>حساب‌های مشتری</dt><dd>{fa(f"{u['accounts']:,}")}</dd>
          <dt>حساب‌های آنلاین</dt><dd>{fa(f"{u['online']:,}")}</dd>
          <dt>نشست/اتصال آنلاین</dt><dd>{fa(f"{u['sessions']:,}")}</dd>
          <dt>دریافت در این سرور</dt><dd>{jdate(u['received_at'])} ({sub})</dd>
          <dt>زمان نمونه در پنل</dt><dd>{sampled}</dd></dl>"""
    return f"""<section class="card"><h2>آمار مشتری‌ها</h2>{body}
      <p class="muted small">حساب مشتری: هر حساب یک بار، هر چند اتصال داشته باشد. آنلاین: حساب‌هایی که دست‌کم یک اتصال
        فعال دارند. نشست/اتصال آنلاین جدا شمرده می‌شود و تعداد مشتری یا دستگاه دقیق نیست. فقط همین اعداد از پنل
        می‌آید و هیچ اطلاعات شناسایی مشتری ارسال نمی‌شود. این آمار فقط نمایشی است و محدودیتی اعمال نمی‌کند.</p></section>"""


def detail_page(db, row: dict, csrf: str, done: str = ""):
    install, issued = row["install"], row["issued"]
    v_badge, v_text = validity_view(row["validity"])
    a_badge, a_text = access_view(install)
    c_badge, c_text = contact_view(row["contact"], install)
    delivery = ledger.delivery(db, install)
    lid = esc(install.license_id)
    succ = row["successor"]
    succ_note = ""
    if succ is not None:
        succ_note = notice(
            ("این لایسنس تمدید شده و پنل اکنون با لایسنس جانشین گزارش می‌دهد: " if row["replaced"] else
             "لایسنس جانشین صادر شده ولی پنل هنوز با آن گزارش نداده؛ توکن جدید باید در پنل مشتری نصب شود: ")
            + f'<a href="{link(succ.license_id)}">{code(succ.license_id)}</a>', "info")
    moved = ""
    if install.fingerprint_changed_at:
        moved = notice("اثر انگشت سرور این لایسنس در " + jdate(install.fingerprint_changed_at)
                       + " تغییر کرد؛ ممکن است پنل به سرور دیگری منتقل یا کپی شده باشد.", "danger")
    scopes = "".join(
        f'<label class="opt"><input type="radio" name="scope" value="{s}"{" checked" if s == install.lock_scope else ""}>'
        f'<b>{SCOPE_LABELS[s]}</b><span>{SCOPE_EFFECTS[s]}</span></label>' for s in SCOPE_LABELS)
    access_form = f"""<section class="card"><h2>قفل و دامنهٔ محدودیت</h2>
      <p class="muted small">{delivery_view(delivery)}</p>
      <form method="get" action="/console/license/access">
        <input type="hidden" name="id" value="{lid}">
        <fieldset><legend>وضعیت</legend>
          <label class="opt inline"><input type="radio" name="revoked" value="0"{"" if install.revoked else " checked"}> باز</label>
          <label class="opt inline"><input type="radio" name="revoked" value="1"{" checked" if install.revoked else ""}> قفل</label>
        </fieldset>
        <fieldset><legend>دامنهٔ قفل</legend>{scopes}</fieldset>
        <button class="btn {'primary' if install.revoked else 'danger'}">بررسی تغییر</button>
        <span class="muted small">تغییر پس از صفحهٔ تأیید اعمال می‌شود.</span>
      </form></section>"""
    label_form = f"""<section class="card"><h2>نام و یادداشت داخلی</h2>
      <form method="post" action="/console/license/label">{csrf_field(csrf)}
        <input type="hidden" name="license_id" value="{lid}">
        <label for="lbl">نام مشتری در این کنسول</label><input id="lbl" name="label" value="{esc(install.label)}" maxlength="200">
        <label for="nt">یادداشت</label><textarea id="nt" name="note" rows="2" maxlength="2000">{esc(install.note)}</textarea>
        <button class="btn">ذخیره</button>
      </form></section>"""
    renew = f'<a class="btn primary" href="/console/issue?renews={quote(install.license_id, safe="")}">تمدید (صدور لایسنس جانشین)</a>'
    forget = ("" if install.revoked else
              f'<a class="btn ghost danger-text" href="/console/license/forget?id={quote(install.license_id, safe="")}">برداشتن از فهرست</a>')
    body = f"""
      <p><a href="/console">&rarr; بازگشت به فهرست</a></p>
      <div class="head"><div><h1>{esc(install.label) or 'بی‌نام'}</h1>
        <div class="idline">{code(install.license_id)} <button type="button" class="btn ghost small" data-copy="{lid}">کپی شناسه</button></div></div>
        <div class="actions">{renew}{forget}</div></div>
      {done_notice(done)}{succ_note}{moved}
      <div class="facts">
        {_fact("اعتبار لایسنس", v_badge, v_text)}
        {_fact("دسترسی", a_badge, a_text)}
        {_fact("آخرین ارتباط", c_badge, c_text)}
      </div>
      {_usage_block(row["usage"])}
      <section class="card"><h2>آنچه پنل گزارش داده</h2><dl class="grid">
        <dt>اثر انگشت فعلی</dt><dd>{code(install.fingerprint) if install.fingerprint else '<span class="muted">نامشخص</span>'}</dd>
        <dt>نسخهٔ پنل</dt><dd>{esc(install.panel_version) or '<span class="muted">نامشخص</span>'}</dd>
        <dt>اولین ثبت</dt><dd>{jdate(install.first_seen)}</dd>
        <dt>تعداد heartbeat</dt><dd>{fa(install.heartbeat_count or 0)}</dd>
      </dl></section>
      {_issued_block(issued, install, csrf)}
      {_chain_block(db, install, ledger.lineage(db, install.license_id))}
      <div class="two">{access_form}{label_form}</div>
      <section class="card"><h2>رویدادهای این لایسنس</h2>{events_table(ledger.events(db, license_id=install.license_id, limit=50), show_license=False)}</section>"""
    return page(install.label or install.license_id, body, csrf=csrf, active="list")


def access_confirm_page(install, revoked: bool, scope: str, csrf: str, error: str = ""):
    lid = esc(install.license_id)
    before = ("قفل" if install.revoked else "باز") + " · " + SCOPE_LABELS[install.lock_scope]
    after = ("قفل" if revoked else "باز") + " · " + SCOPE_LABELS[scope]
    if revoked:
        effect = SCOPE_EFFECTS[scope]
    else:
        effect = ("قفل برداشته می‌شود. اگر لایسنس از نظر تاریخ معتبر باشد، پنل در heartbeat بعدی باز می‌شود؛ "
                  "دامنهٔ انتخاب‌شده فقط برای قفل بعدی نگه داشته می‌شود.")
    ack = ""
    if revoked and scope == "everything":
        ack = """<label class="opt warnbox"><input type="checkbox" name="ack" value="yes" required>
          <span>می‌دانم که اتصال VPN همهٔ کاربران نهایی این پنل قطع می‌شود.</span></label>"""
    errors = {"stale": "وضعیت این لایسنس در این فاصله تغییر کرده است؛ صفحه را دوباره بررسی کنید.",
              "ack": "برای قطع کامل، تأیید آن لازم است."}
    msg = notice(errors[error], "danger") if error in errors else ""
    body = f"""<p><a href="{link(install.license_id)}">&rarr; بازگشت</a></p>
      <section class="card narrow"><h1>تأیید تغییر دسترسی</h1>{msg}
        <p>{esc(install.label) or 'بی‌نام'} {code(install.license_id)}</p>
        <dl class="grid"><dt>اکنون</dt><dd>{esc(before)}</dd><dt>پس از تأیید</dt><dd><b>{esc(after)}</b></dd></dl>
        {notice(effect, "danger" if revoked else "info")}
        <p class="muted small">پنل این تغییر را در heartbeat بعدی خود (پیش‌فرض هر ۳ ساعت) دریافت می‌کند،
          نه فوراً. «بررسی دوباره» در تنظیمات لایسنس پنل مشتری آن را زودتر می‌گیرد.</p>
        <form method="post" action="/console/license/access">{csrf_field(csrf)}
          <input type="hidden" name="license_id" value="{lid}">
          <input type="hidden" name="revoked" value="{'1' if revoked else '0'}">
          <input type="hidden" name="scope" value="{esc(scope)}">
          <input type="hidden" name="expect_revoked" value="{'1' if install.revoked else '0'}">
          <input type="hidden" name="expect_scope" value="{esc(install.lock_scope)}">
          <label for="rsn">دلیل (برای سابقه، اختیاری)</label><input id="rsn" name="reason" maxlength="500">
          {ack}
          <button class="btn {'danger' if revoked else 'primary'}" data-once>تأیید و اعمال</button>
          <a class="btn ghost" href="{link(install.license_id)}">انصراف</a>
        </form></section>"""
    return page("تأیید تغییر دسترسی", body, csrf=csrf, active="list", status_code=409 if error == "stale" else 200)


def forget_page(install, csrf: str, blocked: bool = False):
    if blocked:
        text = notice("این لایسنس قفل است. برداشتن آن از فهرست قفل را از بین می‌برد (پنل در heartbeat بعدی "
                      "به‌عنوان ردیف تازهٔ باز ثبت می‌شود)، پس مجاز نیست. اگر واقعاً لازم است، اول آن را باز کنید.", "danger")
        return page("برداشتن از فهرست", f'<section class="card narrow">{text}<a class="btn" href="{link(install.license_id)}">بازگشت</a></section>',
                    csrf=csrf, status_code=409)
    body = f"""<section class="card narrow"><h1>برداشتن از فهرست</h1>
      <p>{esc(install.label) or 'بی‌نام'} {code(install.license_id)}</p>
      {notice("این کار لایسنس را باطل یا مسدود نمی‌کند. اگر پنل دوباره heartbeat بفرستد، به‌صورت ردیف تازه و باز "
              "برمی‌گردد. سابقهٔ صدور و رویدادها حفظ می‌شود. برای جلوگیری از کار پنل از «قفل» استفاده کنید.", "warn")}
      <form method="post" action="/console/license/forget">{csrf_field(csrf)}
        <input type="hidden" name="license_id" value="{esc(install.license_id)}">
        <button class="btn danger" data-once>برداشتن</button>
        <a class="btn ghost" href="{link(install.license_id)}">انصراف</a></form></section>"""
    return page("برداشتن از فهرست", body, csrf=csrf)
