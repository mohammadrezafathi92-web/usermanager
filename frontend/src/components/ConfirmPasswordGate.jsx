import React, { useEffect, useState } from "react";
import { ShieldAlert } from "lucide-react";
import { setPasswordPrompt } from "../api/client.js";
import { useLanguage } from "../context/LanguageContext.jsx";

/**
 * Asks for the logged-in admin's own password before any destructive
 * (DELETE) request goes out - the browser-side half of the backend's
 * deps.require_confirm_password gate.
 *
 * Mounted once at the app root. Rather than making every delete button in
 * every page open its own dialog, api/client.js's request interceptor
 * calls the promise-returning prompt registered here, so ANY delete -
 * including ones added later - is covered automatically and consistently.
 */
export default function ConfirmPasswordGate() {
  const { t } = useLanguage();
  const [pending, setPending] = useState(null); // { resolve, reject }
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    setPasswordPrompt(
      ({ retry }) =>
        new Promise((resolve, reject) => {
          setPassword("");
          setError(retry ? t("confirmDelete.wrongPassword") : "");
          setPending({ resolve, reject });
        })
    );
    return () => setPasswordPrompt(null);
  }, [t]);

  if (!pending) return null;

  const submit = (e) => {
    e.preventDefault();
    if (!password) return;
    pending.resolve(password);
    setPending(null);
    setPassword("");
  };

  const cancel = () => {
    // Rejecting (not resolving with "") lets the interceptor abort the
    // request entirely instead of firing a doomed one at the server.
    pending.reject(new Error("cancelled"));
    setPending(null);
    setPassword("");
  };

  return (
    <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/50 p-4">
      <form
        onSubmit={submit}
        className="bg-white dark:bg-slate-900 rounded-2xl shadow-xl w-full max-w-sm p-5 space-y-4"
      >
        <div className="flex items-center gap-2">
          <div className="w-9 h-9 rounded-xl bg-red-50 text-red-600 dark:bg-red-500/10 flex items-center justify-center">
            <ShieldAlert size={18} />
          </div>
          <div className="font-bold text-gray-800 dark:text-gray-100">{t("confirmDelete.title")}</div>
        </div>

        <div className="text-xs text-gray-500 dark:text-gray-400">{t("confirmDelete.hint")}</div>

        <input
          type="password"
          autoFocus
          className="input w-full"
          placeholder={t("confirmDelete.placeholder")}
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
        {error && <div className="text-xs text-red-500">{error}</div>}

        <div className="flex gap-2">
          <button type="button" className="btn-secondary flex-1" onClick={cancel}>
            {t("common.cancel")}
          </button>
          <button type="submit" disabled={!password} className="btn-danger flex-1">
            {t("confirmDelete.confirm")}
          </button>
        </div>
      </form>
    </div>
  );
}
