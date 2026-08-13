import React, { useEffect, useRef, useState } from "react";
import { NavLink } from "react-router-dom";
import { LayoutDashboard, Users, Server, Settings, Network, Package, GraduationCap, ShieldCheck, ShieldAlert, Ticket, Sun, Moon, X, Languages, Calculator } from "lucide-react";
import { useAuth } from "../context/AuthContext.jsx";
import { useLanguage } from "../context/LanguageContext.jsx";

// `perm` may be a single permission string or an array meaning "any one of
// these is enough" (see AuthContext.jsx's canAny). "__admin_or_above__"
// means superadmin or level-2 Admin only - a level-3 Seller never sees it.
//
// Menu audit (3-tier hierarchy): Nodes stays structurally Admin-tier-only
// on the backend too (see routers/nodes.py's create_node/accessible_node_ids
// - a Seller can never own or be granted a node) - so its sidebar entry is
// gated accordingly instead of a permission checkbox that could never
// actually grant a Seller anything real.
// Packages, Discount codes, and Settings are real, useful, and already
// internally Seller-aware pages (Packages.jsx hides create/edit/delete and
// shows the Seller's own resale-price editor instead; DiscountCodes.jsx
// lets a Seller manage their OWN codes and hides edit/delete on anything
// they don't own - see routers/discount_codes.py's per-tier ownership,
// confirmed with the panel owner 2026-07-19; Settings.jsx hides the
// superadmin-only/Admin-only cards and shows only password + own-bot +
// own-backup + own-payment for a Seller) - so their links are
// unconditionally visible and each page does its own finer-grained gating
// internally, exactly like Users/Dashboard already did.
const allLinks = [
  { to: "/", labelKey: "nav.dashboard", icon: LayoutDashboard, end: true, perm: null },
  { to: "/users", labelKey: "nav.users", icon: Users, perm: null },
  { to: "/nodes", labelKey: "nav.nodes", icon: Server, perm: "__admin_or_above__" },
  { to: "/packages", labelKey: "nav.packages", icon: Package, perm: null },
  { to: "/tutorials", labelKey: "nav.tutorials", icon: GraduationCap, perm: "view_tutorials" },
  { to: "/radius-logs", labelKey: "nav.radiusLogs", icon: ShieldAlert, perm: null },
  { to: "/discount-codes", labelKey: "nav.discountCodes", icon: Ticket, perm: null },
  // All three tiers see this - the backend scopes what each role's numbers
  // cover (superadmin: whole panel; level-2 Admin: own tree; Seller: self) -
  // see routers/accounting.py.
  { to: "/accounting", labelKey: "nav.accounting", icon: Calculator, perm: null },
  { to: "/settings", labelKey: "nav.settings", icon: Settings, perm: null },
  // Superadmins manage level-2 Admins here; level-2 Admins ALSO see this
  // page (to manage their OWN level-3 Sellers - see routers/admins.py's
  // require_admin_or_above) - only a level-3 Seller never sees it at all.
  { to: "/admins", labelKey: "nav.admins", icon: ShieldCheck, perm: "__admin_or_above__" },
];

const navItemClass = ({ isActive }) =>
  `flex items-center gap-3 rounded-xl px-3 min-h-11 text-sm font-medium transition-colors ${
    isActive
      ? "bg-brand-50 text-brand-700 dark:bg-brand-500/10 dark:text-brand-400"
      : "text-gray-500 hover:bg-gray-50 dark:text-gray-400 dark:hover:bg-slate-800"
  }`;

export default function Sidebar({ mobileOpen = false, onClose = () => {} }) {
  const { canAny, isSuperadmin, isAdminOrAbove, build } = useAuth();
  const { t, toggleLanguage, dir } = useLanguage();
  const panelRef = useRef(null);
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

  // Mobile drawer behaviour. Without these two the drawer was openable but
  // not properly dismissible: Escape did nothing, and the page underneath
  // kept scrolling behind the overlay, so closing it could leave you at a
  // completely different scroll position than where you opened it.
  useEffect(() => {
    if (!mobileOpen) return undefined;
    const onKey = (e) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    panelRef.current?.focus();
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = previousOverflow;
    };
  }, [mobileOpen, onClose]);

  // Which edge the closed drawer parks on. Both are plain utilities, so the
  // md: rule below reliably overrides them on desktop.
  const offCanvas = dir === "ltr" ? "-translate-x-full" : "translate-x-full";

  const links = allLinks.filter((l) => {
    if (l.perm === null) return true;
    if (l.perm === "__superadmin__") return isSuperadmin;
    if (l.perm === "__admin_or_above__") return isAdminOrAbove;
    return canAny(Array.isArray(l.perm) ? l.perm : [l.perm]);
  });

  return (
    <>
      {/* Mobile-only backdrop - tapping it closes the drawer, same as X */}
      {mobileOpen && (
        <div className="fixed inset-0 bg-black/40 backdrop-blur-[1px] z-40 md:hidden" onClick={onClose} />
      )}

      {/*
        Positioning is direction-agnostic on purpose. This panel supports both
        Persian (RTL) and English (LTR) via i18n, and the previous
        `right-0 / translate-x-full` pair was hardcoded for RTL: switching the
        panel to English left the sidebar pinned to the right of an LTR layout
        and made the closed drawer slide off the wrong edge.

        It anchors to inline-START, which is the RIGHT edge in Persian and the
        left edge in English - i.e. exactly where a sidebar belongs in each.
        Worth stating plainly because the intuition runs the other way: an
        earlier attempt used `end-0`, reasoning "the sidebar is on the right,
        so it is the end". In RTL inline-end is the LEFT edge, so on mobile the
        drawer pinned itself to the left and the hide transform then pushed it
        into the middle of the screen instead of off it - visible as a panel
        stuck open over the page, with the hamburger buried underneath.
        `border-e` follows for the same reason: the border faces the content,
        which sits on the sidebar's inline-end side.

        The slide-out direction is chosen in JS (offCanvas below) rather than
        with Tailwind's rtl:/ltr: variants. Those compile to `:where([dir=...])`
        selectors, which carry ZERO specificity and are emitted after the
        responsive variants - so they both lost to, and won over, the md:
        reset depending only on file order, and cost two rounds of "the
        sidebar vanished on desktop" / "the drawer won't slide away on mobile".
        A plain unscoped utility has predictable precedence against the md:
        rule (media query, emitted later, wins), which is exactly how this
        worked before. `start-0` and `border-e` stay logical, because those
        are ordinary properties with no such ordering trap - only the
        transform variants have it, since CSS transforms have no logical
        equivalent and are always physical.
      */}
      <aside
        ref={panelRef}
        tabIndex={-1}
        role={mobileOpen ? "dialog" : undefined}
        aria-modal={mobileOpen ? true : undefined}
        aria-label={t("nav.appName")}
        className={`fixed md:sticky top-0 start-0 z-50 md:z-auto h-screen w-72 sm:w-64 flex flex-col
        bg-white border-e border-gray-200/70 dark:bg-slate-900 dark:border-slate-800
        transition-transform duration-200 md:translate-x-0 focus:outline-none
        ${mobileOpen ? "translate-x-0" : offCanvas}`}
      >
        <div className="flex items-center gap-2 px-5 py-5">
          <div className="w-9 h-9 rounded-xl bg-brand-600 flex items-center justify-center text-white shrink-0">
            <Network size={18} />
          </div>
          <div className="flex-1 min-w-0">
            <div className="font-bold text-gray-800 dark:text-gray-100 leading-none truncate">{t("nav.appName")}</div>
            <div className="text-xs text-gray-400 mt-1 truncate">{t("nav.tagline")}</div>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="md:hidden btn-ghost btn-icon shrink-0"
            aria-label={t("nav.closeMenu")}
          >
            <X size={20} />
          </button>
        </div>

        <nav className="flex-1 px-3 space-y-1 mt-2 overflow-y-auto">
          {links.map(({ to, labelKey, icon: Icon, end }) => (
            <NavLink key={to} to={to} end={end} onClick={onClose} className={navItemClass}>
              <Icon size={18} className="shrink-0" />
              <span className="truncate">{t(labelKey)}</span>
            </NavLink>
          ))}
        </nav>

        <div className="px-3 pb-2 space-y-1 border-t border-gray-100 pt-2 dark:border-slate-800">
          <button
            type="button"
            onClick={toggleLanguage}
            className="w-full flex items-center gap-3 rounded-xl px-3 min-h-11 text-sm font-medium text-gray-500 hover:bg-gray-50 dark:text-gray-400 dark:hover:bg-slate-800 transition-colors"
          >
            <Languages size={18} className="shrink-0" />
            {t("nav.language")}
          </button>
          <button
            type="button"
            onClick={() => setDark((d) => !d)}
            className="w-full flex items-center gap-3 rounded-xl px-3 min-h-11 text-sm font-medium text-gray-500 hover:bg-gray-50 dark:text-gray-400 dark:hover:bg-slate-800 transition-colors"
          >
            {dark ? <Sun size={18} className="shrink-0" /> : <Moon size={18} className="shrink-0" />}
            {dark ? t("nav.lightMode") : t("nav.darkMode")}
          </button>
        </div>

        {/* Was a hardcoded "نسخه ۱.۰" translation string that had never once
            changed. Now the real thing, straight from the running backend -
            with the commit id for superadmins, which is what actually answers
            "is my change deployed?". */}
        <div className="p-4 text-xs text-gray-300 dark:text-gray-600 text-center" dir="ltr">
          {build?.version ? `v${build.version}` : t("nav.version")}
          {build?.commit && <span className="opacity-70"> · {build.commit}</span>}
        </div>
      </aside>
    </>
  );
}
