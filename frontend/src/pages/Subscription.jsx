import React, { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import QRCode from "qrcode";
import { Wifi, Globe, ShieldCheck, Lock, KeyRound, ShieldEllipsis, Copy, Check, Download, Gift, Wallet, CalendarClock } from "lucide-react";
import { fetchPublicSubscriptionInfo } from "../api/client.js";
import { formatBytes, formatDateTime, statusLabel, STATUS_STYLES, copyText, downloadTextFile } from "../utils.js";
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
  };
}

const FILE_EXT = { wireguard: "conf", openvpn: "txt", l2tp: "txt", ikev2: "txt", sstp: "txt" };

// Mirrors UserDetail.jsx's groupConnectionsByPurchase (same purchase_batch
// field), kept as a local copy for the same "public page, no shared admin
// imports" reason as buildTypeMeta above.
function groupByPurchase(connections) {
  const groups = new Map();
  const order = [];
  for (const c of connections) {
    const key = c.purchase_batch || `c${c.id}`;
    if (!groups.has(key)) {
      groups.set(key, { key, connections: [], packageName: c.purchase_batch ? c.package_name_snapshot : null, createdAt: c.created_at || "" });
      order.push(key);
    }
    const g = groups.get(key);
    g.connections.push(c);
    if (c.created_at && (!g.createdAt || c.created_at < g.createdAt)) g.createdAt = c.created_at;
  }
  return order.map((k) => groups.get(k)).sort((a, b) => (a.createdAt < b.createdAt ? 1 : a.createdAt > b.createdAt ? -1 : 0));
}

function ServiceCard({ conn, meta }) {
  const { t } = useLanguage();
  const [qrUrl, setQrUrl] = useState(null);
  const [copiedKey, setCopiedKey] = useState(null);
  const Icon = meta.icon;
  const payload = conn.link || conn.config_text;

  useEffect(() => {
    if (!payload) {
      setQrUrl(null);
      return;
    }
    QRCode.toDataURL(payload, { width: 200, margin: 1 }).then(setQrUrl).catch(() => setQrUrl(null));
  }, [payload]);

  const onCopy = async (key, text) => {
    const ok = await copyText(text);
    setCopiedKey(ok ? key : `${key}-failed`);
    setTimeout(() => setCopiedKey(null), 1500);
  };

  const onDownload = () => {
    if (!conn.config_text) return;
    downloadTextFile(`${conn.node_name || conn.kind}.${FILE_EXT[conn.kind] || "txt"}`, conn.config_text);
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
              <div className="text-[11px] text-gray-400">{t("subscription.qrHint")}</div>
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

          {conn.config_text && (
            <div className="space-y-2">
              <textarea readOnly className="input font-mono text-xs" rows={7} value={conn.config_text} />
              <div className="flex gap-2">
                <button className="btn-secondary flex-1" onClick={() => onCopy("config", conn.config_text)}>
                  {copiedKey === "config" ? <Check size={14} /> : <Copy size={14} />} {t("subscription.copyConfig")}
                </button>
                <button className="btn-primary flex-1" onClick={onDownload}>
                  <Download size={14} /> {t("subscription.download")}
                </button>
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

  const groups = groupByPurchase(data.connections || []);

  return (
    <div className="min-h-screen bg-gray-50 py-6 px-4">
      <div className="max-w-2xl mx-auto space-y-4">
        <div className="card p-5 space-y-4">
          <div className="flex items-center justify-between">
            <div>
              <div className="text-lg font-semibold text-gray-800">{data.full_name || data.username}</div>
              <div className="text-xs text-gray-400">@{data.username}</div>
            </div>
            <span className={`text-xs px-3 py-1 rounded-full ${STATUS_STYLES[data.status] || "bg-gray-100 text-gray-500"}`}>
              {statusLabel(data.status, language)}
            </span>
          </div>

          <div>
            <div className="flex justify-between text-xs text-gray-500 mb-1">
              <span>{t("subscription.usage")}</span>
              <span>{data.total_quota_bytes ? `${formatBytes(data.used_bytes)} / ${formatBytes(data.total_quota_bytes)}` : t("subscription.unlimited")}</span>
            </div>
            <div className="h-2 rounded-full bg-gray-100 overflow-hidden">
              {data.total_quota_bytes ? (
                <div
                  className="h-full bg-brand-600 transition-all"
                  style={{ width: `${Math.min(100, Math.round((data.used_bytes / data.total_quota_bytes) * 100))}%` }}
                />
              ) : (
                <div className="h-full bg-gray-200 w-1/4" />
              )}
            </div>
          </div>

          <div className="grid grid-cols-2 gap-3 text-sm">
            <div className="flex items-center gap-2 text-gray-600">
              <CalendarClock size={16} className="text-gray-400" />
              <span>{data.expire_at ? formatDateTime(data.expire_at, language) : t("subscription.noExpiry")}</span>
            </div>
            <div className="flex items-center gap-2 text-gray-600">
              <Wallet size={16} className="text-gray-400" />
              <span>{(data.balance || 0).toLocaleString()} {t("subscription.toman")}</span>
            </div>
            {data.referral_code && (
              <div className="flex items-center gap-2 text-gray-600 col-span-2">
                <Gift size={16} className="text-gray-400" />
                <span>{t("subscription.referralCode")}: <span className="font-mono">{data.referral_code}</span></span>
              </div>
            )}
          </div>
        </div>

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
          <div className="font-medium text-sm text-gray-800 px-1">{t("subscription.myServices")}</div>
          {groups.length === 0 && <div className="card p-5 text-sm text-gray-400 text-center">{t("subscription.noServices")}</div>}
          {groups.map((g) => (
            <div key={g.key} className="space-y-2">
              {g.packageName && <div className="text-xs text-gray-400 px-1">{g.packageName}</div>}
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                {g.connections.map((conn) => (
                  <ServiceCard key={conn.id} conn={conn} meta={TYPE_META[conn.type] || TYPE_META.xray} />
                ))}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
