import { useEffect, useState } from "react";
import { RefreshCw, Power, Zap, Wifi, WifiOff } from "lucide-react";
import {
  getTelegramTunnel, listTunnelNodes, setupTelegramTunnel,
  tunnelUp, tunnelDown, tunnelTest, tunnelRefreshCidrs,
} from "../api/client.js";
import { errorText, formatBytes } from "../utils.js";
import { useLanguage } from "../context/LanguageContext.jsx";

/** The WireGuard tunnel the bot reaches Telegram through.
 *
 * Split into its own component rather than inlined into Settings because it
 * owns six endpoints and a live status poll; folding that into a page that
 * is already long would bury it.
 *
 * The status shown deliberately leads with the HANDSHAKE, not with whether
 * the interface exists. An interface can be up, addressed and routed and
 * still never have reached its peer - "up" and "working" are different
 * questions, and only the handshake answers the second one.
 */
export default function TelegramTunnelCard() {
  const { t } = useLanguage();
  const [data, setData] = useState(null);
  const [nodes, setNodes] = useState([]);
  const [nodeId, setNodeId] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState(null);

  const load = async () => {
    try {
      const [tun, ns] = await Promise.all([getTelegramTunnel(), listTunnelNodes()]);
      setData(tun.data);
      setNodes(ns.data || []);
      if (!nodeId && tun.data?.node_id) setNodeId(String(tun.data.node_id));
    } catch {
      setData(null);
    }
  };

  useEffect(() => {
    load();
    // Polled rather than fetched once: the handshake age is the whole point
    // and it is only meaningful if it is current.
    const id = setInterval(load, 15000);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const run = async (fn, okText) => {
    setBusy(true);
    setMsg(null);
    try {
      const res = await fn();
      const log = res?.data?.log;
      setMsg({ type: "ok", text: Array.isArray(log) ? log.join("\n") : (okText || "انجام شد") });
      await load();
    } catch (err) {
      setMsg({ type: "err", text: errorText(err, "عملیات ناموفق بود") });
    } finally {
      setBusy(false);
    }
  };

  const up = !!data?.interface_up;
  const shook = data?.handshake_age_s !== null && data?.handshake_age_s !== undefined;
  const healthy = up && shook && data.handshake_age_s < 180;

  return (
    <div className="card p-5">
      <div className="flex items-start justify-between gap-3 flex-wrap">
        <div>
          <h3 className="font-bold text-gray-700">{t("settings.tunnelTitle")}</h3>
          <p className="text-xs text-gray-500 mt-1 max-w-2xl">{t("settings.tunnelHint")}</p>
        </div>
        <span className={`badge ${healthy ? "badge-success" : up ? "badge-warn" : "badge-neutral"}`}>
          {up ? <Wifi size={13} /> : <WifiOff size={13} />}
          {up ? t("settings.tunnelStatusUp") : t("settings.tunnelStatusDown")}
        </span>
      </div>

      <div className="flex flex-col sm:flex-row gap-2 mt-4">
        <select
          className="input input-sm flex-1"
          value={nodeId}
          onChange={(e) => setNodeId(e.target.value)}
        >
          <option value="">{t("settings.tunnelPickNode")}</option>
          {nodes.map((n) => (
            <option key={n.id} value={n.id}>{n.name} — {n.host}</option>
          ))}
        </select>
        <button
          type="button"
          className="btn-primary btn-sm"
          disabled={!nodeId || busy}
          onClick={() => run(() => setupTelegramTunnel({ node_id: Number(nodeId) }))}
        >
          {busy ? "..." : t("settings.tunnelSetup")}
        </button>
      </div>

      {data?.configured && (
        <>
          <div className="stat-grid mt-4">
            <div className="rounded-xl border border-gray-200/70 p-3">
              <div className="text-xs text-gray-500">{t("settings.tunnelHandshake")}</div>
              <div className="text-sm font-medium text-gray-800 tabular-nums mt-1">
                {shook ? `${data.handshake_age_s} ${t("settings.tunnelSecondsAgo")}` : "—"}
              </div>
            </div>
            <div className="rounded-xl border border-gray-200/70 p-3">
              <div className="text-xs text-gray-500">{t("settings.tunnelTraffic")}</div>
              <div className="text-sm font-medium text-gray-800 tabular-nums mt-1">
                ↓ {formatBytes(data.rx_bytes)} · ↑ {formatBytes(data.tx_bytes)}
              </div>
            </div>
            <div className="rounded-xl border border-gray-200/70 p-3">
              <div className="text-xs text-gray-500">{t("settings.tunnelRanges")}</div>
              <div className="text-sm font-medium text-gray-800 tabular-nums mt-1">
                {(data.allowed_ips || []).length}
              </div>
            </div>
          </div>

          {up && !shook && (
            <div className="text-xs text-amber-700 bg-amber-50 rounded-lg px-3 py-2 mt-3">
              {t("settings.tunnelNoHandshake")}
            </div>
          )}

          <div className="flex flex-wrap gap-2 mt-4">
            <button type="button" className="btn-outline btn-sm" disabled={busy}
                    onClick={() => run(tunnelTest)}>
              <Zap size={14} /> {t("settings.tunnelTest")}
            </button>
            <button type="button" className="btn-outline btn-sm" disabled={busy}
                    onClick={() => run(tunnelRefreshCidrs)}>
              <RefreshCw size={14} /> {t("settings.tunnelRefresh")}
            </button>
            <button type="button" className="btn-outline btn-sm" disabled={busy}
                    onClick={() => run(up ? tunnelDown : tunnelUp)}>
              <Power size={14} /> {up ? t("settings.tunnelDown") : t("settings.tunnelUp")}
            </button>
          </div>
        </>
      )}

      {(msg || data?.last_error) && (
        <div className={`text-xs rounded-lg px-3 py-2 mt-3 whitespace-pre-line ${
          msg?.type === "ok" ? "text-emerald-700 bg-emerald-50" : "text-red-600 bg-red-50"
        }`}>
          {msg?.text || data.last_error}
        </div>
      )}
    </div>
  );
}
