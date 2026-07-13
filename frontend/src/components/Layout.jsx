import React, { createContext, useState } from "react";
import Sidebar from "./Sidebar.jsx";
import { useLanguage } from "../context/LanguageContext.jsx";

// Lets Topbar (rendered deep inside {children} on every page) open the
// Sidebar's mobile drawer without prop-drilling through every single page
// component - both just import { MobileNavContext } from here.
export const MobileNavContext = createContext(null);

export default function Layout({ children }) {
  const [mobileOpen, setMobileOpen] = useState(false);
  const { dir } = useLanguage();
  return (
    <MobileNavContext.Provider value={{ mobileOpen, setMobileOpen }}>
      <div className="flex min-h-screen" dir={dir}>
        <Sidebar mobileOpen={mobileOpen} onClose={() => setMobileOpen(false)} />
        <main className="flex-1 p-4 sm:p-6 max-w-[1400px] w-full mx-auto min-w-0">{children}</main>
      </div>
    </MobileNavContext.Provider>
  );
}
