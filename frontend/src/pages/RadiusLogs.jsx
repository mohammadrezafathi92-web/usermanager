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
  { value: "auth_fail", labelKey: "radiusLogs.eventAuthFail" },
  { value: "quota_exceeded", labelKey: "radiusLogs.eventQuotaExceeded" },
  { value: "expired", labelKey: "radiusLogs.eventExpired" },
  { value: "disabled", labelKey: "radiusLogs.eventDisabled" },
  { value: "unknown_user", labelKey: "radiusLogs.eventUnknownUser" },
];

// One place per event type instead of a chain of ifs in two functions -
// adding a type is one row, and a type can no longer end up with a label
// but no colour (or the reverse).
const EVENT_STYLES = {
  ban: { tone: "bg-red-50 text-red-600 dark:bg-red-500/10 dark:text-red-400", labelKey: "radiusLogs.eventBan" },
  unban: { tone: "bg-emerald-50 text-emerald-600 dark:bg-emerald-500/10 dark:text-emerald-400", labelKey: "radiusLogs.eventUnban" },
  reject: { tone: "bg-amber-50 text-amber-600 dark:bg-amber-500/10 dark:text-amber-400", labelKey: "radiusLogs.eventReject" },
  // A wrong password is the one an admin most often needs to tell apart
  // from "the account is finished" - different colour, not just wording.
  auth_fail: { tone: "bg-rose-50 text-rose-600 dark:bg-rose-500/10 dark:text-rose-400", labelKey: "radiusLogs.eventAuthFail" },
  quota_exceeded: { tone: "bg-orange-50 text-orange-600 dark:bg-orange-500/10 dark:text-orange-400", labelKey: "radiusLogs.eventQuotaExceeded" },
  expired: { tone: "bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300", labelKey: "radiusLogs.eventExpired" },
  disabled: { tone: "bg-gray-100 text-gray-500 dark:bg-slate-800 dark:text-gray-400", labelKey: "radiusLogs.eventDisabled" },
  unknown_user: { tone: "bg-purple-50 text-purple-600 dark:bg-purple-500/10 dark:text-purple-400", labelKey: "radiusLogs.eventUnknownUser" },
};

// How many of the most-recent (already newest-first from the API) log rows
// to show - a plain number rather than real pagination, since this page is
// mostly used to eyeball "what just happened" rather than dig through full
// history (تاریخچه کامل still exists via the API for anyone who needs more).
const LIMIT_OPTIONS = [10, 20, 50, 100, 300];

function eventBadgeClass(eventType) {
  return EVENT_STYLES[eventType]?.tone || "bg-amber-50 text-amber-600 dark:bg-amber-500/10 dark:text-amber-400";
}

function eventLabelKey(eventType) {
  return EVENT_STYLES[eventType]?.labelKey || "radiusLogs.eventReject";
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
        {/* دسکتاپ: جدول - از md به بالا نمایش داده می‌شود */}
        <div className="hidden md:block table-wrap !mx-0">
          <table>
            <thead>
              <tr>
                <th>{t("radiusLogs.colType")}</th>
                <th>{t("radiusLogs.colUser")}</th>
                <th>{t("radiusLogs.colConnType")}</th>
                <th>{t("radiusLogs.colIp")}</th>
                <th>{t("radiusLogs.colCount")}</th>
                <th>{t("radiusLogs.colBannedUntil")}</th>
                <th>{t("radiusLogs.colTime")}</th>
              </tr>
            </thead>
            <tbody>
              {logs.map((l) => (
                <tr key={l.id}>
                  <td>
                    <span className={`badge ${eventBadgeClass(l.event_type)}`}>
                      {t(eventLabelKey(l.event_type))}
                    </span>
                  </td>
                  <td>
                    {l.user_id ? (
                      <Link to={`/users/${l.user_id}`} className="font-medium text-gray-800 hover:text-brand-600">
                        {l.username || l.user_id}
                      </Link>
                    ) : (
                      l.username || "-"
                    )}
                  </td>
                  <td className="text-gray-500 dark:text-gray-400">{l.connection_type || "-"}</td>
                  <td className="text-gray-500 dark:text-gray-400 font-mono" dir="ltr">{l.client_ip || "-"}</td>
                  <td className="text-gray-500 dark:text-gray-400">
                    {l.active_count ?? "-"}/{l.limit_value ?? "-"}
                  </td>
                  <td className="text-gray-500 dark:text-gray-400">{l.banned_until ? formatDateTime(l.banned_until, language) : "-"}</td>
                  <td className="text-gray-500 dark:text-gray-400">{formatDateTime(l.created_at, language)}</td>
                </tr>
              ))}
              {!loading && logs.length === 0 && (
                <tr>
                  <td colSpan={7} className="empty-state">
                    {t("radiusLogs.empty")}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>

        {/* موبایل: کارت - زیر md نمایش داده می‌شود */}
        <div className="md:hidden divide-y divide-gray-100 dark:divide-slate-800">
          {logs.map((l) => (
            <div key={l.id} className="p-4">
              <div className="flex items-center justify-between gap-2">
                <span className={`badge ${eventBadgeClass(l.event_type)}`}>
                  {t(eventLabelKey(l.event_type))}
                </span>
                <span className="text-xs text-gray-400">{formatDateTime(l.created_at, language)}</span>
              </div>
              <div className="mt-2">
                {l.user_id ? (
                  <Link to={`/users/${l.user_id}`} className="font-medium text-gray-800 hover:text-brand-600">
                    {l.username || l.user_id}
                  </Link>
                ) : (
                  <span className="text-gray-600">{l.username || "-"}</span>
                )}
              </div>
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1 mt-2 text-xs text-gray-500">
                {l.connection_type && <span>{l.connection_type}</span>}
                {l.client_ip && <span className="font-mono" dir="ltr">{l.client_ip}</span>}
                <span>{l.active_count ?? "-"}/{l.limit_value ?? "-"}</span>
                {l.banned_until && <span>{t("radiusLogs.colBannedUntil")}: {formatDateTime(l.banned_until, language)}</span>}
              </div>
            </div>
          ))}
          {!loading && logs.length === 0 && <div className="empty-state">{t("radiusLogs.empty")}</div>}
        </div>
      </div>
    </Layout>
  );
}
