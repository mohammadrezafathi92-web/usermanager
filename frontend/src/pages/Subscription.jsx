import React, { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import QRCode from "qrcode";
import { Wifi, Globe, Network, ShieldCheck, Lock, KeyRound, ShieldEllipsis, Copy, Check, Download, Gift, Wallet, CalendarClock, ChevronDown, ChevronUp } from "lucide-react";
import { fetchPublicSubscriptionInfo } from "../api/client.js";
import { formatBytes, formatDateTime, statusLabel, STATUS_STYLES, copyText } from "../utils.js";
import { useLanguage } from "../context/LanguageContext.jsx";

// Same protocol -> icon/label/color map as UserDetail.jsx's buildTypeMeta,
// duplicated rather than imported since this page is PUBLIC (no auth) and
// intentionally has zero other dependencies on the authenticated admin UI.
function buildTypeMeta() {
  return {
    wireguard: { label: "WireGuard", icon: Wifi, color: "bg-indigo-50 text-indigo-600" },
    openvpn: { label: "OpenVPN", icon: ShieldCheck, color: "bg-teal-50 text-teal-600" },
    l2tp: { label: "L2TP/IPsec", icon: Lock, color: "bg-amber-50 text-amber-600" },
    ikev2: { label: "IKEv2/IPsec", icon: KeyRound, color: "bg-sky-50 text-sky-600" },
    sstp: { label: "SSTP", icon: ShieldEllipsis, color: "bg-rose-50 text-rose-600" },
    xray: { label: "V2Ray / Xray", icon: Globe, color: "bg-purple-50 text-purple-600" },
    softether: { label: "SoftEther", icon: Network, color: "bg-sky-50 text-sky-600" },
  };
}

const FILE_EXT = { wireguard: "conf", openvpn: "txt", l2tp: "txt", ikev2: "txt", sstp: "txt" };

// Purchase is the quota boundary, not purchase_batch or a sum of protocol
// counters. Keep services with no remaining connections visible as well.
function groupByPurchase(connections, purchases = []) {
  const groups = new Map();
  const order = [];
  for (const p of purchases) {
    const key = `purchase:${p.id}`;
    groups.set(key, { key, purchase: p, connections: [], packageName: p.package_name, comment: p.comment, createdAt: p.created_at || "" });
    order.push(key);
  }
  for (const c of connections) {
    const key = c.purchase_id != null ? `purchase:${c.purchase_id}` : c.purchase_batch || `c${c.id}`;
    if (!groups.has(key)) {
      groups.set(key, { key, purchase: null, connections: [], packageName: c.package_name_snapshot, comment: c.comment, createdAt: c.created_at || "" });
      order.push(key);
    }
    const g = groups.get(key);
    g.connections.push(c);
    if (c.created_at && (!g.createdAt || c.created_at < g.createdAt)) g.createdAt = c.created_at;
  }
  return order.map((k) => groups.get(k)).sort((a, b) => (a.createdAt < b.createdAt ? 1 : a.createdAt > b.createdAt ? -1 : 0));
}

function ServiceCard({ conn, meta, token }) {
  const { t } = useLanguage();
  const [qrUrl, setQrUrl] = useState(null);
  const [copiedKey, setCopiedKey] = useState(null);
  const Icon = meta.icon;
  // The QR now encodes a DOWNLOAD URL for this service's config file
  // (routers/subscription.py's download_connection_config), not the raw
  // config text - scanning it with a phone camera downloads the
  // ready-to-import file instead of showing a wall of text (panel owner's
  // request, 2026-08-09).
  const ovpnFiles = conn.ovpn_files || [];
  const fileUrl = (i = 0) => `${window.location.origin}/api/subscribe/${token}/config/${conn.id}?file=${i}`;
  const downloadUrl = fileUrl(0);
  const hasConfig = Boolean(conn.link || conn.config_text || ovpnFiles.length);

  useEffect(() => {
    if (!hasConfig) {
      setQrUrl(null);
      return;
    }
    QRCode.toDataURL(downloadUrl, { width: 200, margin: 1 }).then(setQrUrl).catch(() => setQrUrl(null));
  }, [downloadUrl, hasConfig]);

  const onCopy = async (key, text) => {
    const ok = await copyText(text);
    setCopiedKey(ok ? key : `${key}-failed`);
    setTimeout(() => setCopiedKey(null), 1500);
  };

  return (
    <div className="card p-4 space-y-3">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className={`w-9 h-9 rounded-lg flex items-center justify-center ${meta.color}`}>
            <Icon size={18} />
          </span>
          <div>
            <div className="font-medium text-sm">{meta.label}</div>
            {conn.node_name && <div className="text-xs text-gray-400">{conn.node_name}</div>}
            {conn.comment && <div className="text-xs text-brand-600">📝 {conn.comment}</div>}
          </div>
        </div>
        <span className={`text-xs px-2 py-1 rounded-full ${conn.online ? "bg-emerald-50 text-emerald-600" : "bg-gray-100 text-gray-500"}`}>
          {conn.online ? t("subscription.online") : t("subscription.offline")}
        </span>
      </div>

      {conn.share_error ? (
        <div className="text-xs text-amber-600 bg-amber-50 rounded-lg px-3 py-2">{t("subscription.unavailable")}</div>
      ) : (
        <>
          {qrUrl && (
            <div className="flex flex-col items-center gap-1 py-1">
              <img src={qrUrl} alt="QR" width={160} height={160} className="rounded-lg border border-gray-100" />
              <div className="text-[11px] text-gray-400">{t("subscription.qrDownloadHint")}</div>
            </div>
          )}

          {conn.link && (
            <div className="flex gap-2">
              <input readOnly className="input font-mono text-xs" value={conn.link} />
              <button className="btn-secondary" onClick={() => onCopy("link", conn.link)}>
                {copiedKey === "link" ? <Check size={14} /> : <Copy size={14} />}
              </button>
            </div>
          )}

          {ovpnFiles.length > 0 && (
            <div className="space-y-2">
              <div className="text-xs text-emerald-600 bg-emerald-50 rounded-lg px-3 py-2">
                {t("subscription.ovpnReady")}
              </div>
              {/* A package can ship several ready-made files (one per
                  server/port variant) - each gets its own download. */}
              {ovpnFiles.map((f, i) => (
                <a key={i} className="btn-primary w-full justify-center whitespace-normal" href={fileUrl(i)} download={f.name}>
                  <Download size={14} className="shrink-0" /> <span className="min-w-0 break-all" dir="auto">{f.name || t("subscription.downloadOvpn")}</span>
                </a>
              ))}
            </div>
          )}

          {conn.config_text && ovpnFiles.length === 0 && (
            <div className="space-y-2">
              <textarea readOnly className="input font-mono text-xs" rows={7} value={conn.config_text} />
              <div className="flex gap-2">
                <button className="btn-secondary flex-1" onClick={() => onCopy("config", conn.config_text)}>
                  {copiedKey === "config" ? <Check size={14} /> : <Copy size={14} />} {t("subscription.copyConfig")}
                </button>
                {/* A real navigation to the server download endpoint, not a
                    client-side Blob URL (see routers/subscription.py's
                    download_connection_config, same endpoint the QR code
                    above already points at) - Blob-URL downloads are
                    silently swallowed by in-app browsers like Telegram's,
                    which is how most customers actually open this page.
                    A server response with Content-Disposition: attachment
                    works everywhere the QR/other download buttons already
                    do. Reported 2026-09-21: OpenVPN username/password
                    services (no admin-uploaded .ovpn template, so this is
                    the fallback path) weren't downloadable. */}
                <a className="btn-primary flex-1 justify-center" href={downloadUrl} download={`${conn.node_name || conn.kind}.${FILE_EXT[conn.kind] || "txt"}`}>
                  <Download size={14} /> {t("subscription.download")}
                </a>
              </div>
            </div>
          )}

          {(copiedKey === "link" || copiedKey === "config") && (
            <div className="text-xs text-emerald-600">{t("subscription.copied")}</div>
          )}
          {(copiedKey === "link-failed" || copiedKey === "config-failed") && (
            <div className="text-xs text-red-500">{t("subscription.copyFailed")}</div>
          )}
        </>
      )}
    </div>
  );
}

// A single account can itself carry several purchases/packages at once
// (e.g. one username bought 4 different packages over time - each its own
// purchase_batch, see groupByPurchase above). Reported 2026-09-23, same
// "page gets huge" concern one level deeper than the account split below:
// each purchase group is its own collapsed-by-default row inside an open
// account, instead of every one of that account's services being dumped
// flat as soon as the account itself is opened.
function PurchaseGroupSection({ group, token, TYPE_META, defaultOpen }) {
  const { t, language } = useLanguage();
  const [open, setOpen] = useState(defaultOpen);
  const purchase = group.purchase;
  const used = Math.max(0, Number(purchase?.used_bytes || 0));
  const quota = Math.max(0, Number(purchase?.quota_bytes || 0));
  const percent = quota ? Math.min(100, Math.max(0, Math.round(used / quota * 100))) : 0;
  const title = group.comment || group.packageName || t("subscription.myServices");

  return (
    <section aria-label={purchase ? `${title} #${purchase.id}` : title} className="rounded-xl border border-gray-200 overflow-hidden">
      <button
        type="button"
        className="w-full min-h-11 flex items-center justify-between gap-2 px-3 py-2.5 text-start bg-gray-50 hover:bg-gray-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-brand-600"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
      >
        <div className="min-w-0">
          <div className="text-sm text-gray-800 font-medium break-words">{title}</div>
          <div className="text-xs text-gray-600 flex flex-wrap gap-x-2 gap-y-1">
            {purchase && <bdi>#{purchase.id}</bdi>}
            {group.comment && group.packageName && <span>{group.packageName}</span>}
            <span>{t("subscription.servicesCount", { count: group.connections.length })}</span>
          </div>
        </div>
        {open ? <ChevronUp size={16} className="text-gray-400 shrink-0" /> : <ChevronDown size={16} className="text-gray-400 shrink-0" />}
      </button>
      {purchase && (
        <div className="px-3 py-3 space-y-2 text-xs border-t border-gray-100">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className={`px-2 py-1 rounded-full ${STATUS_STYLES[purchase.status] || "bg-gray-100 text-gray-600"}`}>{statusLabel(purchase.status, language)}</span>
            {purchase.max_concurrent_sessions != null && <span className="text-gray-600">{t("subscription.sessionCap", { count: purchase.max_concurrent_sessions })}</span>}
          </div>
          <dl className="flex flex-wrap justify-between gap-x-4 gap-y-2 text-gray-600">
            <div><dt className="inline">{t("subscription.usage")}: </dt><dd className="inline text-gray-900"><bdi>{formatBytes(used)} / {quota ? formatBytes(quota) : t("subscription.unlimited")}</bdi></dd></div>
            <div><dt className="inline">{t("subscription.remaining")}: </dt><dd className="inline text-gray-900"><bdi>{quota ? formatBytes(Math.max(0, quota - used)) : t("subscription.unlimited")}</bdi></dd></div>
          </dl>
          {quota > 0 && <div role="progressbar" aria-label={t("subscription.usage")} aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent} className="h-2 rounded-full bg-gray-100 overflow-hidden">
            <div className={`h-full ${used >= quota ? "bg-red-600" : "bg-brand-600"}`} style={{ width: `${percent}%` }} />
          </div>}
          <div className="text-gray-600">{t("subscription.expiry")}: {purchase.expire_at ? <bdi>{formatDateTime(purchase.expire_at, language)}</bdi> : t("subscription.noExpiry")}</div>
        </div>
      )}
      {open && (
        <div className="p-3 grid grid-cols-1 sm:grid-cols-2 gap-3">
          {group.connections.map((conn) => (
            <ServiceCard key={conn.id} conn={conn} meta={TYPE_META[conn.type] || TYPE_META.xray} token={token} />
          ))}
        </div>
      )}
    </section>
  );
}

// One customer can hold several separate accounts (see backend/app/routers/
// subscription.py's _linked_users docstring - repeat purchases under
// different usernames share one telegram_id but stay separate User rows).
// Reported 2026-09-23: "اگر تعداد اکانت زیاد بشاه خیلی صفحه بزرگ میشه" (the
// page gets huge with many accounts) - each account is now its own
// collapsed-by-default section here, opened by tapping its header, instead
// of every account's services being dumped flat on one page.
function AccountSection({ account, token, TYPE_META, defaultOpen }) {
  const { t, language } = useLanguage();
  const [open, setOpen] = useState(defaultOpen);
  const groups = groupByPurchase(account.connections || [], account.purchases || []);
  const showLegacyUsage = !account.purchases?.length || groups.some((group) => !group.purchase);

  return (
    <div className="card overflow-hidden">
      <button
        type="button"
        className="w-full flex items-center justify-between gap-3 p-5 text-right"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
      >
        <div className="flex items-center gap-3 min-w-0">
          <div className="min-w-0">
            <div className="text-lg font-semibold text-gray-800 truncate">{account.full_name || account.username}</div>
            <div className="text-xs text-gray-400">@{account.username}</div>
          </div>
          <span className={`shrink-0 text-xs px-3 py-1 rounded-full ${STATUS_STYLES[account.status] || "bg-gray-100 text-gray-500"}`}>
            {statusLabel(account.status, language)}
          </span>
        </div>
        {open ? <ChevronUp size={18} className="text-gray-400 shrink-0" /> : <ChevronDown size={18} className="text-gray-400 shrink-0" />}
      </button>

      {open && (
        <div className="px-5 pb-5 space-y-4 border-t border-gray-100 pt-4">
          {showLegacyUsage && <div>
            <div className="flex justify-between text-xs text-gray-500 mb-1">
              <span>{account.purchases?.length ? t("subscription.legacyUsage") : t("subscription.usage")}</span>
              <span>{account.total_quota_bytes ? `${formatBytes(account.used_bytes)} / ${formatBytes(account.total_quota_bytes)}` : t("subscription.unlimited")}</span>
            </div>
            <div className="h-2 rounded-full bg-gray-100 overflow-hidden">
              {account.total_quota_bytes ? (
                <div
                  className="h-full bg-brand-600 transition-all"
                  style={{ width: `${Math.min(100, Math.round((account.used_bytes / account.total_quota_bytes) * 100))}%` }}
                />
              ) : (
                <div className="h-full bg-gray-200 w-1/4" />
              )}
            </div>
          </div>}

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 text-sm">
            {showLegacyUsage && <div className="flex items-center gap-2 text-gray-600">
              <CalendarClock size={16} className="text-gray-400" />
              <span>{account.expire_at ? formatDateTime(account.expire_at, language) : t("subscription.noExpiry")}</span>
            </div>}
            <div className="flex items-center gap-2 text-gray-600">
              <Wallet size={16} className="text-gray-400" />
              <span>{(account.balance || 0).toLocaleString()} {t("subscription.toman")}</span>
            </div>
            {account.referral_code && (
              <div className="flex items-center gap-2 text-gray-600 col-span-2">
                <Gift size={16} className="text-gray-400" />
                <span>{t("subscription.referralCode")}: <span className="font-mono">{account.referral_code}</span></span>
              </div>
            )}
          </div>

          <div className="space-y-3">
            <div className="font-medium text-sm text-gray-800 px-1">{t("subscription.myServices")}</div>
            {groups.length === 0 && <div className="text-sm text-gray-400 text-center py-4">{t("subscription.noServices")}</div>}
            {groups.map((g) => (
              <PurchaseGroupSection key={g.key} group={g} token={token} TYPE_META={TYPE_META} defaultOpen={groups.length === 1} />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

export default function Subscription() {
  const { token } = useParams();
  const { t, language } = useLanguage();
  const TYPE_META = buildTypeMeta();
  const [data, setData] = useState(null);
  const [notFound, setNotFound] = useState(false);
  const [copiedApp, setCopiedApp] = useState(false);

  useEffect(() => {
    fetchPublicSubscriptionInfo(token)
      .then((res) => setData(res.data))
      .catch(() => setNotFound(true));
  }, [token]);

  // This page has no dark-mode toggle of its own and none of its colors
  // carry a dark: variant - it is a public, unauthenticated page that
  // never asked for one. But `dark` is a class on <html>, persisted in
  // localStorage, shared by every page on this origin - so a customer
  // opening their link in a browser that once had the admin panel's dark
  // mode switched on inherits it too, half-broken (cards go dark via the
  // shared .card class, plain-color badges do not). Force this one page
  // back to its designed light theme regardless, and restore whatever was
  // there on the way out in case the same tab later reaches the panel.
  useEffect(() => {
    const root = document.documentElement;
    const hadDark = root.classList.contains("dark");
    root.classList.remove("dark");
    return () => {
      if (hadDark) root.classList.add("dark");
    };
  }, []);

  const appLink = `${window.location.origin}/api/subscribe/${token}`;

  const onCopyApp = async () => {
    const ok = await copyText(appLink);
    setCopiedApp(ok);
    setTimeout(() => setCopiedApp(false), 1500);
  };

  if (notFound) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-gray-50 px-4">
        <div className="card p-6 max-w-sm w-full text-center">
          <div className="text-lg font-semibold text-gray-800 mb-1">{t("subscription.notFoundTitle")}</div>
          <div className="text-sm text-gray-500">{t("subscription.notFoundBody")}</div>
        </div>
      </div>
    );
  }

  if (!data) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-gray-50 text-gray-400">{t("common.loading")}</div>
    );
  }

  const accounts = data.accounts || [];

  return (
    <div className="min-h-screen bg-gray-50 py-6 px-4">
      <div className="max-w-2xl mx-auto space-y-4">
        <div className="card p-5 space-y-2">
          <div className="font-medium text-sm text-gray-800">{t("subscription.appSubscribeTitle")}</div>
          <div className="text-xs text-gray-500">{t("subscription.appSubscribeHint")}</div>
          <div className="flex gap-2">
            <input readOnly className="input font-mono text-xs" value={appLink} />
            <button className="btn-secondary" onClick={onCopyApp}>
              {copiedApp ? <Check size={14} /> : <Copy size={14} />}
            </button>
          </div>
          {copiedApp && <div className="text-xs text-emerald-600">{t("subscription.copied")}</div>}
        </div>

        <div className="space-y-3">
          {accounts.length > 1 && (
            <div className="px-1">
              <div className="font-medium text-sm text-gray-800">{t("subscription.accounts")}</div>
              <div className="text-xs text-gray-400">{t("subscription.accountsHint")}</div>
            </div>
          )}
          {accounts.map((acc) => (
            <AccountSection
              key={acc.username}
              account={acc}
              token={token}
              TYPE_META={TYPE_META}
              defaultOpen={accounts.length <= 1}
            />
          ))}
        </div>
      </div>
    </div>
  );
}
