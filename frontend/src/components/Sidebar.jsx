import React, { useEffect, useState } from "react";
import { NavLink } from "react-router-dom";
import { LayoutDashboard, Users, Server, Settings, Network, Package, GraduationCap, ShieldCheck, Sun, Moon, X, Languages } from "lucide-react";
import { useAuth } from "../context/AuthContext.jsx";
import { useLanguage } from "../context/LanguageContext.jsx";

const allLinks = [
  { to: "/", labelKey: "nav.dashboard", icon: LayoutDashboard, end: true, perm: null },
  { to: "/users", labelKey: "nav.users", icon: Users, perm: null },
  { to: "/nodes", labelKey: "nav.nodes", icon: Server, perm: "manage_nodes" },
  { to: "/packages", labelKey: "nav.packages", icon: Package, perm: "manage_packages" },
  { to: "/tutorials", labelKey: "nav.tutorials", icon: GraduationCap, perm: "manage_tutorials" },
  { to: "/settings", labelKey: "nav.settings", icon: Settings, perm: "manage_settings" },
  { to: "/admins", labelKey: "nav.admins", icon: ShieldCheck, perm: "__superadmin__" },
];

export default function Sidebar({ mobileOpen = false, onClose = () => {} }) {
  const { can, isSuperadmin } = useAuth();
  const { t, toggleLanguage } = useLanguage();
  const [dark, setDark] = useState(() => {
    try {
      return localStorage.getItem("theme") === "dark";
    } catch {
      return false;
    }
  });

  useEffect(() => {
    document.documentElement.classList.toggle("dark", dark);
    try {
      localStorage.setItem("theme", dark ? "dark" : "light");
    } catch {
      /* ignore (e.g. private mode) */
    }
  }, [dark]);

  const links = allLinks.filter((l) => {
    if (l.perm === null) return true;
    if (l.perm === "__superadmin__") return isSuperadmin;
    return can(l.perm);
  });

  return (
    <>
      {/* Mobile-only backdrop - tapping it closes the drawer, same as X */}
      {mobileOpen && (
        <div className="fixed inset-0 bg-black/40 z-40 md:hidden" onClick={onClose} />
      )}

      <aside
        className={`fixed md:sticky top-0 right-0 z-50 md:z-auto h-screen w-64 flex flex-col
        bg-white border-l border-gray-100 dark:bg-slate-900 dark:border-slate-800
        transition-transform duration-200 md:translate-x-0
        ${mobileOpen ? "translate-x-0" : "translate-x-full"}`}
      >
        <div className="flex items-center gap-2 px-6 py-5">
          <div className="w-9 h-9 rounded-xl bg-brand-600 flex items-center justify-center text-white">
            <Network size={18} />
          </div>
          <div className="flex-1">
            <div className="font-bold text-gray-800 dark:text-gray-100 leading-none">{t("nav.appName")}</div>
            <div className="text-xs text-gray-400 mt-1">{t("nav.tagline")}</div>
          </div>
          <button type="button" onClick={onClose} className="md:hidden text-gray-400 hover:text-gray-600 dark:hover:text-gray-200" aria-label={t("nav.closeMenu")}>
            <X size={20} />
          </button>
        </div>

        <nav className="flex-1 px-3 space-y-1 mt-2 overflow-y-auto">
          {links.map(({ to, labelKey, icon: Icon, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              onClick={onClose}
              className={({ isActive }) =>
                `flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-medium transition-colors ${
                  isActive
                    ? "bg-brand-50 text-brand-700 dark:bg-brand-500/10 dark:text-brand-400"
                    : "text-gray-500 hover:bg-gray-50 dark:text-gray-400 dark:hover:bg-slate-800"
                }`
              }
            >
              <Icon size={18} />
              {t(labelKey)}
            </NavLink>
          ))}
        </nav>

        <div className="px-3 pb-2 space-y-1">
          <button
            type="button"
            onClick={toggleLanguage}
            className="w-full flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-medium text-gray-500 hover:bg-gray-50 dark:text-gray-400 dark:hover:bg-slate-800 transition-colors"
          >
            <Languages size={18} />
            {t("nav.language")}
          </button>
          <button
            type="button"
            onClick={() => setDark((d) => !d)}
            className="w-full flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-medium text-gray-500 hover:bg-gray-50 dark:text-gray-400 dark:hover:bg-slate-800 transition-colors"
          >
            {dark ? <Sun size={18} /> : <Moon size={18} />}
            {dark ? t("nav.lightMode") : t("nav.darkMode")}
          </button>
        </div>

        <div className="p-4 text-xs text-gray-300 dark:text-gray-600 text-center">{t("nav.version")}</div>
      </aside>
    </>
  );
}
