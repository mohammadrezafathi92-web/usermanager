import React, { createContext, useContext, useEffect, useMemo, useState } from "react";
import { translate } from "../i18n/translations.js";

const LanguageContext = createContext(null);

const STORAGE_KEY = "um_lang";

export function LanguageProvider({ children }) {
  const [language, setLanguage] = useState(() => {
    try {
      return localStorage.getItem(STORAGE_KEY) === "en" ? "en" : "fa";
    } catch {
      return "fa";
    }
  });

  useEffect(() => {
    document.documentElement.dir = language === "en" ? "ltr" : "rtl";
    document.documentElement.lang = language === "en" ? "en" : "fa";
    try {
      localStorage.setItem(STORAGE_KEY, language);
    } catch {
      /* ignore (e.g. private mode) */
    }
  }, [language]);

  const value = useMemo(
    () => ({
      language,
      dir: language === "en" ? "ltr" : "rtl",
      toggleLanguage: () => setLanguage((l) => (l === "en" ? "fa" : "en")),
      setLanguage,
      t: (key, vars) => translate(language, key, vars),
    }),
    [language]
  );

  return <LanguageContext.Provider value={value}>{children}</LanguageContext.Provider>;
}

export function useLanguage() {
  const ctx = useContext(LanguageContext);
  if (!ctx) throw new Error("useLanguage must be used within LanguageProvider");
  return ctx;
}
