import React, { useContext } from "react";
import { LogOut, User, Menu } from "lucide-react";
import { useAuth } from "../context/AuthContext.jsx";
import { useLanguage } from "../context/LanguageContext.jsx";
import { MobileNavContext } from "./Layout.jsx";

export default function Topbar({ title, subtitle }) {
  const { username, logout } = useAuth();
  const { t } = useLanguage();
  const nav = useContext(MobileNavContext);
  return (
    <div className="flex items-center justify-between mb-6 gap-2">
      <div className="flex items-center gap-3 min-w-0">
        {nav && (
          <button
            type="button"
            onClick={() => nav.setMobileOpen(true)}
            className="md:hidden btn-secondary !px-2.5 !py-2.5 shrink-0"
            aria-label={t("topbar.openMenu")}
          >
            <Menu size={18} />
          </button>
        )}
        <div className="min-w-0">
          <h1 className="text-xl font-bold text-gray-800 dark:text-gray-100 truncate">{title}</h1>
          {subtitle && <p className="text-sm text-gray-400 mt-1 truncate">{subtitle}</p>}
        </div>
      </div>
      <div className="flex items-center gap-3 shrink-0">
        <div className="hidden sm:flex items-center gap-2 bg-white rounded-xl border border-gray-100 px-3 py-2 text-sm text-gray-600 dark:bg-slate-900 dark:border-slate-800 dark:text-gray-300">
          <User size={16} />
          {username || t("topbar.admin")}
        </div>
        <button onClick={logout} className="btn-secondary" title={t("topbar.logout")}>
          <LogOut size={16} />
        </button>
      </div>
    </div>
  );
}
