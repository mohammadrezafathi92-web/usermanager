import React, { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Users, UserCheck, UserX, AlertTriangle, Server, Database, Wifi, Wallet, Activity } from "lucide-react";
import { AreaChart, Area, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid } from "recharts";
import Layout from "../components/Layout.jsx";
import Topbar from "../components/Topbar.jsx";
import StatCard from "../components/StatCard.jsx";
import { fetchDashboard } from "../api/client.js";
import { formatBytes } from "../utils.js";

export default function Dashboard() {
  const [stats, setStats] = useState(null);
  const navigate = useNavigate();

  const load = () => fetchDashboard().then((res) => setStats(res.data));

  useEffect(() => {
    load();
    const t = setInterval(load, 15000);
    return () => clearInterval(t);
  }, []);

  const chartData = (stats?.usage_last_24h || []).map((d) => ({
    time: d.bucket.slice(11, 16),
    bytes: d.bytes,
    label: formatBytes(d.bytes),
  }));

  return (
    <Layout>
      <Topbar title="داشبورد" subtitle="نمای کلی مصرف و وضعیت کاربران" />

      {!stats ? (
        <div className="text-gray-400">در حال بارگذاری...</div>
      ) : (
        <>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
            <StatCard icon={Users} label="کل کاربران" value={stats.total_users} tone="brand" onClick={() => navigate("/users")} />
            <StatCard icon={UserCheck} label="کاربران فعال" value={stats.active_users} tone="emerald" onClick={() => navigate("/users?status=active")} />
            <StatCard icon={AlertTriangle} label="اتمام حجم" value={stats.quota_exceeded_users} tone="amber" onClick={() => navigate("/users?status=quota_exceeded")} />
            <StatCard icon={UserX} label="غیرفعال" value={stats.disabled_users} tone="red" onClick={() => navigate("/users?status=disabled")} />
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
                <div className="text-sm text-gray-400">سرورهای آنلاین</div>
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
                <div className="text-sm text-gray-400">کاربران آنلاین الان</div>
              </div>
            </button>
            <div className="card flex items-center gap-4">
              <div className="w-12 h-12 rounded-2xl bg-brand-50 text-brand-600 dark:bg-brand-500/10 dark:text-brand-400 flex items-center justify-center">
                <Database size={22} />
              </div>
              <div>
                <div className="text-2xl font-bold text-gray-800 dark:text-gray-100 whitespace-nowrap" dir="ltr">
                  {formatBytes(stats.total_used_bytes)}
                  <span className="text-sm text-gray-400 font-normal"> / {stats.total_quota_bytes ? formatBytes(stats.total_quota_bytes) : "نامحدود"}</span>
                </div>
                <div className="text-sm text-gray-400">مجموع مصرف همه کاربران</div>
              </div>
            </div>
            <div className="card flex items-center gap-4">
              <div className="w-12 h-12 rounded-2xl bg-amber-50 text-amber-600 dark:bg-amber-500/10 dark:text-amber-400 flex items-center justify-center">
                <Activity size={22} />
              </div>
              <div>
                <div className="text-2xl font-bold text-gray-800 dark:text-gray-100 whitespace-nowrap" dir="ltr">
                  {formatBytes(stats.avg_speed_bps)}
                  <span className="text-sm text-gray-400 font-normal">/s</span>
                </div>
                <div className="text-sm text-gray-400">میانگین سرعت مصرف (۱ دقیقه اخیر)</div>
              </div>
            </div>
            {stats.admin_balance != null && (
              <div className="card flex items-center gap-4">
                <div className="w-12 h-12 rounded-2xl bg-emerald-50 text-emerald-600 dark:bg-emerald-500/10 dark:text-emerald-400 flex items-center justify-center">
                  <Wallet size={22} />
                </div>
                <div>
                  <div className="text-2xl font-bold text-gray-800 dark:text-gray-100" dir="ltr">
                    {new Intl.NumberFormat("fa-IR").format(stats.admin_balance)} <span className="text-sm text-gray-400 font-normal">تومان</span>
                  </div>
                  <div className="text-sm text-gray-400">اعتبار فعلی شما</div>
                </div>
              </div>
            )}
          </div>

          <div className="card">
            <h3 className="font-bold text-gray-700 dark:text-gray-300 mb-4">مصرف ۲۴ ساعت اخیر</h3>
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
                <Tooltip formatter={(v) => formatBytes(v)} labelFormatter={(l) => `ساعت ${l}`} />
                <Area type="monotone" dataKey="bytes" stroke="#4763f5" fill="url(#colorUsage)" strokeWidth={2} />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        </>
      )}
    </Layout>
  );
}
