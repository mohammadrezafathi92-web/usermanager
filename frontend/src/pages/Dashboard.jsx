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
