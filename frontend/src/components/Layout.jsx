import React, { createContext, useCallback, useState } from "react";
import { Link } from "react-router-dom";
import { ShieldAlert } from "lucide-react";
import Sidebar from "./Sidebar.jsx";
import { useLanguage } from "../context/LanguageContext.jsx";
import { useAuth } from "../context/AuthContext.jsx";

// Lets Topbar (rendered deep inside {children} on every page) open the
// Sidebar's mobile drawer without prop-drilling through every single page
// component - both just import { MobileNavContext } from here.
export const MobileNavContext = createContext(null);

/** Shown on every page while this account still uses the password printed
 *  in the repository. Deliberately not dismissible: it is one click from
 *  anyone reading the source having full control of the panel, and the
 *  startup log warning that existed before this clearly was not read.
 *
 *  Not a modal blocking the panel, though - locking someone out of their
 *  own live panel over a warning would be its own outage. */
function DefaultPasswordBanner() {
  const { passwordIsDefault } = useAuth();
  const { t } = useLanguage();
  if (!passwordIsDefault) return null;
  return (
    <div className="mb-4 rounded-xl border border-red-200 bg-red-50 px-4 py-3 flex items-start gap-3">
      <ShieldAlert size={20} className="text-red-600 shrink-0 mt-0.5" />
      <div className="text-sm text-red-700">
        <div className="font-bold mb-0.5">{t("auth.defaultPasswordTitle")}</div>
        <div className="text-xs leading-6">{t("auth.defaultPasswordBody")}</div>
        <Link to="/settings" className="text-xs font-medium underline mt-1 inline-block">
          {t("auth.defaultPasswordAction")}
        </Link>
      </div>
    </div>
  );
}

export default function Layout({ children }) {
  const [mobileOpen, setMobileOpen] = useState(false);
  const { dir } = useLanguage();
  // Stable identity. Sidebar lists this in an Effect's dependency array (for
  // the Escape handler and the background scroll lock); an inline arrow here
  // would be a new function on every render of every page, tearing down and
  // re-attaching that listener continuously while the drawer is open.
  const closeMobileNav = useCallback(() => setMobileOpen(false), []);
  return (
    <MobileNavContext.Provider value={{ mobileOpen, setMobileOpen }}>
      <div className="flex min-h-screen" dir={dir}>
        <Sidebar mobileOpen={mobileOpen} onClose={closeMobileNav} />
        {/* min-w-0 is load-bearing: without it a wide table inside a flex
            child refuses to shrink and pushes the whole layout sideways
            instead of scrolling within its own .table-wrap. */}
        <main className="flex-1 p-4 sm:p-6 pb-16 max-w-[1400px] w-full mx-auto min-w-0">
          <DefaultPasswordBanner />
          {children}
        </main>
      </div>
    </MobileNavContext.Provider>
  );
}
