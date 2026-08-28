import React from "react";
import { ShieldX } from "lucide-react";
import { useAuth } from "../context/AuthContext.jsx";
import { useLanguage } from "../context/LanguageContext.jsx";
import LicenseCard from "./LicenseCard.jsx";

/**
 * Full-screen, un-dismissable notice for a session that was already open
 * when the panel's licence got locked (see backend routers/auth.py's /me
 * docstring: a fresh login is already refused while locked, so this only
 * ever fires for a session that outlived the moment of locking - caught by
 * AuthContext's periodic /me poll).
 *
 * A superadmin gets the licence card embedded right here so they can paste
 * a fresh key without needing to log back in (PUT /api/license/key does
 * not itself check the licence gate - only /login does) - onChanged calls
 * refreshMe() so the overlay clears the instant a valid key is saved,
 * rather than waiting for the next 5-minute poll. Anyone else just sees
 * the message; only the superadmin can actually fix this.
 */
export default function LicenseLockOverlay() {
  const { license, isSuperadmin, refreshMe, logout } = useAuth();
  const { t } = useLanguage();

  if (!license?.locked) return null;

  return (
    <div className="fixed inset-0 z-[300] flex items-center justify-center bg-black/70 p-4 overflow-y-auto">
      <div className="bg-white rounded-2xl shadow-xl w-full max-w-lg my-8">
        <div className="p-6 space-y-4">
          <div className="flex items-center gap-3">
            <div className="w-11 h-11 rounded-xl bg-red-50 text-red-600 flex items-center justify-center shrink-0">
              <ShieldX size={22} />
            </div>
            <div>
              <div className="font-bold text-gray-800">{t("license.overlayTitle")}</div>
              <div className="text-xs text-gray-400">{t("license.overlaySubtitle")}</div>
            </div>
          </div>

          <div className="text-sm text-red-600 bg-red-50 rounded-lg px-3 py-3">
            {license.message || t("license.overlayDefaultMessage")}
          </div>

          {!isSuperadmin && (
            <div className="text-xs text-gray-400">{t("license.overlayContactSupport")}</div>
          )}

          {isSuperadmin && (
            <div className="pt-2">
              <LicenseCard t={t} onChanged={refreshMe} />
            </div>
          )}

          <button type="button" className="btn-secondary w-full" onClick={logout}>
            {t("topbar.logout")}
          </button>
        </div>
      </div>
    </div>
  );
}
