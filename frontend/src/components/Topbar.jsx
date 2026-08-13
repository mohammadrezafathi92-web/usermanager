import React, { useContext } from "react";
import { LogOut, User, Menu } from "lucide-react";
import { useAuth } from "../context/AuthContext.jsx";
import { useLanguage } from "../context/LanguageContext.jsx";
import { MobileNavContext } from "./Layout.jsx";

/**
 * Every page's header. `actions` is optional and renders beside the account
 * controls - pages that used to scatter their primary action somewhere in
 * the body can pass it here instead so the "main action" always sits in the
 * same place. Existing callers that pass only title/subtitle are unaffected.
 *
 * Sticky on small screens only: on a phone these pages are long enough that
 * the hamburger would otherwise scroll away, stranding the user with no way
 * to switch sections without scrolling back to the top.
 */
export default function Topbar({ title, subtitle, actions = null }) {
  const { username, logout } = useAuth();
  const { t } = useLanguage();
  const nav = useContext(MobileNavContext);
  return (
    <div
      className="sticky md:static top-0 z-30 py-3 md:py-0 mb-4 sm:mb-6
                 -mx-4 px-4 sm:-mx-6 sm:px-6 md:mx-0 md:px-0
                 bg-slate-100/90 backdrop-blur supports-[backdrop-filter]:bg-slate-100/70
                 md:bg-transparent md:backdrop-blur-none
                 dark:bg-[#0b0d13]/90 dark:supports-[backdrop-filter]:bg-[#0b0d13]/70 dark:md:bg-transparent
                 flex items-center justify-between gap-2"
    >
      <div className="flex items-center gap-3 min-w-0">
        {nav && (
          <button
            type="button"
            onClick={() => nav.setMobileOpen(true)}
            className="md:hidden btn-secondary btn-icon shrink-0"
            aria-label={t("topbar.openMenu")}
          >
            <Menu size={18} />
          </button>
        )}
        <div className="min-w-0">
          <h1 className="page-title truncate">{title}</h1>
          {subtitle && <p className="text-sm text-gray-400 mt-0.5 truncate">{subtitle}</p>}
        </div>
      </div>
      <div className="flex items-center gap-2 sm:gap-3 shrink-0">
        {actions}
        <div className="hidden sm:flex items-center gap-2 bg-white rounded-xl border border-gray-200/70 px-3 py-2 text-sm text-gray-600 dark:bg-slate-900 dark:border-slate-800 dark:text-gray-300">
          <User size={16} />
          <span className="max-w-[10rem] truncate">{username || t("topbar.admin")}</span>
        </div>
        <button onClick={logout} className="btn-secondary btn-icon" title={t("topbar.logout")} aria-label={t("topbar.logout")}>
          <LogOut size={16} />
        </button>
      </div>
    </div>
  );
}
