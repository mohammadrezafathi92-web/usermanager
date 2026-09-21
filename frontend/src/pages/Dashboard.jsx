import React, { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Users, UserCheck, UserX, AlertTriangle, Server, Database, Wifi, Wallet, Activity, Cpu, MemoryStick, HardDrive, Clock, Shield, ShieldOff, TrendingUp, TrendingDown, Radio } from "lucide-react";
import { AreaChart, Area, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid } from "recharts";
import Layout from "../components/Layout.jsx";
import Topbar from "../components/Topbar.jsx";
import StatCard from "../components/StatCard.jsx";
import UsageBar from "../components/UsageBar.jsx";
import { fetchDashboard, fetchUsageHistory } from "../api/client.js";
import { formatBytes, formatBitrate, formatUptime, toDisplayDate, formatDate, formatToman, formatGb } from "../utils.js";
import { useLanguage } from "../context/LanguageContext.jsx";

const PROTOCOL_LABELS = { wireguard: "WireGuard", openvpn: "OpenVPN", l2tp: "L2TP", ikev2: "IKEv2", sstp: "SSTP", pptp: "PPTP", xray: "V2Ray/Xray" };


// One actionable tile: a number that means "go do something", with the page
// it should take you to. Muted (not alarming) when the count is zero, so a
// clean panel reads as calm rather than as four red boxes at 0. A live tile
// (idle === false) also gets a small pulsing dot on its icon - purely
// decorative urgency, stripped by the global prefers-reduced-motion rule.
function ActionCard({ icon: Icon, label, hint, value, tone, onClick }) {
  const idle = !value;
  const tones = {
    amber: "bg-gradient-to-br from-amber-400/25 to-amber-600/10 text-amber-600 ring-1 ring-inset ring-amber-500/20 dark:text-amber-400",
    red: "bg-gradient-to-br from-red-400/25 to-red-600/10 text-red-600 ring-1 ring-inset ring-red-500/20 dark:text-red-400",
    brand: "bg-gradient-to-br from-brand-400/25 to-brand-600/10 text-brand-600 ring-1 ring-inset ring-brand-500/20 dark:text-brand-400",
  };
  const dot = { amber: "bg-amber-500", red: "bg-red-500", brand: "bg-brand-500" };
  const muted = "bg-gray-100 text-gray-400 dark:bg-slate-800 dark:text-gray-500";
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={idle}
      className={`card flex items-center gap-3 sm:gap-4 w-full text-start ${idle ? "opacity-70" : "card-hover cursor-pointer"}`}
    >
      <div className={`relative w-11 h-11 rounded-2xl flex items-center justify-center shrink-0 ${idle ? muted : tones[tone]}`}>
        <Icon size={20} />
        {!idle && <span className={`absolute -top-1 -end-1 w-2.5 h-2.5 rounded-full ring-2 ring-white dark:ring-slate-900 ${dot[tone]} animate-pulse`} />}
      </div>
      <div className="min-w-0">
        <div className="text-xl font-bold font-mono text-gray-800 dark:text-gray-100 tnum">{value}</div>
        <div className="text-sm text-gray-400 truncate">{label}</div>
        {hint && <div className="text-xs text-gray-400 truncate">{hint}</div>}
      </div>
    </button>
  );
}

const MONEY_TONES = {
  brand: "border-t-brand-400 dark:border-t-brand-500",
  emerald: "border-t-emerald-400 dark:border-t-emerald-500",
  slate: "border-t-gray-200 dark:border-t-slate-700",
};

function MoneyTile({ icon: Icon, label, value, t, lang, trend, tone = "slate" }) {
  return (
    <div className={`card border-t-4 ${MONEY_TONES[tone]}`}>
      <div className="flex items-center justify-between gap-2">
        <div className="text-sm text-gray-400">{label}</div>
        {Icon && <Icon size={15} className="text-gray-300 dark:text-gray-600" />}
      </div>
      <div className="text-xl sm:text-2xl font-bold font-mono text-gray-800 dark:text-gray-100 tnum mt-1.5" dir="ltr">
        {formatToman(value, lang)} <span className="text-sm font-normal font-sans text-gray-400">{t("dashboard.toman")}</span>
      </div>
      {trend}
    </div>
  );
}

/** One number in the hero strip - icon chip, big tabular value, quiet label under it. */
function HeroStat({ icon: Icon, value, label }) {
  return (
    <div className="flex items-center gap-2.5">
      {Icon && (
        <div className="w-9 h-9 rounded-xl bg-white/15 backdrop-blur flex items-center justify-center text-white shrink-0">
          <Icon size={17} />
        </div>
      )}
      <div className="text-start">
        <div className="text-lg sm:text-xl font-bold font-mono text-white tnum" dir="ltr">
          {value}
        </div>
        <div className="text-xs text-white/70 whitespace-nowrap">{label}</div>
      </div>
    </div>
  );
}

// Tabs for the usage chart's time range - kept as an ordered array (not just
// an object) so the buttons render in a fixed, deliberate order regardless
// of insertion order in translations.js.
const USAGE_RANGES = [
  { key: "24h", labelKey: "dashboard.range24h" },
  { key: "7d", labelKey: "dashboard.range7d" },
  { key: "30d", labelKey: "dashboard.range30d" },
];

export default function Dashboard() {
  const [stats, setStats] = useState(null);
  const [usageRange, setUsageRange] = useState("24h");
  const [usageBuckets, setUsageBuckets] = useState(null);
  const navigate = useNavigate();
  const { t, language } = useLanguage();

  const load = () => fetchDashboard().then((res) => setStats(res.data));

  useEffect(() => {
    load();
    const timer = setInterval(load, 15000);
    return () => clearInterval(timer);
  }, []);

  // /stats already carries the 24h view (usage_last_24h, unchanged), so the
  // default tab needs no extra request - only switching to 7d/30d fetches
  // from the dedicated /usage-history endpoint. Re-fetches the active range
  // every 15s too, so tab switches stay live like the rest of the page.
  useEffect(() => {
    if (usageRange === "24h") {
      setUsageBuckets(null);
      return undefined;
    }
    const loadRange = () => fetchUsageHistory(usageRange).then((res) => setUsageBuckets(res.data.buckets));
    loadRange();
    const timer = setInterval(loadRange, 15000);
    return () => clearInterval(timer);
  }, [usageRange]);

  const rawBuckets = usageRange === "24h" ? stats?.usage_last_24h : usageBuckets;

  // The bucket keys are UTC ("2026-08-13 14:00" for 24h, "2026-08-13" for
  // 7d/30d). Slicing the hour straight out of the string labelled the axis
  // in UTC, so the traffic peak appeared 3.5 hours away from when it
  // actually happened locally. The space becomes a "T" first so
  // toDisplayDate gets a value Date can parse.
  const chartData = (rawBuckets || []).map((d) => {
    if (usageRange === "24h") {
      const at = toDisplayDate(String(d.bucket).replace(" ", "T"));
      return {
        time: at ? `${String(at.getUTCHours()).padStart(2, "0")}:00` : d.bucket.slice(11, 16),
        bytes: d.bytes,
        upload_bytes: d.upload_bytes,
        download_bytes: d.download_bytes,
        label: formatBytes(d.bytes),
      };
    }
    // 7d/30d buckets are date-only ("2026-08-13") - formatDate wants a
    // full timestamp to run through toDisplayDate/gregorianToJalali.
    return {
      time: formatDate(`${d.bucket}T00:00:00`, language),
      bytes: d.bytes,
      upload_bytes: d.upload_bytes,
      download_bytes: d.download_bytes,
      label: formatBytes(d.bytes),
    };
  });

  return (
    <Layout>
      <Topbar title={t("dashboard.title")} subtitle={t("dashboard.subtitle")} />

      {!stats ? (
        <div className="text-gray-400">{t("common.loading")}</div>
      ) : (
        <>
          {/* Hero: the "is everything alive" glance, before anything else. */}
          <div className="relative overflow-hidden rounded-3xl mb-6 p-5 sm:p-7 bg-gradient-to-br from-brand-600 via-brand-500 to-indigo-600 dark:from-brand-700 dark:via-brand-600 dark:to-indigo-800 shadow-glow">
            <div className="absolute inset-0 opacity-20 [background:radial-gradient(circle_at_20%_20%,#fff,transparent_35%),radial-gradient(circle_at_85%_75%,#fff,transparent_30%)]" />
            <div className="relative">
              <div className="inline-flex items-center gap-1.5 text-xs font-medium text-white/90 bg-white/15 backdrop-blur px-2.5 py-1 rounded-full mb-3">
                <Radio size={12} className="animate-pulse" />
                {t("dashboard.liveOverview")}
              </div>
              <div className="flex flex-wrap items-end justify-between gap-4">
                <div>
                  <h2 className="text-xl sm:text-2xl font-bold text-white">{t("dashboard.title")}</h2>
                  {t("dashboard.subtitle") && (
                    <p className="text-sm text-white/70 mt-1">{t("dashboard.subtitle")}</p>
                  )}
                </div>
                <div className="flex flex-wrap gap-3 sm:gap-4">
                  <HeroStat icon={Wifi} label={t("dashboard.onlineUsersNow")} value={stats.online_users_now} />
                  <HeroStat icon={Server} label={t("dashboard.onlineServers")} value={`${stats.online_nodes}/${stats.total_nodes}`} />
                  <HeroStat icon={Users} label={t("dashboard.totalUsers")} value={stats.total_users} />
                </div>
              </div>
            </div>
          </div>

          {/* What needs doing, before the totals. Ordered by urgency:
              something broken, then money about to walk out the door. */}
          <div className="section-title mb-2">{t("dashboard.needsYou")}</div>
          <div className="grid grid-cols-1 xs:grid-cols-2 lg:grid-cols-3 gap-3 sm:gap-4 mb-6">
            <ActionCard
              icon={ShieldOff}
              tone="red"
              label={t("dashboard.offlineNodes")}
              hint={(stats.offline_node_names || []).join("، ") || t("dashboard.allNodesOnline")}
              value={(stats.offline_node_names || []).length}
              onClick={() => navigate("/nodes")}
            />
            <ActionCard
              icon={AlertTriangle}
              tone="amber"
              label={t("dashboard.quotaExceeded")}
              hint={t("dashboard.renewalChance")}
              value={stats.quota_exceeded_users}
              onClick={() => navigate("/users?status=quota_exceeded")}
            />
            <ActionCard
              icon={Clock}
              tone="brand"
              label={t("dashboard.expiringSoon")}
              hint={t("dashboard.expiringSoonHint", { days: stats.expiring_soon_days })}
              value={stats.expiring_soon_users}
              onClick={() => navigate("/users?status=active")}
            />
          </div>

          <div className="section-title mb-2">{t("dashboard.money")}</div>
          <div className="grid grid-cols-1 xs:grid-cols-3 gap-3 sm:gap-4 mb-6">
            <MoneyTile icon={Wallet} tone="brand" label={t("dashboard.salesToday")} value={stats.sales_today} t={t} lang={language} />
            <MoneyTile
              icon={TrendingUp}
              tone="emerald"
              label={t("dashboard.salesMonth")}
              value={stats.sales_month}
              t={t}
              lang={language}
              trend={
                stats.sales_prev_month > 0 ? (
                  <div
                    className={`inline-flex items-center gap-1 text-xs font-medium mt-2 px-2 py-0.5 rounded-full ${
                      stats.sales_month >= stats.sales_prev_month
                        ? "bg-emerald-50 text-emerald-700 dark:bg-emerald-500/10 dark:text-emerald-400"
                        : "bg-red-50 text-red-600 dark:bg-red-500/10 dark:text-red-400"
                    }`}
                  >
                    {stats.sales_month >= stats.sales_prev_month ? <TrendingUp size={12} /> : <TrendingDown size={12} />}
                    {t("dashboard.vsPrevMonth", {
                      percent: Math.round(((stats.sales_month - stats.sales_prev_month) / stats.sales_prev_month) * 100),
                    })}
                  </div>
                ) : null
              }
            />
            <MoneyTile icon={Clock} tone="slate" label={t("dashboard.salesPrevMonth")} value={stats.sales_prev_month} t={t} lang={language} />
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
            <StatCard icon={Users} label={t("dashboard.totalUsers")} value={stats.total_users} tone="brand" onClick={() => navigate("/users")} />
            <StatCard icon={UserCheck} label={t("dashboard.activeUsers")} value={stats.active_users} tone="emerald" onClick={() => navigate("/users?status=active")} />
            <StatCard icon={AlertTriangle} label={t("dashboard.quotaExceeded")} value={stats.quota_exceeded_users} tone="amber" onClick={() => navigate("/users?status=quota_exceeded")} />
            <StatCard icon={UserX} label={t("dashboard.disabledUsers")} value={stats.disabled_users} tone="red" onClick={() => navigate("/users?status=disabled")} />
          </div>

          {/* lg:grid-cols-3 only when the admin_balance tile below actually
              renders (it's null for a superadmin - see dashboard.py) - a
              fixed 3 columns left a bare, cardless gap next to "avg speed"
              on desktop for every superadmin login. */}
          <div className={`grid grid-cols-1 sm:grid-cols-2 ${stats.admin_balance != null ? "lg:grid-cols-3" : "lg:grid-cols-2"} gap-4 mb-6`}>
            <div className="card flex items-center gap-4">
              <div className="w-12 h-12 rounded-2xl bg-gradient-to-br from-brand-400/20 to-brand-600/10 text-brand-600 ring-1 ring-inset ring-brand-500/15 dark:from-brand-400/25 dark:to-brand-600/10 dark:text-brand-400 flex items-center justify-center shrink-0">
                <Database size={22} />
              </div>
              <div className="min-w-0">
                <div className="text-xl sm:text-2xl font-bold font-mono text-gray-800 dark:text-gray-100 whitespace-nowrap tnum" dir="ltr">
                  {formatBytes(stats.total_used_bytes)}
                  <span className="text-sm text-gray-400 font-normal font-sans"> / {stats.total_quota_bytes ? formatBytes(stats.total_quota_bytes) : t("userDetail.unlimited")}</span>
                </div>
                <div className="text-sm text-gray-400">{t("dashboard.totalUsageAllUsers")}</div>
              </div>
            </div>
            <div className="card flex items-center gap-4">
              <div className="w-12 h-12 rounded-2xl bg-gradient-to-br from-amber-400/20 to-amber-600/10 text-amber-600 ring-1 ring-inset ring-amber-500/15 dark:from-amber-400/25 dark:to-amber-600/10 dark:text-amber-400 flex items-center justify-center shrink-0">
                <Activity size={22} />
              </div>
              <div>
                <div className="text-xl sm:text-2xl font-bold font-mono text-gray-800 dark:text-gray-100 whitespace-nowrap tnum" dir="ltr">
                  {formatBitrate(stats.avg_speed_bps)}
                </div>
                <div className="text-sm text-gray-400">{t("dashboard.avgSpeed")}</div>
              </div>
            </div>
            {stats.admin_balance != null && (
              <div className="card flex items-center gap-4">
                {/* An admin metered by volume (billing_mode "usage") has no
                    meaningful Toman balance - admin.balance stays whatever
                    it happened to be when the mode was switched, it's just
                    not what's being spent. Show their GB pool instead, same
                    as the superadmin's own Admins list already does (see
                    Admins.jsx's per-row balance column) - reported 2026-09-06
                    as missing here specifically. */}
                {stats.admin_billing_mode === "usage" ? (
                  <>
                    <div className="w-12 h-12 rounded-2xl bg-gradient-to-br from-violet-400/20 to-violet-600/10 text-violet-600 ring-1 ring-inset ring-violet-500/15 dark:from-violet-400/25 dark:to-violet-600/10 dark:text-violet-400 flex items-center justify-center shrink-0">
                      <Database size={22} />
                    </div>
                    <div>
                      <div
                        className={`text-xl sm:text-2xl font-bold font-mono tnum ${
                          stats.admin_volume_balance_gb < 0 ? "text-red-500" : "text-gray-800 dark:text-gray-100"
                        }`}
                        dir="ltr"
                      >
                        {formatGb(stats.admin_volume_balance_gb, language)} <span className="text-sm text-gray-400 font-normal font-sans">GB</span>
                      </div>
                      <div className="text-sm text-gray-400">{t("dashboard.yourBalance")}</div>
                      {/* A negative pool is legitimate and was alarming
                          precisely because nothing said so: traffic is
                          deducted as it actually flows (quota_manager's
                          _apply_delta) and is never blocked mid-session, so
                          customers can consume past the end of the pool.
                          It reads as debt, not as an error - and the
                          deduction stays even if the customer who caused it
                          is later deleted, which is why it can sit next to a
                          usage figure of zero. */}
                      {stats.admin_volume_balance_gb < 0 && (
                        <div className="text-xs text-red-500 mt-1">{t("dashboard.volumeDebtHint")}</div>
                      )}
                      {/* The money side of the same account. A usage-billed
                          reseller was shown a GB pool and nothing else, so
                          the one number they actually owe was the one the
                          panel never told them. Includes traffic metered
                          but not yet billed, which is why it can move
                          without any charge appearing yet. */}
                      {stats.admin_debt_toman > 0 && (
                        <div className="text-xs text-red-500 mt-1" dir="rtl">
                          {t("dashboard.debtLabel")}{" "}
                          <span dir="ltr">{formatToman(stats.admin_debt_toman, language)}</span>{" "}
                          {t("dashboard.tomanUnit")}
                          {stats.admin_unbilled_usage_gb > 0 && (
                            <span className="opacity-70">
                              {" "}({t("dashboard.debtIncludesUnbilled").replace(
                                "{gb}", formatGb(stats.admin_unbilled_usage_gb, language))})
                            </span>
                          )}
                        </div>
                      )}
                    </div>
                  </>
                ) : (
                  <>
                    <div className="w-12 h-12 rounded-2xl bg-gradient-to-br from-emerald-400/20 to-emerald-600/10 text-emerald-600 ring-1 ring-inset ring-emerald-500/15 dark:from-emerald-400/25 dark:to-emerald-600/10 dark:text-emerald-400 flex items-center justify-center shrink-0">
                      <Wallet size={22} />
                    </div>
                    <div>
                      <div className="text-xl sm:text-2xl font-bold font-mono tnum text-gray-800 dark:text-gray-100" dir="ltr">
                        {formatToman(stats.admin_balance, language)} <span className="text-sm text-gray-400 font-normal font-sans">{t("dashboard.tomanUnit")}</span>
                      </div>
                      <div className="text-sm text-gray-400">{t("dashboard.yourBalance")}</div>
                      {stats.admin_debt_toman > 0 && (
                        <div className="text-xs text-red-500 mt-1" dir="rtl">
                          {t("dashboard.debtLabel")}{" "}
                          <span dir="ltr">{formatToman(stats.admin_debt_toman, language)}</span>{" "}
                          {t("dashboard.tomanUnit")}
                        </div>
                      )}
                    </div>
                  </>
                )}
              </div>
            )}
          </div>

          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 mb-6">
            {stats.system_cpu_percent != null && (
              <div className="card">
                <h3 className="flex items-center gap-2 font-bold text-gray-700 dark:text-gray-300 mb-4">
                  <span className="w-7 h-7 rounded-lg bg-brand-50 text-brand-600 dark:bg-brand-500/10 dark:text-brand-400 flex items-center justify-center">
                    <Cpu size={14} />
                  </span>
                  {t("dashboard.systemStatus")}
                </h3>
                <div className="space-y-4">
                  <div>
                    <div className="flex items-center justify-between text-sm mb-1.5">
                      <span className="flex items-center gap-1.5 text-gray-500 dark:text-gray-400">
                        <Cpu size={14} /> {t("dashboard.cpu")}
                        <span className="text-xs text-gray-300 dark:text-gray-600">
                          ({t("dashboard.cores", { count: stats.system_cpu_cores || 0 })})
                        </span>
                      </span>
                      <span className="font-medium text-gray-700 dark:text-gray-200" dir="ltr">{stats.system_cpu_percent}%</span>
                    </div>
                    <UsageBar percent={stats.system_cpu_percent} />
                  </div>
                  <div>
                    <div className="flex items-center justify-between text-sm mb-1.5">
                      <span className="flex items-center gap-1.5 text-gray-500 dark:text-gray-400">
                        <MemoryStick size={14} /> {t("dashboard.ram")}
                      </span>
                      <span className="font-medium text-gray-700 dark:text-gray-200" dir="ltr">
                        {formatBytes(stats.system_ram_used_bytes)} / {formatBytes(stats.system_ram_total_bytes)}
                      </span>
                    </div>
                    <UsageBar percent={(stats.system_ram_used_bytes / stats.system_ram_total_bytes) * 100} />
                  </div>
                  <div>
                    <div className="flex items-center justify-between text-sm mb-1.5">
                      <span className="flex items-center gap-1.5 text-gray-500 dark:text-gray-400">
                        <HardDrive size={14} /> {t("dashboard.disk")}
                      </span>
                      <span className="font-medium text-gray-700 dark:text-gray-200" dir="ltr">
                        {formatBytes(stats.system_disk_used_bytes)} / {formatBytes(stats.system_disk_total_bytes)}
                      </span>
                    </div>
                    <UsageBar percent={(stats.system_disk_used_bytes / stats.system_disk_total_bytes) * 100} />
                  </div>
                  <div className="flex items-center gap-1.5 text-sm text-gray-400 pt-1">
                    <Clock size={14} /> {t("dashboard.uptime")}: <span dir="ltr">{formatUptime(stats.system_uptime_seconds)}</span>
                  </div>
                </div>
              </div>
            )}

            <div className="card">
              <h3 className="flex items-center gap-2 font-bold text-gray-700 dark:text-gray-300 mb-4">
                <span className="w-7 h-7 rounded-lg bg-emerald-50 text-emerald-600 dark:bg-emerald-500/10 dark:text-emerald-400 flex items-center justify-center">
                  <Shield size={14} />
                </span>
                {t("dashboard.protocolStatus")}
              </h3>
              <div className="grid grid-cols-2 gap-2.5">
                {Object.entries(PROTOCOL_LABELS).map(([key, label]) => {
                  const count = stats.protocol_connection_counts?.[key] || 0;
                  const online = stats.protocol_online_counts?.[key] || 0;
                  const active = count > 0;
                  return (
                    <div
                      key={key}
                      className={`flex items-center justify-between rounded-xl border px-3 py-2.5 transition-colors ${
                        active
                          ? "border-emerald-200/60 bg-emerald-50/50 dark:border-emerald-500/20 dark:bg-emerald-500/5"
                          : "border-gray-100 dark:border-slate-800"
                      }`}
                    >
                      <span className="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-200">
                        {active ? (
                          <Shield size={14} className="text-emerald-500 shrink-0" />
                        ) : (
                          <ShieldOff size={14} className="text-gray-300 dark:text-gray-600 shrink-0" />
                        )}
                        {label}
                      </span>
                      <span className="flex items-center gap-1.5" dir="ltr">
                        {online > 0 && (
                          <span
                            className="inline-flex items-center gap-1 text-[10px] font-medium text-brand-600 dark:text-brand-400 bg-brand-50 dark:bg-brand-500/10 rounded-full px-1.5 py-0.5"
                            title={t("dashboard.onlineCount", { count: online })}
                          >
                            <span className="w-1.5 h-1.5 rounded-full bg-brand-500 animate-pulse" />
                            {online}
                          </span>
                        )}
                        <span
                          className={`text-xs font-medium ${
                            active ? "text-emerald-600 dark:text-emerald-400" : "text-gray-300 dark:text-gray-600"
                          }`}
                        >
                          {count}
                        </span>
                      </span>
                    </div>
                  );
                })}
              </div>
            </div>
          </div>

          <div className="card">
            <div className="flex items-center justify-between flex-wrap gap-3 mb-4">
              <h3 className="flex items-center gap-2 font-bold text-gray-700 dark:text-gray-300">
                <span className="w-7 h-7 rounded-lg bg-brand-50 text-brand-600 dark:bg-brand-500/10 dark:text-brand-400 flex items-center justify-center">
                  <TrendingUp size={14} />
                </span>
                {t(usageRange === "24h" ? "dashboard.usageLast24h" : "dashboard.usageChartTitle")}
              </h3>
              <div className="flex items-center gap-1 bg-gray-100 dark:bg-slate-800 rounded-xl p-1">
                {USAGE_RANGES.map((r) => (
                  <button
                    key={r.key}
                    type="button"
                    onClick={() => setUsageRange(r.key)}
                    className={`px-3 py-1.5 rounded-lg text-xs font-medium transition-colors ${
                      usageRange === r.key
                        ? "bg-white dark:bg-slate-700 text-brand-600 dark:text-brand-400 shadow-sm"
                        : "text-gray-400 hover:text-gray-600 dark:hover:text-gray-300"
                    }`}
                  >
                    {t(r.labelKey)}
                  </button>
                ))}
              </div>
            </div>
            <div className="flex items-center gap-4 mb-3 text-xs text-gray-500 dark:text-gray-400">
              <span className="flex items-center gap-1.5">
                <span className="w-2.5 h-2.5 rounded-full" style={{ background: "var(--rc-download-stroke)" }} />
                {t("dashboard.download")}
              </span>
              <span className="flex items-center gap-1.5">
                <span className="w-2.5 h-2.5 rounded-full" style={{ background: "var(--rc-upload-stroke)" }} />
                {t("dashboard.upload")}
              </span>
              <span className="flex items-center gap-1.5">
                <span className="w-2.5 h-2.5 rounded-full" style={{ background: "var(--rc-total-stroke)" }} />
                {t("dashboard.totalUsage")}
              </span>
            </div>
            {/* recharts renders its own inline SVG styles and doesn't see
                Tailwind's dark: variants - it's themed here off the --rc-*
                custom properties defined in index.css instead, which flip
                with the .dark class the same way everything else does. */}
            <ResponsiveContainer width="100%" height={280}>
              <AreaChart data={chartData}>
                <defs>
                  <linearGradient id="colorDownload" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="5%" stopColor="var(--rc-download-fill)" stopOpacity={0.4} />
                    <stop offset="95%" stopColor="var(--rc-download-fill)" stopOpacity={0} />
                  </linearGradient>
                  <linearGradient id="colorUpload" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="5%" stopColor="var(--rc-upload-fill)" stopOpacity={0.4} />
                    <stop offset="95%" stopColor="var(--rc-upload-fill)" stopOpacity={0} />
                  </linearGradient>
                  <linearGradient id="colorTotal" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="5%" stopColor="var(--rc-total-fill)" stopOpacity={0.25} />
                    <stop offset="95%" stopColor="var(--rc-total-fill)" stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid strokeDasharray="3 3" stroke="var(--rc-grid)" />
                <XAxis dataKey="time" tick={{ fontSize: 12, fill: "var(--rc-tick)" }} axisLine={{ stroke: "var(--rc-grid)" }} tickLine={{ stroke: "var(--rc-grid)" }} />
                <YAxis
                  tickFormatter={(v) => formatBytes(v)}
                  tick={{ fontSize: 12, fill: "var(--rc-tick)" }}
                  axisLine={{ stroke: "var(--rc-grid)" }}
                  tickLine={{ stroke: "var(--rc-grid)" }}
                  width={70}
                />
                <Tooltip
                  formatter={(v, name) => {
                    const label = name === "download_bytes" ? t("dashboard.download") : name === "upload_bytes" ? t("dashboard.upload") : t("dashboard.totalUsage");
                    return [formatBytes(v), label];
                  }}
                  labelFormatter={(l) => (usageRange === "24h" ? t("dashboard.hourLabel", { value: l }) : l)}
                  contentStyle={{ background: "var(--rc-tooltip-bg)", border: "1px solid var(--rc-tooltip-border)", borderRadius: 12 }}
                  labelStyle={{ color: "var(--rc-tooltip-fg)" }}
                  itemStyle={{ color: "var(--rc-tooltip-fg)" }}
                />
                <Area type="monotone" dataKey="bytes" name={t("dashboard.totalUsage")} stroke="var(--rc-total-stroke)" fill="url(#colorTotal)" strokeWidth={2} strokeDasharray="4 3" />
                <Area type="monotone" dataKey="download_bytes" name={t("dashboard.download")} stroke="var(--rc-download-stroke)" fill="url(#colorDownload)" strokeWidth={2.5} />
                <Area type="monotone" dataKey="upload_bytes" name={t("dashboard.upload")} stroke="var(--rc-upload-stroke)" fill="url(#colorUpload)" strokeWidth={2.5} />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        </>
      )}
    </Layout>
  );
}
