import React, { useEffect, useState } from "react";
import { Package, Wallet, Layers, AlertTriangle } from "lucide-react";
import { fetchMiniAppHome } from "../api/client.js";
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
 * they are read from its CSS variables with the panel's own as a fallback.
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

function Card({ children, className = "" }) {
  return (
    <div className={`rounded-2xl bg-white/5 border border-white/10 p-4 ${className}`}>{children}</div>
  );
}

function PackageCard({ pkg }) {
  const services = [...new Set((pkg.connections || []).map((c) => c.protocol))];
  return (
    <Card className="mb-3">
      <div className="font-bold">{pkg.name}</div>

      <div className="flex flex-wrap gap-x-3 gap-y-1 text-xs opacity-60 mt-2">
        <span>{pkg.quota_gb ? `${fa(pkg.quota_gb)} گیگابایت` : "نامحدود"}</span>
        <span>·</span>
        <span>{pkg.duration_days ? `${fa(pkg.duration_days)} روز` : "بدون انقضا"}</span>
        {pkg.max_concurrent_sessions > 0 && (
          <>
            <span>·</span>
            <span>{fa(pkg.max_concurrent_sessions)} کاربر همزمان</span>
          </>
        )}
      </div>

      {services.length > 0 && (
        <div className="flex flex-wrap gap-1.5 mt-2">
          {services.map((protocol) => (
            <span key={protocol} className="text-[10px] px-2 py-0.5 rounded-full bg-white/10 opacity-80">
              {protocol}
            </span>
          ))}
        </div>
      )}

      {pkg.description && <p className="text-xs opacity-70 mt-2 leading-relaxed">{pkg.description}</p>}

      {/* Its own row rather than squeezed beside the name. The name is the
          long part and the price is what the eye looks for; on a phone, in
          RTL, the two fought each other for the same line. */}
      <div className="flex items-baseline justify-between gap-2 mt-3 pt-3 border-t border-white/10">
        <span className="text-xs opacity-50">قیمت</span>
        <span className="font-bold">{formatToman(pkg.price, "fa")} تومان</span>
      </div>
    </Card>
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
  active: ["فعال", "text-emerald-400"],
  disabled: ["غیرفعال", "text-red-400"],
  quota_exceeded: ["اتمام حجم", "text-amber-400"],
  expired: ["منقضی", "text-gray-400"],
};

function ServiceCard({ service }) {
  const used = service.used_bytes || 0;
  const total = service.quota_bytes || 0;
  const pct = total ? Math.min(100, Math.round((used / total) * 100)) : 0;
  const [label, colour] = STATUS[service.status] || [service.status, "text-gray-400"];
  const reserved = (service.reserved_quota_bytes || 0) + (service.reserved_duration_days || 0);

  return (
    <Card className="mb-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="font-bold truncate">{service.name}</div>
          <div className="text-xs opacity-60 mt-0.5">انقضا: {fmtDate(service.expire_at)}</div>
        </div>
        <div className={`text-xs shrink-0 ${colour}`}>{label}</div>
      </div>

      <div className="mt-3">
        <div className="h-1.5 rounded-full bg-white/10 overflow-hidden">
          <div
            className={`h-full ${pct > 90 ? "bg-red-400" : pct > 70 ? "bg-amber-400" : "bg-sky-400"}`}
            style={{ width: total ? `${pct}%` : "100%" }}
          />
        </div>
        <div className="flex items-center justify-between text-xs opacity-60 mt-1.5">
          <span dir="ltr">{total ? `${formatBytes(used)} / ${formatBytes(total)}` : formatBytes(used)}</span>
          <span>{total ? `${fa(pct)}٪` : "نامحدود"}</span>
        </div>
      </div>

      {/* A renewal already paid for, waiting for this one to run out. Without
          it, a customer who has just renewed sees nothing change and buys
          again. */}
      {reserved > 0 && (
        <div className="text-xs text-sky-400 mt-2">
          ⏳ تمدید رزروشده
          {service.reserved_quota_bytes ? ` · ${formatBytes(service.reserved_quota_bytes)}` : ""}
          {service.reserved_duration_days ? ` · ${fa(service.reserved_duration_days)} روز` : ""}
        </div>
      )}

      {service.connection_count > 0 && (
        <div className="text-xs opacity-40 mt-2">{fa(service.connection_count)} اتصال</div>
      )}
    </Card>
  );
}

export default function MiniApp() {
  const { webApp, settled } = useTelegram();
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [tab, setTab] = useState("shop");

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
    document.body.style.background = "var(--tg-theme-bg-color, #17212b)";
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

  if (error) {
    return (
      <div className="min-h-screen bg-[var(--tg-theme-bg-color,#17212b)] text-[var(--tg-theme-text-color,#fff)] flex items-center justify-center p-6" dir="rtl">
        <Card className="text-center max-w-sm">
          <AlertTriangle className="mx-auto mb-3 text-amber-400" size={28} />
          <div className="text-sm">{error}</div>
          {/* Something to tap. An error screen with no control on it is
              indistinguishable from a frozen app - which is exactly how the
              first version was reported ("هیچ کلیکی نمیشه"). */}
          <button
            type="button"
            className="mt-4 px-4 py-2 rounded-xl bg-sky-500 text-white text-sm"
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
            <summary className="text-xs opacity-50 cursor-pointer">جزئیات فنی</summary>
            <div className="text-[11px] opacity-70 mt-2 space-y-0.5 font-mono" dir="ltr">
              <div>telegram script: {webApp ? "loaded" : settled ? "absent" : "pending"}</div>
              <div>initData in url: {initData ? `${initData.length} chars` : "no"}</div>
              <div>initData from script: {webApp?.initData ? `${webApp.initData.length} chars` : "no"}</div>
              <div>url keys: {debugUrlKeys() || "(none)"}</div>
              <div>platform: {webApp?.platform || "unknown"}</div>
            </div>
          </details>
        </Card>
      </div>
    );
  }

  if (!data) {
    return (
      <div className="min-h-screen bg-[var(--tg-theme-bg-color,#17212b)] text-[var(--tg-theme-text-color,#fff)] flex items-center justify-center">
        <div className="opacity-60 text-sm">در حال بارگذاری…</div>
      </div>
    );
  }

  const tabs = [
    { id: "shop", label: "فروشگاه", icon: Package },
    { id: "services", label: "سرویس‌های من", icon: Layers },
    { id: "wallet", label: "کیف پول", icon: Wallet },
  ];

  return (
    <div
      className="min-h-screen bg-[var(--tg-theme-bg-color,#17212b)] text-[var(--tg-theme-text-color,#fff)]"
      dir="rtl"
    >
      <div className="px-4 pt-5 pb-24 max-w-lg mx-auto">
        <div className="flex items-center justify-between mb-5">
          <div>
            <div className="text-lg font-bold">
              {data.me.name ? `سلام ${data.me.name}` : "فروشگاه"}
            </div>
            {data.shop.title && <div className="text-xs opacity-60 mt-0.5">{data.shop.title}</div>}
          </div>
          <div className="text-end">
            <div className="text-xs opacity-60">موجودی</div>
            <div className="font-bold text-sm" dir="ltr">{formatToman(data.me.wallet, "fa")}</div>
          </div>
        </div>

        {tab === "shop" && (
          <>
            {data.shop.packages.length === 0 && (
              <Card className="text-center text-sm opacity-60">هنوز پلنی برای فروش تعریف نشده.</Card>
            )}
            {data.shop.packages.map((pkg) => (
              <PackageCard key={pkg.id} pkg={pkg} />
            ))}
          </>
        )}

        {tab === "services" && (
          <>
            {data.services.length === 0 && (
              <Card className="text-center text-sm opacity-60">هنوز سرویسی ندارید.</Card>
            )}
            {data.services.map((service) => (
              <ServiceCard key={service.id} service={service} />
            ))}
          </>
        )}

        {tab === "wallet" && (
          <Card>
            <div className="text-xs opacity-60">موجودی کیف پول</div>
            <div className="text-2xl font-bold mt-1" dir="ltr">
              {formatToman(data.me.wallet, "fa")}
            </div>
            {data.payment?.payment_card_number && (
              <div className="mt-4 pt-4 border-t border-white/10 text-sm space-y-1">
                <div className="opacity-60 text-xs">برای شارژ، کارت‌به‌کارت کنید:</div>
                <div dir="ltr" className="font-mono">{data.payment.payment_card_number}</div>
                {data.payment.payment_card_holder && (
                  <div className="opacity-70">{data.payment.payment_card_holder}</div>
                )}
              </div>
            )}
          </Card>
        )}
      </div>

      {/* Telegram puts its own chrome at the top, so the tab bar goes at the
          bottom where a thumb reaches it. */}
      <div className="fixed bottom-0 inset-x-0 bg-[var(--tg-theme-secondary-bg-color,#232e3c)] border-t border-white/10">
        <div className="max-w-lg mx-auto grid grid-cols-3">
          {tabs.map(({ id, label, icon: Icon }) => (
            <button
              key={id}
              type="button"
              onClick={() => setTab(id)}
              className={`py-3 flex flex-col items-center gap-1 text-[11px] ${
                tab === id ? "text-sky-400" : "opacity-60"
              }`}
            >
              <Icon size={18} />
              {label}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
