import React from "react";
import { NavLink } from "react-router-dom";
import { LayoutDashboard, Users, Server, Settings, Network, Package, GraduationCap, ShieldCheck } from "lucide-react";
import { useAuth } from "../context/AuthContext.jsx";

const allLinks = [
  { to: "/", label: "داشبورد", icon: LayoutDashboard, end: true, perm: null },
  { to: "/users", label: "کاربران", icon: Users, perm: null },
  { to: "/nodes", label: "سرورها (نودها)", icon: Server, perm: "manage_nodes" },
  { to: "/packages", label: "پکیج‌ها", icon: Package, perm: "manage_packages" },
  { to: "/tutorials", label: "آموزش‌ها", icon: GraduationCap, perm: "manage_tutorials" },
  { to: "/settings", label: "تنظیمات", icon: Settings, perm: "manage_settings" },
  { to: "/admins", label: "مدیریت ادمین‌ها", icon: ShieldCheck, perm: "__superadmin__" },
];

export default function Sidebar() {
  const { can, isSuperadmin } = useAuth();
  const links = allLinks.filter((l) => {
    if (l.perm === null) return true;
    if (l.perm === "__superadmin__") return isSuperadmin;
    return can(l.perm);
  });

  return (
    <aside className="hidden md:flex md:w-64 flex-col bg-white border-l border-gray-100 h-screen sticky top-0">
      <div className="flex items-center gap-2 px-6 py-5">
        <div className="w-9 h-9 rounded-xl bg-brand-600 flex items-center justify-center text-white">
          <Network size={18} />
        </div>
        <div>
          <div className="font-bold text-gray-800 leading-none">یوزر منیجر</div>
          <div className="text-xs text-gray-400 mt-1">مدیریت یکپارچه اتصالات</div>
        </div>
      </div>

      <nav className="flex-1 px-3 space-y-1 mt-2">
        {links.map(({ to, label, icon: Icon, end }) => (
          <NavLink
            key={to}
            to={to}
            end={end}
            className={({ isActive }) =>
              `flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-medium transition-colors ${
                isActive ? "bg-brand-50 text-brand-700" : "text-gray-500 hover:bg-gray-50"
              }`
            }
          >
            <Icon size={18} />
            {label}
          </NavLink>
        ))}
      </nav>

      <div className="p-4 text-xs text-gray-300 text-center">نسخه ۱.۰</div>
    </aside>
  );
}
