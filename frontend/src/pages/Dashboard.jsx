import React, { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Users, UserCheck, UserX, AlertTriangle, Server, Database, Wifi, Wallet, Activity, Cpu, MemoryStick, HardDrive, Clock, Shield, ShieldOff } from "lucide-react";
import { AreaChart, Area, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid } from "recharts";
import Layout from "../components/Layout.jsx";
import Topbar from "../components/Topbar.jsx";
import StatCard from "../components/StatCard.jsx";
import { fetchDashboard } from "../api/client.js";
import { formatBytes, formatBitrate, formatUptime } from "../utils.js";
import { useLanguage } from "../context/LanguageContext.jsx";

const PROTOCOL_LABELS = { wireguard: "WireGuard", openvpn: "OpenVPN", l2tp: "L2TP", ikev2: "IKEv2", sstp: "SSTP", xray: "V2Ray/Xray" };

// Small horizontal usage bar (CPU/RAM/disk %) - color shifts from the
// brand/emerald "fine" range up through amber/red as it approaches full,
// same at-a-glance severity cue the reference panel's own gauges use.
function UsageBar({ percent }) {
  const pct = Math.max(0, Math.min(100, percent || 0));
  const color = pct >= 90 ? "bg-red-500" : pct >= 70 ? "bg-amber-500" : "bg-emerald-500";
  return (
    <div className="w-full h-1.5 rounded-full bg-gray-100 dark:bg-slate-800 overflow-hidden">
      <div className={`h-full rounded-full ${color} transition-all`} style={{ width: `${pct}%` }} />
    </div>
  );
}


const toman = (n) => Number(n || 0).toLocaleString("en-US");

// One actionable tile: a number that means "go do something", with the page
// it should take you to. Muted (not alarming) when the count is zero, so a
// clean panel reads as calm rather than as four red boxes at 0.
function ActionCard({ icon: Icon, label, hint, value, tone, onClick }) {
  const idle = !value;
  const tones = {
    amber: "bg-amber-50 text-amber-600 dark:bg-amber-500/10 dark:text-amber-400",
    red: "bg-red-50 text-red-600 dark:bg-red-500/10 dark:text-red-400",
    brand: "bg-brand-50 text-brand-600 dark:bg-brand-500/10 dark:text-brand-400",
  };
  const muted = "bg-gray-100 text-gray-400 dark:bg-slate-800 dark:text-gray-500";
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={idle}
      className={`card flex items-center gap-3 sm:gap-4 w-full text-start ${idle ? "opacity-70" : "card-hover cursor-pointer"}`}
    >
      <div className={`w-11 h-11 rounded-2xl flex items-center justify-center shrink-0 ${idle ? muted : tones[tone]}`}>
        <Icon size={20} />
      </div>
      <div className="min-w-0">
        <div className="text-xl font-bold text-gray-800 dark:text-gray-100 tnum">{value}</div>
        <div className="text-sm text-gray-400 truncate">{label}</div>
        {hint && <div className="text-xs text-gray-400 truncate">{hint}</div>}
      </div>
    </button>
  );
}

function MoneyTile({ label, value, t, sub }) {
  return (
    <div className="card">
      <div className="text-sm text-gray-400">{label}</div>
      <div className="text-xl sm:text-2xl font-bold text-gray-800 dark:text-gray-100 tnum mt-1" dir="ltr">
        {toman(value)} <span className="text-sm font-normal text-gray-400">{t("dashboard.toman")}</span>
      </div>
      {sub}
    </div>
  );
}

export default function Dashboard() {
  const [stats, setStats] = useState(null);
  const navigate = useNavigate();
  const { t } = useLanguage();

  const load = () => fetchDashboard().then((res) => setStats(res.data));

  useEffect(() => {
    load();
    const timer = setInterval(load, 15000);
    return () => clearInterval(timer);
  }, []);

  const chartData = (stats?.usage_last_24h || []).map((d) => ({
    time: d.bucket.slice(11, 16),
    bytes: d.bytes,
    label: formatBytes(d.bytes),
  }));

  return (
    <Layout>
      <Topbar title={t("dashboard.title")} subtitle={t("dashboard.subtitle")} />

      {!stats ? (
        <div className="text-gray-400">{t("common.loading")}</div>
      ) : (
        <>
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
            <MoneyTile label={t("dashboard.salesToday")} value={stats.sales_today} t={t} />
            <MoneyTile
              label={t("dashboard.salesMonth")}
              value={stats.sales_month}
              t={t}
              sub={
                stats.sales_prev_month > 0 ? (
                  <div className={`text-xs mt-1 ${stats.sales_month >= stats.sales_prev_month ? "text-emerald-600" : "text-red-500"}`}>
                    {t("dashboard.vsPrevMonth", {
                      percent: Math.round(((stats.sales_month - stats.sales_prev_month) / stats.sales_prev_month) * 100),
                    })}
                  </div>
                ) : null
              }
            />
            <MoneyTile label={t("dashboard.salesPrevMonth")} value={stats.sales_prev_month} t={t} />
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
            <StatCard icon={Users} label={t("dashboard.totalUsers")} value={stats.total_users} tone="brand" onClick={() => navigate("/users")} />
            <StatCard icon={UserCheck} label={t("dashboard.activeUsers")} value={stats.active_users} tone="emerald" onClick={() => navigate("/users?status=active")} />
            <StatCard icon={AlertTriangle} label={t("dashboard.quotaExceeded")} value={stats.quota_exceeded_users} tone="amber" onClick={() => navigate("/users?status=quota_exceeded")} />
            <StatCard icon={UserX} label={t("dashboard.disabledUsers")} value={stats.disabled_users} tone="red" onClick={() => navigate("/users?status=disabled")} />
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
            <div className="card flex items-center gap-4">
              <div className="w-12 h-12 rounded-2xl bg-brand-50 text-brand-600 dark:bg-brand-500/10 dark:text-brand-400 flex items-center justify-center">
                <Server size={22} />
              </div>
              <div>
                <div className="text-2xl font-bold text-gray-800 dark:text-gray-100" dir="ltr">
                  {stats.online_nodes}/{stats.total_nodes}
                </div>
                <div className="text-sm text-gray-400">{t("dashboard.onlineServers")}</div>
              </div>
            </div>
            <button
              type="button"
              onClick={() => navigate("/users?online_only=1")}
              className="card flex items-center gap-4 w-full text-right cursor-pointer hover:shadow-md hover:-translate-y-0.5 transition"
            >
              <div className="w-12 h-12 rounded-2xl bg-emerald-50 text-emerald-600 dark:bg-emerald-500/10 dark:text-emerald-400 flex items-center justify-center">
                <Wifi size={22} />
              </div>
              <div>
                <div className="text-2xl font-bold text-gray-800 dark:text-gray-100" dir="ltr">
                  {stats.online_users_now}
                </div>
                <div className="text-sm text-gray-400">{t("dashboard.onlineUsersNow")}</div>
              </div>
            </button>
            <div className="card flex items-center gap-4">
              <div className="w-12 h-12 rounded-2xl bg-brand-50 text-brand-600 dark:bg-brand-500/10 dark:text-brand-400 flex items-center justify-center">
                <Database size={22} />
              </div>
              <div>
                <div className="text-2xl font-bold text-gray-800 dark:text-gray-100 whitespace-nowrap" dir="ltr">
                  {formatBytes(stats.total_used_bytes)}
                  <span className="text-sm text-gray-400 font-normal"> / {stats.total_quota_bytes ? formatBytes(stats.total_quota_bytes) : t("userDetail.unlimited")}</span>
                </div>
                <div className="text-sm text-gray-400">{t("dashboard.totalUsageAllUsers")}</div>
              </div>
            </div>
            <div className="card flex items-center gap-4">
              <div className="w-12 h-12 rounded-2xl bg-amber-50 text-amber-600 dark:bg-amber-500/10 dark:text-amber-400 flex items-center justify-center">
                <Activity size={22} />
              </div>
              <div>
                <div className="text-2xl font-bold text-gray-800 dark:text-gray-100 whitespace-nowrap" dir="ltr">
                  {formatBitrate(stats.avg_speed_bps)}
                </div>
                <div className="text-sm text-gray-400">{t("dashboard.avgSpeed")}</div>
              </div>
            </div>
            {stats.admin_balance != null && (
              <div className="card flex items-center gap-4">
                <div className="w-12 h-12 rounded-2xl bg-emerald-50 text-emerald-600 dark:bg-emerald-500/10 dark:text-emerald-400 flex items-center justify-center">
                  <Wallet size={22} />
                </div>
                <div>
                  <div className="text-2xl font-bold text-gray-800 dark:text-gray-100" dir="ltr">
                    {new Intl.NumberFormat("fa-IR").format(stats.admin_balance)} <span className="text-sm text-gray-400 font-normal">{t("dashboard.tomanUnit")}</span>
                  </div>
                  <div className="text-sm text-gray-400">{t("dashboard.yourBalance")}</div>
                </div>
              </div>
            )}
          </div>

          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 mb-6">
            {stats.system_cpu_percent != null && (
              <div className="card">
                <h3 className="font-bold text-gray-700 dark:text-gray-300 mb-4">{t("dashboard.systemStatus")}</h3>
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
              <h3 className="font-bold text-gray-700 dark:text-gray-300 mb-4">{t("dashboard.protocolStatus")}</h3>
              <div className="grid grid-cols-2 gap-2.5">
                {Object.entries(PROTOCOL_LABELS).map(([key, label]) => {
                  const count = stats.protocol_connection_counts?.[key] || 0;
                  const active = count > 0;
                  return (
                    <div
                      key={key}
                      className="flex items-center justify-between rounded-xl border border-gray-100 dark:border-slate-800 px-3 py-2.5"
                    >
                      <span className="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-200">
                        {active ? (
                          <Shield size={14} className="text-emerald-500 shrink-0" />
                        ) : (
                          <ShieldOff size={14} className="text-gray-300 dark:text-gray-600 shrink-0" />
                        )}
                        {label}
                      </span>
                      <span
                        className={`text-xs font-medium ${
                          active ? "text-emerald-600 dark:text-emerald-400" : "text-gray-300 dark:text-gray-600"
                        }`}
                        dir="ltr"
                      >
                        {count}
                      </span>
                    </div>
                  );
                })}
              </div>
            </div>
          </div>

          <div className="card">
            <h3 className="font-bold text-gray-700 dark:text-gray-300 mb-4">{t("dashboard.usageLast24h")}</h3>
            <ResponsiveContainer width="100%" height={280}>
              <AreaChart data={chartData}>
                <defs>
                  <linearGradient id="colorUsage" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="5%" stopColor="#4763f5" stopOpacity={0.4} />
                    <stop offset="95%" stopColor="#4763f5" stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
                <XAxis dataKey="time" tick={{ fontSize: 12 }} />
                <YAxis tickFormatter={(v) => formatBytes(v)} tick={{ fontSize: 12 }} width={70} />
                <Tooltip formatter={(v) => formatBytes(v)} labelFormatter={(l) => t("dashboard.hourLabel", { value: l })} />
                <Area type="monotone" dataKey="bytes" stroke="#4763f5" fill="url(#colorUsage)" strokeWidth={2} />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        </>
      )}
    </Layout>
  );
}
