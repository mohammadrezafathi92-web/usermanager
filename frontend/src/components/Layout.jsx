import React, { createContext, useCallback, useState } from "react";
import Sidebar from "./Sidebar.jsx";
import { useLanguage } from "../context/LanguageContext.jsx";

// Lets Topbar (rendered deep inside {children} on every page) open the
// Sidebar's mobile drawer without prop-drilling through every single page
// component - both just import { MobileNavContext } from here.
export const MobileNavContext = createContext(null);

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
        <main className="flex-1 p-4 sm:p-6 pb-16 max-w-[1400px] w-full mx-auto min-w-0">{children}</main>
      </div>
    </MobileNavContext.Provider>
  );
}
