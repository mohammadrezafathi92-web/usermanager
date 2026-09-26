import React, { useEffect, useRef, useState } from "react";
import {
  Store, Wallet, Layers, AlertTriangle, Check, Upload, X, Loader2, Gift, Globe,
  Copy, ShoppingBag, Plus, RefreshCw, Clock,
} from "lucide-react";
import {
  fetchMiniAppHome, miniAppCheckout, miniAppCheckoutReceipt, miniAppTopupReceipt,
} from "../api/client.js";
import { formatBytes, formatToman } from "../utils.js";

/** Persian digits. Latin numerals beside Persian text render in a
 *  different face on most phones, which is what made the first
 *  version look unfinished next to a competitor's. */
const fa = (value) => String(value).replace(/[0-9]/g, (d) => "۰۱۲۳۴۵۶۷۸۹"[d]);

/**
 * The Telegram Mini App - the shop, seen from inside Telegram.
 *
 * ONE page serves every reseller on this install. They share a panel and a
 * hostname; what differs is the bot the customer opened it from, and
 * Telegram signs the initData blob with THAT bot's token. The backend checks
 * the signature against every bot this panel runs and whichever matches
 * names the reseller - so the prices, packages and payment details below
 * are that reseller's, decided by a signature rather than by anything in
 * the URL. See backend services/telegram_webapp.py.
 *
 * Deliberately NOT behind the admin router's auth: there is no session here
 * and no login. The only credential is initData, which Telegram hands the
 * page and which is useless anywhere else.
 *
 * Styling is its own thing rather than the panel's. This renders inside
 * Telegram's webview, on a phone, for a customer who has never seen the
 * admin panel - and Telegram supplies the surrounding colours, which is why
 * they are read from its CSS variables with our own dark set as the
 * fallback. The fallbacks are deliberately DARKER than the panel: a shop
 * that opens inside a chat app is judged against other shops that open
 * inside chat apps, and every one of them is dark.
 */
// Served by this panel, not by telegram.org - see the backend endpoint of
// the same name. telegram.org is precisely the host that is blocked on the
// networks this is sold into, and loading it from there made the page
// depend, at load time, on the one domain its customers cannot reach.
const TELEGRAM_SCRIPT = "/api/miniapp/telegram-web-app.js";

/**
 * The initData blob, from the URL rather than from Telegram's script.
 *
 * Telegram puts it in the page's fragment as tgWebAppData when it opens a
 * Mini App, so it is already there before any script runs. Reading it here
 * matters more than it looks: telegram.org is exactly the kind of host that
 * is slow or blocked on the networks this product is sold into, and the
 * first version made the whole page depend on that file arriving. When it
 * did not, `window.Telegram` never appeared, the effect that loads the page
 * skipped itself, and the app sat on "در حال بارگذاری…" for ever with
 * nothing to tap - reported as "هیچ ری‌اکتی نداره".
 *
 * The script is still loaded, because it is what supplies the theme
 * colours, expand() and the native buttons. But nothing the customer needs
 * depends on it any more.
 */
function initDataFromUrl() {
  const sources = [window.location.hash.slice(1), window.location.search.slice(1)];
  for (const source of sources) {
    if (!source) continue;
    const found = new URLSearchParams(source).get("tgWebAppData");
    if (found) return found;
  }
  return "";
}

/** Which parameters Telegram put on the url - names only, never values. */
function debugUrlKeys() {
  const keys = [];
  for (const source of [window.location.hash.slice(1), window.location.search.slice(1)]) {
    if (!source) continue;
    for (const [key] of new URLSearchParams(source)) keys.push(key);
  }
  return keys.join(", ");
}

function useTelegram() {
  // `undefined` means "still trying"; `null` means "the script is not
  // coming". Keeping those apart is the whole fix - collapsing them into
  // one falsy value is what made the page wait for ever.
  const [webApp, setWebApp] = useState(window.Telegram?.WebApp);
  const [settled, setSettled] = useState(Boolean(window.Telegram?.WebApp));

  useEffect(() => {
    if (window.Telegram?.WebApp) return undefined;

    // Loaded here rather than in index.html: the admin panel is the same
    // bundle and has no business fetching from telegram.org on every login
    // page, on networks where that request is one more thing to hang.
    const script = document.createElement("script");
    script.src = TELEGRAM_SCRIPT;
    script.async = true;
    const done = () => {
      setWebApp(window.Telegram?.WebApp || null);
      setSettled(true);
    };
    script.onload = done;
    script.onerror = done;
    // Belt and braces: a request that neither loads nor errors - a
    // connection that just hangs, the common failure here - would otherwise
    // never call either handler.
    const timer = setTimeout(done, 6000);
    document.head.appendChild(script);
    return () => {
      clearTimeout(timer);
      script.remove();
    };
  }, []);

  return { webApp, settled };
}

// ---------------------------------------------------------------- surfaces

const BG = "var(--tg-theme-bg-color,#0b0f14)";
const CARD = "bg-[var(--mi-card)] border border-[color:var(--mi-line)]";

// ------------------------------------------------------------------- theme
//
// Surfaces, lines and accents are CSS variables rather than the white/N% and
// *-400 utilities they used to be. Those were tuned for a dark background
// only: in Telegram's LIGHT theme the card fills (white at 4%) vanished into
// a white page, the hairlines disappeared, and the -400 accent text sat at
// roughly 2:1 on white. The wrapper's data-scheme (Telegram's own
// colorScheme) picks the set; the dark values are the ones the app shipped
// with, unchanged.
const MI_CSS = `
.mi{--mi-card:rgba(255,255,255,.05);--mi-soft:rgba(255,255,255,.07);--mi-line:rgba(255,255,255,.1);
--mi-sky:#38bdf8;--mi-emerald:#34d399;--mi-amber:#fbbf24;--mi-red:#f87171;--mi-violet:#a78bfa;}
.mi[data-scheme="light"]{--mi-card:rgba(0,0,0,.04);--mi-soft:rgba(0,0,0,.06);--mi-line:rgba(0,0,0,.12);
--mi-sky:#0369a1;--mi-emerald:#047857;--mi-amber:#b45309;--mi-red:#b91c1c;--mi-violet:#6d28d9}
.mi button,.mi [role=button]{-webkit-tap-highlight-color:transparent;touch-action:manipulation}
.mi button:focus-visible,.mi input:focus-visible{outline:2px solid var(--mi-sky);outline-offset:2px}
@keyframes mi-sheet{from{transform:translateY(28px);opacity:0}to{transform:none;opacity:1}}
@keyframes mi-fade{from{opacity:0}to{opacity:1}}
@keyframes mi-pulse{0%,100%{opacity:.55}50%{opacity:1}}
.mi-sheet{animation:mi-sheet .22s ease-out both}
.mi-fade{animation:mi-fade .18s ease-out both}
.mi-skel{background:var(--mi-soft);animation:mi-pulse 1.4s ease-in-out infinite}
@media (prefers-reduced-motion:reduce){.mi-sheet,.mi-fade,.mi-skel{animation:none}}
`;

/** Telegram's own bottom inset when it reports one, the OS's otherwise. */
const SAFE_BOTTOM = "max(1.25rem, env(safe-area-inset-bottom), var(--tg-safe-area-inset-bottom, 0px))";

/** Root of every screen: theme variables, the Telegram colours, RTL. */
function Shell({ scheme, children, className = "", center = false }) {
  return (
    <div
      className={`mi min-h-screen text-[var(--tg-theme-text-color,#fff)] ${center ? "flex items-center justify-center p-6" : ""} ${className}`}
      style={{ background: BG }}
      data-scheme={scheme === "light" ? "light" : "dark"}
      dir="rtl"
    >
      <style>{MI_CSS}</style>
      {children}
    </div>
  );
}


function Card({ children, className = "" }) {
  return <div className={`rounded-2xl ${CARD} p-4 ${className}`}>{children}</div>;
}

/** The tinted rounded square every card leads with. One shape, one size,
 *  so a column of cards has a single vertical rhythm instead of each card
 *  starting wherever its content happens to. */
function IconTile({ icon: Icon, tone = "sky" }) {
  const tones = {
    sky: "bg-sky-500/15 text-[color:var(--mi-sky)]",
    emerald: "bg-emerald-500/15 text-[color:var(--mi-emerald)]",
    violet: "bg-violet-500/15 text-[color:var(--mi-violet)]",
  };
  return (
    <div className={`w-11 h-11 rounded-2xl flex items-center justify-center shrink-0 ${tones[tone]}`}>
      <Icon size={20} />
    </div>
  );
}

function Badge({ children, tone = "emerald" }) {
  const tones = {
    emerald: "bg-emerald-500 text-white",
    slate: "bg-[var(--mi-soft)] opacity-80",
  };
  return (
    <span className={`text-[11px] px-2 py-1 rounded-lg font-medium ${tones[tone]}`}>{children}</span>
  );
}

function PageTitle({ title, subtitle }) {
  return (
    <div className="mb-5">
      <h1 className="text-2xl font-bold">{title}</h1>
      {subtitle && <p className="text-xs opacity-60 mt-1.5 leading-relaxed">{subtitle}</p>}
    </div>
  );
}

// ------------------------------------------------------------------ pieces

/**
 * The invite card.
 *
 * Real, not decoration: the referral program already exists end to end
 * (models.User.referral_code, PanelSettings.referral_*), the customer's own
 * code arrives with their account, and the reward amounts arrive with the
 * payment settings. Drawn only when the reseller has actually set a reward -
 * a card offering nothing is worse than no card.
 */
function ReferralCard({ code, payment, botUsername }) {
  const [copied, setCopied] = useState(false);
  const credit = payment?.referral_new_user_reward_credit || 0;
  const gb = payment?.referral_new_user_reward_gb || 0;
  const myCredit = payment?.referral_referrer_reward_credit || 0;
  const myGb = payment?.referral_referrer_reward_gb || 0;
  if (!code || (!credit && !gb && !myCredit && !myGb)) return null;

  const reward = (c, g) =>
    [c ? `${formatToman(c, "fa")} تومان` : "", g ? `${fa(g)} گیگابایت` : ""]
      .filter(Boolean)
      .join(" و ");

  const share = () => {
    const link = botUsername ? `https://t.me/${botUsername}?start=${code}` : code;
    // Telegram's own share sheet when the script is here; the clipboard
    // when it is not. The card must still do something on a webview where
    // telegram-web-app.js never arrived - that case is not rare here.
    if (window.Telegram?.WebApp?.openTelegramLink) {
      window.Telegram.WebApp.openTelegramLink(
        `https://t.me/share/url?url=${encodeURIComponent(link)}`
      );
      return;
    }
    navigator.clipboard?.writeText(link);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <Card className="mb-4">
      <div className="flex items-start gap-3">
        <IconTile icon={Gift} tone="violet" />
        <div className="min-w-0 flex-1">
          <div className="font-bold text-[15px]">با دعوت دوستان اعتبار بگیرید</div>
          <div className="text-xs opacity-60 mt-2 space-y-1 leading-relaxed">
            {reward(myCredit, myGb) && <div>شما بابت هر دعوت {reward(myCredit, myGb)} می‌گیرید.</div>}
            {reward(credit, gb) && <div>دوستتان هم {reward(credit, gb)} هدیه می‌گیرد.</div>}
          </div>
        </div>
      </div>
      <div className="flex items-center gap-2 mt-4">
        <button
          type="button"
          onClick={share}
          className="flex-1 min-h-[44px] rounded-xl bg-sky-500 active:bg-sky-600 text-white text-sm font-medium flex items-center justify-center gap-1.5"
        >
          {copied && <Check size={15} />}
          {copied ? "کپی شد" : "دعوت دوستان"}
        </button>
        <div
          className="px-3 min-h-[44px] rounded-xl bg-[var(--mi-soft)] text-xs font-mono flex items-center gap-2"
          dir="ltr"
        >
          <Copy size={13} className="opacity-60" />
          {code}
        </div>
      </div>
    </Card>
  );
}


/**
 * The free sample, at the top, on its own.
 *
 * It used to arrive as an ordinary plan inside «سایر پلن‌ها» - filed with
 * the leftovers at the very bottom, below everything a customer might pay
 * for, which is the opposite of what a sample is for. It is the first
 * thing a shop should offer someone who has not bought anything, so it is
 * the first thing on the screen.
 *
 * Drawn as a strip rather than a plan card on purpose: it is not one of
 * the options being compared. A customer choosing between plans is doing
 * a different job from a customer deciding whether to try at all, and
 * putting the sample in the comparison invites them to weigh «رایگان»
 * against a price, which it always wins.
 */
function TrialBanner({ trial, busy, onTake }) {
  if (!trial) return null;
  return (
    <div className="rounded-2xl border border-violet-500/25 bg-violet-500/[0.08] p-4 mb-4">
      <div className="flex items-start gap-3">
        <IconTile icon={Gift} tone="violet" />
        <div className="min-w-0 flex-1">
          <div className="font-bold text-[15px]">{trial.name}</div>
          <div className="text-xs opacity-60 mt-1">
            {trial.quota_gb
              ? `${fa(Math.round(trial.quota_gb * 1024))} مگابایت`
              : "حجم نامحدود"}
            {" · "}
            {trial.duration_days ? `${fa(trial.duration_days)} روز` : "بدون انقضا"}
            {" · "}
            رایگان
          </div>
        </div>
      </div>
      <button
        type="button"
        disabled={busy}
        onClick={() => onTake(trial)}
        className="w-full mt-3.5 min-h-[48px] rounded-xl bg-violet-500 active:bg-violet-600 disabled:opacity-50 text-white text-sm font-medium flex items-center justify-center gap-2"
      >
        {busy ? <Loader2 size={16} className="animate-spin" /> : <Gift size={16} />}
        دریافت رایگان
      </button>
    </div>
  );
}

/**
 * Every plan in ONE card.
 *
 * The names run along the top as a row of chips and the body below shows
 * whichever is selected - rather than a column of near-identical cards the
 * customer has to scroll through and hold in their head to compare. Asked
 * for in exactly those terms: «همه اینا یه کارت بشه و اسم پکیج ها کنار هم
 * باشه و با زدن روی هر کدوم توضیحات کارت عوض بشه».
 *
 * It is also the honest shape for this data. The plans differ in three
 * numbers and a sentence; showing one at a time with the numbers in a fixed
 * position means switching between them moves only what actually differs.
 *
 * Selection is kept as an ID and resolved against the current list on every
 * render, never as an index. The list changes underneath it - the seat
 * filter above, a reload after a purchase - and an index would then point at
 * a different plan than the one whose name is highlighted, which is the
 * quiet version of selling someone the wrong thing.
 */
function PackagePicker({ packages, onBuy }) {
  const [selectedId, setSelectedId] = useState(null);
  const pkg = packages.find((p) => p.id === selectedId) || packages[0];
  if (!pkg) return null;

  const protocols = [...new Set((pkg.connections || []).map((c) => c.protocol))];
  // A package the admin never gave a services bundle needs a node and a
  // protocol chosen by hand, which the bot asks over several questions and
  // this screen does not ask yet. Saying so is better than a button that
  // fails after it is pressed.
  const buyable = protocols.length > 0;
  const seats = pkg.max_concurrent_sessions || 0;
  // Nothing to pay, so nothing to ask. A checkout sheet offering a wallet
  // and a card number for a plan that costs zero is a form with no
  // question in it - and it was the only thing standing between the
  // customer and the sample.
  const free = (pkg.price || 0) === 0;
  // First in the list is the reseller's own answer to which plan they want
  // pushed: the packages arrive in Package.sort_order, which the admin
  // arranges by hand (routers/bot.py's list_packages). No new field, no guess.
  const recommended = packages[0];

  return (
    <div className={`rounded-2xl border ${CARD} overflow-hidden`}>
      {/* The names. Horizontally scrollable rather than wrapped: a wrapping
          row changes height as the selection moves, which makes the whole
          card jump under the thumb that just tapped it. */}
      <div className="flex gap-2 overflow-x-auto p-3 pb-3 border-b border-[color:var(--mi-line)]">
        {packages.map((p) => {
          const active = p.id === pkg.id;
          return (
            <button
              key={p.id}
              type="button"
              onClick={() => setSelectedId(p.id)}
              aria-pressed={active}
              className={`shrink-0 min-h-[44px] px-3.5 rounded-xl text-xs font-medium whitespace-nowrap transition-colors ${
                active ? "bg-sky-500 text-white" : "bg-[var(--mi-soft)] opacity-80"
              }`}
            >
              {p.name}
              {p.id === recommended.id && !active && (
                <span className="ms-1.5 inline-block w-1.5 h-1.5 rounded-full bg-emerald-400 align-middle" />
              )}
            </button>
          );
        })}
      </div>

      <div className="p-4">
        <div className="flex items-start gap-3">
          <IconTile icon={Globe} tone={pkg.id === recommended.id ? "emerald" : "sky"} />
          <div className="min-w-0 flex-1">
            <div className="flex items-start justify-between gap-2">
              <div className="font-bold text-[15px] truncate">{pkg.name}</div>
              {pkg.id === recommended.id && <Badge>پیشنهاد ما</Badge>}
            </div>
            <div className="text-xs opacity-60 mt-1">
              {pkg.quota_gb ? `${fa(pkg.quota_gb)} گیگابایت` : "حجم نامحدود"}
              {" · "}
              {pkg.duration_days ? `${fa(pkg.duration_days)} روز` : "بدون انقضا"}
              {seats > 0 ? ` · ${fa(seats)} کاربر همزمان` : ""}
            </div>
          </div>
        </div>

        {/* A fixed minimum height. Plans have descriptions of wildly
            different lengths, and without this the buy button slides up and
            down the screen as the customer taps between names - so the place
            they are aiming for is never where it was a moment ago. */}
        <div className="min-h-[3.5rem] mt-3">
          {pkg.description && (
            <p className="text-xs opacity-60 leading-relaxed">{pkg.description}</p>
          )}
        </div>

        {protocols.length > 0 && (
          <div className="flex flex-wrap gap-1.5 mt-1">
            {protocols.map((protocol) => (
              <span
                key={protocol}
                className="text-[11px] px-2 py-1 rounded-lg bg-[var(--mi-soft)] opacity-80"
                dir="ltr"
              >
                {protocol}
              </span>
            ))}
          </div>
        )}

        {/* The price sits INSIDE the button, on the far side. The eye looks
            for the number and the thumb looks for the button; putting them in
            one shape means it only has to find one thing. */}
        {buyable && free ? (
          <button
            type="button"
            onClick={() => onBuy(pkg, { free: true })}
            className="w-full mt-4 min-h-[48px] rounded-xl bg-violet-500 active:bg-violet-600 text-sm font-medium text-white flex items-center justify-center gap-2"
          >
            <Gift size={16} />
            دریافت رایگان
          </button>
        ) : buyable ? (
          <button
            type="button"
            onClick={() => onBuy(pkg)}
            className={`w-full mt-4 rounded-xl flex items-center justify-between gap-2 p-1 pe-4 text-sm font-medium text-white ${
              pkg.id === recommended.id
                ? "bg-emerald-500 active:bg-emerald-600"
                : "bg-sky-500 active:bg-sky-600"
            }`}
          >
            <span className="bg-black/20 rounded-lg px-3 py-2 text-xs">
              {formatToman(pkg.price, "fa")} تومان
            </span>
            <span className="flex-1 text-center">خرید</span>
          </button>
        ) : (
          <div className="text-xs opacity-60 mt-4 text-center py-3">
            خرید این پلن فعلاً از داخل خود ربات انجام می‌شود.
          </div>
        )}
      </div>
    </div>
  );
}

function fmtDate(value) {
  if (!value) return "بدون انقضا";
  try {
    return new Intl.DateTimeFormat("fa-IR", { dateStyle: "medium" }).format(new Date(value));
  } catch {
    return "-";
  }
}

const STATUS = {
  active: ["فعال", "text-[color:var(--mi-emerald)] bg-emerald-500/10"],
  disabled: ["غیرفعال", "text-[color:var(--mi-red)] bg-red-500/10"],
  quota_exceeded: ["اتمام حجم", "text-[color:var(--mi-amber)] bg-amber-500/10"],
  expired: ["منقضی", "text-[color:var(--tg-theme-hint-color,#9ca3af)] bg-[var(--mi-soft)]"],
};

/** Whole days until `value`, or null for no expiry / unparseable. */
function daysUntil(value) {
  if (!value) return null;
  const ms = new Date(value).getTime() - Date.now();
  if (Number.isNaN(ms)) return null;
  return Math.ceil(ms / 86400000);
}

function ServiceCard({ service, onRenew }) {
  const used = service.used_bytes || 0;
  const total = service.quota_bytes || 0;
  const pct = total ? Math.min(100, Math.round((used / total) * 100)) : 0;
  const [label, colour] = STATUS[service.status] || [service.status, "text-[color:var(--mi-amber)] bg-[var(--mi-soft)]"];
  const reserved = (service.reserved_quota_bytes || 0) + (service.reserved_duration_days || 0);
  const days = daysUntil(service.expire_at);

  // What the customer most needs to notice on this card, in one line, in
  // words - not only in a bar colour. The renew button below is promoted
  // exactly when this fires, so the screen leads with the next action instead
  // of making them work out from three numbers that it is time.
  const attention =
    service.status === "expired" || (days !== null && days <= 0)
      ? "این سرویس منقضی شده است."
      : service.status === "quota_exceeded" || (total && pct >= 100)
        ? "حجم این سرویس تمام شده است."
        : days !== null && days <= 3
          ? `فقط ${fa(days)} روز به پایان سرویس مانده است.`
          : total && pct >= 90
            ? "حجم این سرویس رو به اتمام است."
            : "";

  return (
    <Card className="mb-3">
      <div className="flex items-start gap-3">
        <IconTile icon={Layers} tone="sky" />
        <div className="min-w-0 flex-1">
          <div className="flex items-start justify-between gap-2">
            <div className="font-bold text-[15px] truncate">{service.name}</div>
            <span className={`text-xs px-2 py-1 rounded-lg shrink-0 ${colour}`}>{label}</span>
          </div>
          <div className="text-xs opacity-60 mt-1">
            انقضا: {fmtDate(service.expire_at)}
            {days !== null && days > 0 ? ` · ${fa(days)} روز مانده` : ""}
          </div>
        </div>
      </div>

      <div className="mt-4">
        <div
          role="progressbar"
          aria-label="حجم مصرف‌شده"
          aria-valuemin={0}
          aria-valuemax={total || 100}
          aria-valuenow={total ? Math.min(used, total) : 0}
          className="h-2 rounded-full bg-[var(--mi-soft)] overflow-hidden"
        >
          <div
            className={`h-full rounded-full ${
              pct > 90 ? "bg-red-400" : pct > 70 ? "bg-amber-400" : "bg-sky-400"
            }`}
            style={{ width: total ? `${pct}%` : "100%", opacity: total ? 1 : 0.35 }}
          />
        </div>
        <div className="flex items-center justify-between text-xs opacity-65 mt-2">
          <span dir="ltr">
            {total ? `${formatBytes(used)} / ${formatBytes(total)}` : formatBytes(used)}
          </span>
          <span>{total ? `${fa(pct)}٪` : "نامحدود"}</span>
        </div>
      </div>

      {attention && (
        <div className="text-xs text-[color:var(--mi-amber)] bg-amber-500/10 rounded-lg px-3 py-2 mt-3 flex items-center gap-2">
          <AlertTriangle size={14} className="shrink-0" />
          {attention}
        </div>
      )}

      {/* A renewal already paid for, waiting for this one to run out. Without
          it, a customer who has just renewed sees nothing change and buys
          again. */}
      {reserved > 0 && (
        <div className="text-xs text-[color:var(--mi-sky)] mt-3 bg-sky-500/10 rounded-lg px-3 py-2 flex items-center gap-2">
          <Clock size={14} className="shrink-0" />
          <span>
            تمدید رزروشده
            {service.reserved_quota_bytes ? ` · ${formatBytes(service.reserved_quota_bytes)}` : ""}
            {service.reserved_duration_days ? ` · ${fa(service.reserved_duration_days)} روز` : ""}
          </span>
        </div>
      )}

      {service.connection_count > 0 && (
        <div className="text-xs opacity-60 mt-3">{fa(service.connection_count)} اتصال</div>
      )}

      {/* Renewal continues THIS service (same connections, same links) - the
          bot's «تمدید سرویس». Solid when the card has just said something is
          wrong, quiet otherwise. */}
      <button
        type="button"
        onClick={() => onRenew(service)}
        className={`w-full mt-4 min-h-[44px] rounded-xl text-sm font-medium flex items-center justify-center gap-2 ${
          attention
            ? "bg-sky-500 active:bg-sky-600 text-white"
            : "bg-sky-500/15 text-[color:var(--mi-sky)] active:bg-sky-500/25"
        }`}
      >
        <RefreshCw size={15} />
        تمدید این سرویس
      </button>
    </Card>
  );
}

/**
 * The bottom sheet all three flows share (renewal plan, checkout, top-up).
 *
 * It was written out three times, which is how the three drifted: a close
 * button ~28px square and unlabelled, no dialog semantics for a screen
 * reader, the page behind still scrolling under the thumb, no way to
 * dismiss it with Telegram's own back button, and a bottom padding that
 * ignored the phone's home indicator. Fixed once, here.
 *
 * `dismissible` is false while a request is in flight: closing then would
 * leave the customer unsure whether the payment went through.
 */
function Sheet({ title, subtitle, onClose, dismissible = true, children }) {
  const closeRef = useRef(onClose);
  closeRef.current = dismissible ? onClose : () => {};

  useEffect(() => {
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";

    const onKey = (e) => {
      if (e.key === "Escape") closeRef.current();
    };
    document.addEventListener("keydown", onKey);

    // Telegram's own back arrow closes the sheet - what a phone user reaches
    // for first. Guarded: on a webview where the script never arrived there
    // is no BackButton and nothing here may throw.
    const back = window.Telegram?.WebApp?.BackButton;
    const onBack = () => closeRef.current();
    try {
      back?.show?.();
      back?.onClick?.(onBack);
    } catch {
      /* older client - the visible close button still works */
    }

    return () => {
      document.body.style.overflow = previous;
      document.removeEventListener("keydown", onKey);
      try {
        back?.offClick?.(onBack);
        back?.hide?.();
      } catch {
        /* see above */
      }
    };
  }, []);

  return (
    <div className="fixed inset-0 z-50 flex items-end" dir="rtl">
      <div className="mi-fade absolute inset-0 bg-black/60" onClick={dismissible ? onClose : undefined} />
      <div
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className="mi-sheet relative w-full max-w-lg mx-auto rounded-t-3xl border-t border-[color:var(--mi-line)] px-5 pt-3 max-h-[88vh] overflow-y-auto overscroll-contain"
        style={{ background: "var(--tg-theme-secondary-bg-color,#151b23)", paddingBottom: SAFE_BOTTOM }}
      >
        <div className="w-10 h-1 rounded-full bg-[var(--mi-line)] mx-auto mb-3" />
        <div className="flex items-start justify-between gap-3 mb-4">
          <div className="min-w-0 pt-1.5">
            <div className="font-bold">{title}</div>
            {subtitle && <div className="text-xs opacity-60 mt-1">{subtitle}</div>}
          </div>
          <button
            type="button"
            onClick={onClose}
            disabled={!dismissible}
            aria-label="بستن"
            className="shrink-0 w-11 h-11 -mt-1 -me-2 rounded-full flex items-center justify-center opacity-60 active:bg-[var(--mi-soft)] disabled:opacity-25"
          >
            <X size={20} />
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}

/**
 * The card number, with something to do about it. It used to be text with
 * `select-all` - which in a phone webview means a long-press, a drag of the
 * selection handles and a menu, for the one string the customer has to move
 * into their banking app to pay. One tap now.
 */
function CardNumber({ number, holder, amountHint }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(String(number).replace(/\s+/g, ""));
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      /* clipboard blocked - the number is still on screen and selectable */
    }
  };
  return (
    <div className="rounded-xl bg-[var(--mi-card)] border border-[color:var(--mi-line)] p-3.5 text-sm mb-3">
      {amountHint && <div className="text-xs opacity-60 mb-2">{amountHint}</div>}
      <div className="flex items-center justify-between gap-3">
        <div dir="ltr" className="font-mono text-base tracking-wider select-all">{number}</div>
        <button
          type="button"
          onClick={copy}
          aria-label="کپی شماره کارت"
          className="shrink-0 min-h-[44px] px-3 rounded-lg bg-[var(--mi-soft)] active:opacity-70 text-xs flex items-center gap-1.5"
        >
          {copied ? <Check size={14} className="text-[color:var(--mi-emerald)]" /> : <Copy size={14} />}
          {copied ? "کپی شد" : "کپی"}
        </button>
      </div>
      {holder && <div className="opacity-70 text-xs mt-2">{holder}</div>}
    </div>
  );
}

/**
 * «کدام پلن؟» for a renewal. Every plan this shop sells, flat - a renewal
 * queues a plan behind the service the customer already has, so unlike a new
 * purchase it does not matter whether the plan bundles services (the bot's
 * renewal list does not filter either). Picking one hands over to the same
 * CheckoutSheet a purchase uses.
 */
function RenewPicker({ service, packages, onPick, onClose }) {
  return (
    <Sheet title={`تمدید ${service.name}`} subtitle="پلن تمدید را انتخاب کنید." onClose={onClose}>
      {packages.length === 0 && (
        <div className="text-sm opacity-60 text-center py-6">پلنی برای تمدید موجود نیست.</div>
      )}
      <div className="space-y-2">
        {packages.map((p) => (
          <button
            key={p.id}
            type="button"
            onClick={() => onPick(p)}
            className="w-full min-h-[56px] rounded-xl bg-[var(--mi-card)] border border-[color:var(--mi-line)] active:bg-[var(--mi-soft)] p-3.5 flex items-center justify-between gap-3 text-start"
          >
            <div className="min-w-0">
              <div className="text-sm font-medium truncate">{p.name}</div>
              <div className="text-xs opacity-60 mt-1">
                {p.quota_gb ? `${fa(p.quota_gb)} گیگابایت` : "حجم نامحدود"}
                {" · "}
                {p.duration_days ? `${fa(p.duration_days)} روز` : "بدون انقضا"}
              </div>
            </div>
            <span className="shrink-0 text-xs bg-sky-500/15 text-[color:var(--mi-sky)] rounded-lg px-2.5 py-1.5">
              {formatToman(p.price || 0, "fa")} تومان
            </span>
          </button>
        ))}
      </div>
    </Sheet>
  );
}

/**
 * Checkout. One package, one sheet, two ways to pay - the two the bot
 * already offers, so a sale made here lands in exactly the same places.
 *
 * The wallet button is only offered when the balance actually covers the
 * price. An enabled button that answers «موجودی کافی نیست» is a worse way
 * of saying the same thing, one tap later.
 */
function CheckoutSheet({ pkg, wallet, account, payment, initData, renew, onClose, onDone }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [file, setFile] = useState(null);
  const fileInput = useRef(null);
  const price = pkg.price || 0;
  const canPayFromWallet = wallet >= price && Boolean(account);

  const fail = (err) => setError(err?.response?.data?.detail || "انجام نشد. دوباره تلاش کنید.");

  const payFromWallet = async () => {
    setBusy(true);
    setError("");
    try {
      const res = await miniAppCheckout(initData, {
        package_id: pkg.id, account, renew_purchase_id: renew?.id,
      });
      onDone(res.data.message, res.data.status);
    } catch (err) {
      fail(err);
    } finally {
      setBusy(false);
    }
  };

  const sendReceipt = async () => {
    if (!file) return;
    setBusy(true);
    setError("");
    try {
      const res = await miniAppCheckoutReceipt(initData, {
        packageId: pkg.id, account, file, renewPurchaseId: renew?.id,
      });
      onDone(res.data.message, res.data.status);
    } catch (err) {
      fail(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Sheet
      title={renew ? `تمدید ${renew.name}` : pkg.name}
      subtitle={`${renew ? `${pkg.name} · ` : ""}${formatToman(price, "fa")} تومان`}
      onClose={onClose}
      dismissible={!busy}
    >
      <div className="rounded-xl bg-[var(--mi-card)] px-3 py-3 text-xs flex items-center justify-between mb-4">
        <span className="opacity-60">موجودی کیف پول</span>
        <span>{formatToman(wallet, "fa")} تومان</span>
      </div>

      {canPayFromWallet && (
        <button
          type="button"
          onClick={payFromWallet}
          disabled={busy}
          className="w-full min-h-[48px] rounded-xl bg-emerald-500 active:bg-emerald-600 disabled:opacity-50 text-white text-sm font-medium flex items-center justify-center gap-2"
        >
          {busy ? <Loader2 size={16} className="animate-spin" /> : <Check size={16} />}
          پرداخت از کیف پول
        </button>
      )}

      <div className={canPayFromWallet ? "mt-5 pt-5 border-t border-[color:var(--mi-line)]" : ""}>
        <div className="text-sm font-medium mb-3">
          {canPayFromWallet ? "یا پرداخت کارت‌به‌کارت" : "پرداخت کارت‌به‌کارت"}
        </div>
        {!canPayFromWallet && wallet > 0 && (
          <div className="text-xs text-[color:var(--mi-amber)] bg-amber-500/10 rounded-lg px-3 py-2 mb-3">
            موجودی کیف پول برای این پلن کافی نیست.
          </div>
        )}

        {payment?.payment_card_number ? (
          <CardNumber
            number={payment.payment_card_number}
            holder={payment.payment_card_holder}
            amountHint={`مبلغ ${formatToman(price, "fa")} تومان را به این کارت واریز کنید:`}
          />
        ) : (
          <div className="text-xs opacity-60 mb-3">
            هنوز روش پرداخت تنظیم نشده - با پشتیبانی تماس بگیرید.
          </div>
        )}

        {/* A plain file input is unstyleable and, in Telegram's webview,
            easy to miss entirely. The button drives it instead. */}
        <input
          ref={fileInput}
          type="file"
          accept="image/*"
          className="hidden"
          onChange={(e) => {
            setFile(e.target.files?.[0] || null);
            setError("");
          }}
        />
        <button
          type="button"
          onClick={() => fileInput.current?.click()}
          disabled={busy}
          className={`w-full min-h-[48px] rounded-xl border text-sm flex items-center justify-center gap-2 disabled:opacity-50 ${
            file
              ? "border-emerald-500/40 text-[color:var(--mi-emerald)] bg-emerald-500/5"
              : "border-[color:var(--mi-line)]"
          }`}
        >
          {file ? <Check size={15} /> : <Upload size={15} />}
          <span className="truncate max-w-[70%]">{file ? file.name : "انتخاب عکس رسید"}</span>
        </button>

        <button
          type="button"
          onClick={sendReceipt}
          disabled={busy || !file}
          className="w-full mt-2 min-h-[48px] rounded-xl bg-sky-500 active:bg-sky-600 disabled:opacity-40 text-white text-sm font-medium flex items-center justify-center gap-2"
        >
          {busy ? <Loader2 size={16} className="animate-spin" /> : null}
          ارسال رسید
        </button>
        {!file && (
          <div className="text-xs opacity-60 mt-2 text-center">
            بعد از واریز، عکس رسید را انتخاب کنید تا دکمه فعال شود.
          </div>
        )}
      </div>

      {error && (
        <div role="alert" className="text-xs text-[color:var(--mi-red)] bg-red-500/10 rounded-lg px-3 py-2.5 mt-4">
          {error}
        </div>
      )}
    </Sheet>
  );
}


/**
 * Adding credit to the wallet.
 *
 * The wallet tab used to show a card number under the heading «افزایش
 * اعتبار» and stop - a label where an action belongs, so there was
 * nothing to press. Reported, again accurately, as «دکمه افزایش اعتبار
 * کار نمیکنه».
 *
 * Same two steps as the card-to-card half of checkout, in the same order:
 * how much, then the receipt. The amount is asked FIRST because it is what
 * the customer has to type into their bank app, and the card number is
 * shown beside it rather than on a previous screen they would have to go
 * back to.
 */
function TopupSheet({ wallet, account, payment, initData, onClose, onDone }) {
  const [amount, setAmount] = useState("");
  const [file, setFile] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const fileInput = useRef(null);

  // The reseller's own quick amounts (PanelSettings.topup_presets), the
  // same list the bot offers. Parsed exactly as the bot parses it, so the
  // two never disagree about what the admin typed.
  const presets = String(payment?.topup_presets || "")
    .split(",")
    .map((part) => part.trim())
    .filter((part) => /^\d+$/.test(part))
    .map(Number);

  const value = Number(amount) || 0;

  const send = async () => {
    if (!value || !file) return;
    setBusy(true);
    setError("");
    try {
      const res = await miniAppTopupReceipt(initData, { amount: value, account, file });
      onDone(res.data.message, res.data.status);
    } catch (err) {
      setError(err?.response?.data?.detail || "انجام نشد. دوباره تلاش کنید.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Sheet
      title="افزایش اعتبار"
      subtitle={`موجودی فعلی: ${formatToman(wallet, "fa")} تومان`}
      onClose={onClose}
      dismissible={!busy}
    >
      <label htmlFor="mi-topup-amount" className="block text-sm font-medium mb-2">چه مبلغی؟</label>
      {presets.length > 0 && (
        <div className="flex gap-2 overflow-x-auto pb-1 mb-2">
          {presets.map((preset) => (
            <button
              key={preset}
              type="button"
              onClick={() => setAmount(String(preset))}
              aria-pressed={value === preset}
              className={`shrink-0 min-h-[40px] px-3.5 rounded-xl text-xs font-medium ${
                value === preset ? "bg-sky-500 text-white" : "bg-[var(--mi-soft)] opacity-80"
              }`}
            >
              {formatToman(preset, "fa")}
            </button>
          ))}
        </div>
      )}
      {/* inputMode rather than type="number": a numeric keypad without the
          spinner arrows, and without a browser quietly reformatting what
          was typed. */}
      <input
        id="mi-topup-amount"
        className="w-full min-h-[48px] rounded-xl bg-[var(--mi-card)] px-3.5 text-sm outline-none border border-[color:var(--mi-line)] focus:border-sky-500 placeholder:opacity-50"
        inputMode="numeric"
        placeholder="مبلغ به تومان"
        value={amount}
        disabled={busy}
        onChange={(e) => {
          setAmount(e.target.value.replace(/[^0-9]/g, ""));
          setError("");
        }}
      />
      {value > 0 && (
        <div className="text-xs opacity-60 mt-2">{formatToman(value, "fa")} تومان</div>
      )}

      <div className="mt-5 pt-5 border-t border-[color:var(--mi-line)]">
        {payment?.payment_card_number ? (
          <CardNumber
            number={payment.payment_card_number}
            holder={payment.payment_card_holder}
            amountHint="مبلغ را به این کارت واریز کنید:"
          />
        ) : (
          <div className="text-xs opacity-60 mb-3">
            هنوز روش پرداخت تنظیم نشده - با پشتیبانی تماس بگیرید.
          </div>
        )}

        <input
          ref={fileInput}
          type="file"
          accept="image/*"
          className="hidden"
          onChange={(e) => {
            setFile(e.target.files?.[0] || null);
            setError("");
          }}
        />
        <button
          type="button"
          onClick={() => fileInput.current?.click()}
          disabled={busy}
          className={`w-full min-h-[48px] rounded-xl border text-sm flex items-center justify-center gap-2 disabled:opacity-50 ${
            file
              ? "border-emerald-500/40 text-[color:var(--mi-emerald)] bg-emerald-500/5"
              : "border-[color:var(--mi-line)]"
          }`}
        >
          {file ? <Check size={15} /> : <Upload size={15} />}
          <span className="truncate max-w-[70%]">{file ? file.name : "انتخاب عکس رسید"}</span>
        </button>

        <button
          type="button"
          onClick={send}
          disabled={busy || !value || !file}
          className="w-full mt-2 min-h-[48px] rounded-xl bg-sky-500 active:bg-sky-600 disabled:opacity-40 text-white text-sm font-medium flex items-center justify-center gap-2"
        >
          {busy ? <Loader2 size={16} className="animate-spin" /> : null}
          ارسال رسید
        </button>
        {(!value || !file) && (
          <div className="text-xs opacity-60 mt-2 text-center">
            {!value ? "مبلغ را وارد کنید" : "عکس رسید را انتخاب کنید"} تا دکمه فعال شود.
          </div>
        )}
      </div>

      {error && (
        <div role="alert" className="text-xs text-[color:var(--mi-red)] bg-red-500/10 rounded-lg px-3 py-2.5 mt-4">
          {error}
        </div>
      )}
    </Sheet>
  );
}

// -------------------------------------------------------------------- page

export default function MiniApp() {
  const { webApp, settled } = useTelegram();
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [tab, setTab] = useState("shop");
  const [buying, setBuying] = useState(null);
  // The service being renewed, when `buying` is a renewal rather than a new
  // purchase; `pickingRenewal` is the step before that (which plan?).
  const [renewTarget, setRenewTarget] = useState(null);
  const [pickingRenewal, setPickingRenewal] = useState(null);
  const [toppingUp, setToppingUp] = useState(false);
  const [claiming, setClaiming] = useState(null);
  const [done, setDone] = useState("");
  // "ok" | "pending" | "error" - so a failure is not dressed up as a success.
  // takePackage's error path used to put the server's refusal in the same
  // green-tick dialog a completed purchase gets.
  const [doneTone, setDoneTone] = useState("ok");
  const notify = (text, tone = "ok") => {
    setDone(text);
    setDoneTone(tone);
  };

  // Read once, from the URL, and kept for the retry button. Not derived
  // from webApp: see initDataFromUrl on why the page must not wait for
  // telegram.org.
  const [initData] = useState(() => initDataFromUrl());

  // The panel's own stylesheet gives <body> a light background, which showed
  // as a pale strip down the side of the dark app wherever our container did
  // not reach. Painted on the document itself, and only on this route.
  //
  // ITS OWN EFFECT, deliberately. It was originally folded into the loading
  // effect below, where its cleanup `return` sat ABOVE the fetch - so the
  // effect returned before ever calling load(), `data` stayed null, and the
  // page showed «در حال بارگذاری» for ever. An effect that both paints and
  // fetches has a return statement in the middle of it waiting to swallow
  // whatever gets added next; two effects cannot.
  useEffect(() => {
    const previous = document.body.style.background;
    document.body.style.background = BG;
    return () => {
      document.body.style.background = previous;
    };
  }, []);

  useEffect(() => {
    // Wait only for the script's fate to be decided, never for the script
    // itself to succeed.
    if (!settled) return;
    webApp?.ready?.();
    webApp?.expand?.();

    const credential = initData || webApp?.initData || "";
    if (!credential) {
      setError("این صفحه را از داخل ربات تلگرام باز کنید.");
      return;
    }
    load(credential);
  }, [settled, webApp, initData]);

  /**
   * Buying, and the case where there is nothing to buy.
   *
   * A free plan goes straight to the endpoint - no sheet, because a sheet
   * that offers a wallet and a card number for a zero-toman plan is a form
   * with no question in it, and it was the only thing between the customer
   * and the sample. Everything else opens checkout as before.
   */
  const takePackage = (pkg, opts) => {
    if (!opts?.free) {
      setBuying(pkg);
      return;
    }
    setClaiming(pkg.id);
    miniAppCheckout(initData || webApp?.initData || "", { package_id: pkg.id })
      .then((res) => {
        notify(res.data.message);
        load(initData || webApp?.initData || "");
      })
      .catch((err) => notify(err?.response?.data?.detail || "انجام نشد. دوباره تلاش کنید.", "error"))
      .finally(() => setClaiming(null));
  };

  const load = (initData) => {
    setError("");
    fetchMiniAppHome(initData)
      .then((r) => setData(r.data))
      .catch((err) => {
        // The status matters here in a way it usually does not. This page
        // has three quite different failures that all look identical to a
        // customer staring at a screen with nothing to tap: the panel has
        // not been updated yet so the endpoint is not there (404), the
        // signature was refused (401), or the server is simply down. Saying
        // which turns "the app does not work" into something answerable.
        const status = err?.response?.status;
        const detail = err?.response?.data?.detail;
        if (status === 404) {
          setError("این پنل هنوز به‌روزرسانی نشده است - نسخه‌ی جدید را نصب کنید.");
        } else if (detail) {
          setError(detail);
        } else {
          setError("اتصال به سرور برقرار نشد.");
        }
      });
  };

  // The shelves, exactly as the server arranged them. No grouping is done
  // here on purpose: which plans this shop sells, which the Mini App may
  // show, which shelves are on and in what order are all decided in
  // routers/miniapp.py's _shop_shelves, and re-deriving any of it here
  // would be the same rules written twice, free to disagree.
  const shelves = data?.shop?.groups || [];
  // Telegram's own answer, not the OS's: the two disagree whenever the
  // customer picked a theme inside Telegram. Dark until Telegram says
  // otherwise, which is also what a webview with no script gets.
  const scheme = webApp?.colorScheme === "light" ? "light" : "dark";

  if (error) {
    return (
      <Shell scheme={scheme} center>
        <Card className="text-center max-w-sm">
          <AlertTriangle className="mx-auto mb-3 text-[color:var(--mi-amber)]" size={28} />
          <span className="sr-only">خطا</span>
          <div className="text-sm">{error}</div>
          {/* Something to tap. An error screen with no control on it is
              indistinguishable from a frozen app - which is exactly how the
              first version was reported ("هیچ کلیکی نمیشه"). */}
          <button
            type="button"
            className="mt-4 min-h-[44px] px-5 rounded-xl bg-sky-500 active:bg-sky-600 text-white text-sm"
            onClick={() => load(initData || webApp?.initData || "")}
          >
            تلاش دوباره
          </button>

          {/* What the page actually found, for when it did not work. Three
              rounds of "it does not open" went by without anyone being able
              to say WHY, because the only evidence lived in a console nobody
              could reach from inside Telegram's webview. Names and lengths
              only - never the signature itself, which is a credential. */}
          <details className="mt-4 text-start">
            <summary className="text-xs opacity-60 cursor-pointer min-h-[44px] flex items-center">جزئیات فنی</summary>
            <div className="text-[11px] opacity-70 mt-2 space-y-0.5 font-mono" dir="ltr">
              <div>telegram script: {webApp ? "loaded" : settled ? "absent" : "pending"}</div>
              <div>initData in url: {initData ? `${initData.length} chars` : "no"}</div>
              <div>initData from script: {webApp?.initData ? `${webApp.initData.length} chars` : "no"}</div>
              <div>url keys: {debugUrlKeys() || "(none)"}</div>
              <div>platform: {webApp?.platform || "unknown"}</div>
            </div>
          </details>
        </Card>
      </Shell>
    );
  }

  if (!data) {
    // The page's own shape, greyed, instead of a lone spinner on a blank
    // screen: on the mobile networks this is sold into the first response can
    // take seconds, and a layout that is already there reads as "loading",
    // where an empty screen reads as "broken".
    return (
      <Shell scheme={scheme}>
        <div className="px-4 pt-4 max-w-lg mx-auto" role="status" aria-live="polite" aria-label="در حال بارگذاری">
          <div className="flex items-center justify-between mb-6">
            <div className="flex items-center gap-2.5">
              <div className="mi-skel w-9 h-9 rounded-full" />
              <div className="mi-skel h-4 w-24 rounded" />
            </div>
            <div className="mi-skel h-9 w-24 rounded-full" />
          </div>
          <div className="mi-skel h-7 w-40 rounded mb-2" />
          <div className="mi-skel h-3 w-56 rounded mb-6" />
          <div className="mi-skel h-24 rounded-2xl mb-4" />
          <div className="mi-skel h-56 rounded-2xl" />
        </div>
      </Shell>
    );
  }

  const tabs = [
    { id: "shop", label: "فروشگاه", icon: Store },
    { id: "services", label: "سرویس‌های من", icon: Layers },
    { id: "wallet", label: "کیف پول", icon: Wallet },
  ];
  const shopName = data.shop.title || "فروشگاه";

  return (
    <Shell scheme={scheme}>
      <div className="px-4 pt-4 pb-32 max-w-lg mx-auto">
        {/* Top bar: who this shop is, and what the customer has in it. */}
        <div className="flex items-center justify-between mb-6">
          <div className="flex items-center gap-2.5">
            <div className="w-9 h-9 rounded-full bg-sky-500/15 text-[color:var(--mi-sky)] flex items-center justify-center text-sm font-bold">
              {shopName.slice(0, 1)}
            </div>
            <div className="text-sm font-medium">{shopName}</div>
          </div>
          <button
            type="button"
            onClick={() => setTab("wallet")}
            aria-label={`کیف پول: ${formatToman(data.me.wallet, "fa")} تومان`}
            className="flex items-center gap-1.5 text-xs bg-[var(--mi-soft)] rounded-full ps-3.5 pe-3 min-h-[44px]"
          >
            {formatToman(data.me.wallet, "fa")}
            <Wallet size={14} className="opacity-60" />
          </button>
        </div>

        {tab === "shop" && (
          <>
            <PageTitle
              title={data.me.name ? `سلام ${data.me.name}` : "فروشگاه"}
              subtitle="پلن مورد نظر خود را انتخاب کنید."
            />

            <TrialBanner
              trial={data.shop.trial}
              busy={claiming === data.shop.trial?.id}
              onTake={(pkg) => takePackage(pkg, { free: true })}
            />

            <ReferralCard
              code={data.me.accounts?.[0]?.referral_code}
              payment={data.payment}
              botUsername={data.shop.bot_username}
            />

            {shelves.length === 0 && (
              <Card className="text-center text-sm opacity-50 py-8">
                هنوز پلنی برای فروش تعریف نشده.
              </Card>
            )}

            {shelves.map((shelf) => (
              // Keyed by the shelf, so each card keeps its OWN selection.
              // Sharing one PackagePicker across cards would have a tap on
              // one card silently change what another is showing.
              <div key={shelf.id ?? "ungrouped"} className="mb-5">
                {shelf.name && (
                  <div className="px-1 mb-2.5">
                    <div className="text-sm font-bold">{shelf.name}</div>
                    {shelf.description && (
                      <div className="text-xs opacity-60 mt-1 leading-relaxed">
                        {shelf.description}
                      </div>
                    )}
                  </div>
                )}
                <PackagePicker packages={shelf.packages} onBuy={takePackage} />
              </div>
            ))}
          </>
        )}

        {tab === "services" && (
          <>
            <PageTitle
              title="سرویس‌های من"
              subtitle="حجم مصرفی و تاریخ انقضای هر سرویس."
            />
            {data.services.length === 0 && (
              <Card className="text-center py-10">
                <ShoppingBag className="mx-auto mb-3 opacity-25" size={30} />
                <div className="text-sm opacity-50">هنوز سرویسی ندارید.</div>
                <button
                  type="button"
                  onClick={() => setTab("shop")}
                  className="mt-4 min-h-[48px] px-6 rounded-xl bg-sky-500 active:bg-sky-600 text-white text-sm"
                >
                  رفتن به فروشگاه
                </button>
              </Card>
            )}
            {data.services.map((service) => (
              <ServiceCard key={service.id} service={service} onRenew={setPickingRenewal} />
            ))}
          </>
        )}

        {tab === "wallet" && (
          <>
            <PageTitle title="کیف پول" subtitle="موجودی شما و راه افزایش آن." />
            <Card className="mb-3 text-center py-7">
              <div className="text-xs opacity-60">موجودی</div>
              <div className="text-3xl font-bold mt-2">{formatToman(data.me.wallet, "fa")}</div>
              <div className="text-xs opacity-60 mt-1">تومان</div>
            </Card>
            <Card>
              <div className="flex items-start gap-3">
                <IconTile icon={Wallet} tone="emerald" />
                <div className="min-w-0 flex-1">
                  <div className="font-bold text-[15px]">افزایش اعتبار</div>
                  <div className="text-xs opacity-60 mt-1">
                    کارت‌به‌کارت کنید و عکس رسید را همین‌جا بفرستید.
                  </div>
                </div>
              </div>
              {/* An actual button. This card used to end at the heading
                  above plus a card number - a label where an action
                  belongs, so there was nothing to press. */}
              <button
                type="button"
                onClick={() => setToppingUp(true)}
                className="w-full mt-4 min-h-[48px] rounded-xl bg-emerald-500 active:bg-emerald-600 text-white text-sm font-medium flex items-center justify-center gap-2"
              >
                <Plus size={16} />
                افزایش اعتبار
              </button>
              {data.payment?.payment_card_number && (
                <div className="mt-3">
                  <CardNumber
                    number={data.payment.payment_card_number}
                    holder={data.payment.payment_card_holder}
                    amountHint="شماره کارت"
                  />
                </div>
              )}
            </Card>
          </>
        )}
      </div>

      {/* Floating, not flush. A bar pinned to the very bottom edge collides
          with the phone's own home indicator; lifting it off the edge is
          what makes it look like part of an app rather than part of the
          page. Telegram puts its own chrome at the TOP, which is why the
          navigation goes down here where a thumb reaches it. */}
      <div className="fixed bottom-0 inset-x-0 px-4 pointer-events-none" style={{ paddingBottom: SAFE_BOTTOM }}>
        <nav
          aria-label="بخش‌های برنامه"
          className="max-w-xs mx-auto rounded-2xl border border-[color:var(--mi-line)] grid grid-cols-3 p-1.5 pointer-events-auto shadow-2xl shadow-black/30"
          style={{ background: "var(--tg-theme-secondary-bg-color,#1a212b)" }}
        >
          {tabs.map(({ id, label, icon: Icon }) => (
            <button
              key={id}
              type="button"
              onClick={() => setTab(id)}
              aria-current={tab === id ? "page" : undefined}
              className={`min-h-[52px] rounded-xl flex flex-col items-center justify-center gap-1 text-xs active:bg-[var(--mi-soft)] ${
                tab === id ? "bg-sky-500/15 text-[color:var(--mi-sky)] font-medium" : "opacity-65"
              }`}
            >
              <Icon size={18} />
              {label}
            </button>
          ))}
        </nav>
      </div>

      {pickingRenewal && (
        <RenewPicker
          service={pickingRenewal}
          packages={shelves.flatMap((shelf) => shelf.packages || [])}
          onClose={() => setPickingRenewal(null)}
          onPick={(pkg) => {
            setRenewTarget(pickingRenewal);
            setPickingRenewal(null);
            setBuying(pkg);
          }}
        />
      )}

      {buying && (
        <CheckoutSheet
          pkg={buying}
          renew={renewTarget}
          // A renewal is paid from, and belongs to, the account that OWNS the
          // service - which is not necessarily the first one when the same
          // Telegram id holds several - so that account's own balance is what
          // decides whether the wallet button is offered.
          wallet={
            renewTarget
              ? (data.me.accounts || []).find((a) => a.username === renewTarget.account)?.balance ?? 0
              : data.me.wallet
          }
          account={renewTarget ? renewTarget.account : data.me.accounts?.[0]?.username || ""}
          payment={data.payment}
          initData={initData || webApp?.initData || ""}
          onClose={() => { setBuying(null); setRenewTarget(null); }}
          onDone={(message, status) => {
            setBuying(null);
            setRenewTarget(null);
            notify(message, status === "pending" ? "pending" : "ok");
            // Refetch rather than patch the state by hand: the wallet, the
            // services list and the one-time-package rules all moved, and
            // guessing which is how a screen ends up disagreeing with the
            // server about what the customer owns.
            load(initData || webApp?.initData || "");
          }}
        />
      )}

      {toppingUp && (
        <TopupSheet
          wallet={data.me.wallet}
          account={data.me.accounts?.[0]?.username || ""}
          payment={data.payment}
          initData={initData || webApp?.initData || ""}
          onClose={() => setToppingUp(false)}
          onDone={(message, status) => {
            setToppingUp(false);
            notify(message, status === "pending" ? "pending" : "ok");
            load(initData || webApp?.initData || "");
          }}
        />
      )}

      {done && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-6" dir="rtl">
          <div className="mi-fade absolute inset-0 bg-black/60" onClick={() => setDone("")} />
          <div
            role="alertdialog"
            aria-modal="true"
            aria-label={doneTone === "error" ? "خطا" : "نتیجه"}
            className="mi-sheet relative rounded-2xl border border-[color:var(--mi-line)] p-6 text-center max-w-xs w-full"
            style={{ background: "var(--tg-theme-secondary-bg-color,#151b23)" }}
          >
            <div
              className={`w-14 h-14 rounded-full flex items-center justify-center mx-auto mb-4 ${
                doneTone === "error"
                  ? "bg-amber-500/15 text-[color:var(--mi-amber)]"
                  : doneTone === "pending"
                    ? "bg-sky-500/15 text-[color:var(--mi-sky)]"
                    : "bg-emerald-500/15 text-[color:var(--mi-emerald)]"
              }`}
            >
              {doneTone === "error" ? <AlertTriangle size={26} /> : doneTone === "pending" ? <Clock size={26} /> : <Check size={26} />}
            </div>
            <div className="text-sm leading-relaxed">{done}</div>
            <button
              type="button"
              className="mt-5 w-full min-h-[48px] rounded-xl bg-sky-500 active:bg-sky-600 text-white text-sm"
              onClick={() => setDone("")}
            >
              باشه
            </button>
          </div>
        </div>
      )}
    </Shell>
  );
}
