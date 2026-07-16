import React, { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ShieldAlert } from "lucide-react";
import Layout from "../components/Layout.jsx";
import Topbar from "../components/Topbar.jsx";
import { fetchRadiusLimitLogs } from "../api/client.js";
import { formatDateTime } from "../utils.js";
import { useLanguage } from "../context/LanguageContext.jsx";

const EVENT_FILTER_OPTIONS = [
  { value: "", labelKey: "radiusLogs.filterAll" },
  { value: "ban", labelKey: "radiusLogs.eventBan" },
  { value: "unban", labelKey: "radiusLogs.eventUnban" },
  { value: "reject", labelKey: "radiusLogs.eventReject" },
];

// How many of the most-recent (already newest-first from the API) log rows
// to show - a plain number rather than real pagination, since this page is
// mostly used to eyeball "what just happened" rather than dig through full
// history (تاریخچه کامل still exists via the API for anyone who needs more).
const LIMIT_OPTIONS = [10, 20, 50, 100, 300];

function eventBadgeClass(eventType) {
  if (eventType === "ban") return "bg-red-50 text-red-600";
  if (eventType === "unban") return "bg-emerald-50 text-emerald-600";
  return "bg-amber-50 text-amber-600";
}

function eventLabelKey(eventType) {
  if (eventType === "ban") return "radiusLogs.eventBan";
  if (eventType === "unban") return "radiusLogs.eventUnban";
  return "radiusLogs.eventReject";
}

export default function RadiusLogs() {
  const { t, language } = useLanguage();
  const [logs, setLogs] = useState([]);
  const [loading, setLoading] = useState(true);
  const [eventType, setEventType] = useState("");
  const [limit, setLimit] = useState(50);

  const load = () => {
    setLoading(true);
    fetchRadiusLimitLogs({ event_type: eventType || undefined, limit })
      .then((res) => setLogs(res.data))
      .catch(() => setLogs([]))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [eventType, limit]);

  return (
    <Layout>
      <Topbar title={t("radiusLogs.title")} subtitle={t("radiusLogs.subtitle")} />

      <div className="card !p-4 mb-4">
        <div className="flex items-center gap-2 flex-wrap">
          <ShieldAlert size={16} className="text-gray-400" />
          <select className="input !w-auto min-w-[10rem] cursor-pointer" value={eventType} onChange={(e) => setEventType(e.target.value)}>
            {EVENT_FILTER_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {t(o.labelKey)}
              </option>
            ))}
          </select>
          <select className="input !w-auto min-w-[8rem] cursor-pointer" value={limit} onChange={(e) => setLimit(Number(e.target.value))}>
            {LIMIT_OPTIONS.map((n) => (
              <option key={n} value={n}>
                {t("radiusLogs.filterLastN", { count: n })}
              </option>
            ))}
          </select>
        </div>
      </div>

      <div className="card !p-0 overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-xs text-gray-400 border-b border-gray-50">
                <th className="text-right font-medium px-4 py-3">{t("radiusLogs.colType")}</th>
                <th className="text-right font-medium px-4 py-3">{t("radiusLogs.colUser")}</th>
                <th className="text-right font-medium px-4 py-3">{t("radiusLogs.colConnType")}</th>
                <th className="text-right font-medium px-4 py-3">{t("radiusLogs.colIp")}</th>
                <th className="text-right font-medium px-4 py-3">{t("radiusLogs.colCount")}</th>
                <th className="text-right font-medium px-4 py-3">{t("radiusLogs.colBannedUntil")}</th>
                <th className="text-right font-medium px-4 py-3">{t("radiusLogs.colTime")}</th>
              </tr>
            </thead>
            <tbody>
              {logs.map((l) => (
                <tr key={l.id} className="border-t border-gray-50 hover:bg-gray-50/60">
                  <td className="px-4 py-3">
                    <span className={`badge ${eventBadgeClass(l.event_type)}`}>
                      {t(eventLabelKey(l.event_type))}
                    </span>
                  </td>
                  <td className="px-4 py-3">
                    {l.user_id ? (
                      <Link to={`/users/${l.user_id}`} className="font-medium text-gray-800 hover:text-brand-600">
                        {l.username || l.user_id}
                      </Link>
                    ) : (
                      l.username || "-"
                    )}
                  </td>
                  <td className="px-4 py-3 text-gray-500">{l.connection_type || "-"}</td>
                  <td className="px-4 py-3 text-gray-500 font-mono" dir="ltr">{l.client_ip || "-"}</td>
                  <td className="px-4 py-3 text-gray-500">
                    {l.active_count ?? "-"}/{l.limit_value ?? "-"}
                  </td>
                  <td className="px-4 py-3 text-gray-500">{l.banned_until ? formatDateTime(l.banned_until, language) : "-"}</td>
                  <td className="px-4 py-3 text-gray-500">{formatDateTime(l.created_at, language)}</td>
                </tr>
              ))}
              {!loading && logs.length === 0 && (
                <tr>
                  <td colSpan={7} className="px-4 py-10 text-center text-gray-400">
                    {t("radiusLogs.empty")}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </Layout>
  );
}
