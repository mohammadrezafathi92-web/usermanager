import React, { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Network, LogIn, Languages, KeyRound, Copy } from "lucide-react";
import { useAuth } from "../context/AuthContext.jsx";
import { useLanguage } from "../context/LanguageContext.jsx";
import { fetchLicenseState, activateLicense } from "../api/client.js";
import { errorText } from "../utils.js";

export default function Login() {
  const { login } = useAuth();
  const { t, dir, toggleLanguage } = useLanguage();
  const navigate = useNavigate();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  // A panel with no licence refuses logins, and the licence form used to
  // live behind that login - so a fresh install had no way in at all. The
  // login screen asks up front whether the panel is licence-locked and, if
  // it is, offers the key form right here (2026-09).
  const [lic, setLic] = useState(null);
  const [showActivate, setShowActivate] = useState(false);
  const [licKey, setLicKey] = useState("");
  const [licError, setLicError] = useState("");
  const [licSaving, setLicSaving] = useState(false);
  const [licDone, setLicDone] = useState(false);

  useEffect(() => {
    fetchLicenseState()
      .then((res) => {
        setLic(res.data);
        if (res.data?.locked) setShowActivate(true);
      })
      .catch(() => setLic(null));   // an older backend has no such route
  }, []);

  const submit = async (e) => {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      await login(username, password);
      navigate("/");
    } catch (err) {
      // No `response` at all means the request never got an HTTP answer -
      // DNS, TLS, a dead proxy, no network. That is a very different problem
      // from a rejected credential and must not be reported as one.
      if (!err?.response) setError(t("login.networkError"));
      else setError(err.response?.data?.detail || t("login.error"));
      // A 403 here is the licence lock, not a bad password - surface the
      // key form instead of leaving them retyping a correct password.
      if (err?.response?.status === 403) setShowActivate(true);
    } finally {
      setLoading(false);
    }
  };

  const submitLicense = async (e) => {
    e.preventDefault();
    setLicError("");
    setLicSaving(true);
    try {
      await activateLicense({ username, password, key: licKey.trim() });
      setLicDone(true);
      setLicError("");
      // The key is in; a normal login now works.
      try {
        await login(username, password);
        navigate("/");
      } catch {
        /* let them press the login button themselves */
      }
    } catch (err) {
      setLicError(errorText(err, t("login.licenseError")));
    } finally {
      setLicSaving(false);
    }
  };

  return (
    <div className="min-h-screen flex items-center justify-center bg-gradient-to-br from-brand-700 to-brand-900 p-4" dir={dir}>
      <div className="bg-white dark:bg-slate-900 rounded-2xl shadow-xl w-full max-w-sm p-8 relative">
        <button
          type="button"
          onClick={toggleLanguage}
          className="absolute top-4 left-4 text-xs text-gray-400 hover:text-gray-600 flex items-center gap-1"
        >
          <Languages size={14} /> {t("nav.language")}
        </button>
        <div className="flex flex-col items-center mb-6">
          <div className="w-14 h-14 rounded-2xl bg-brand-600 flex items-center justify-center text-white mb-3">
            <Network size={28} />
          </div>
          <h1 className="font-bold text-lg text-gray-800">{t("login.title")}</h1>
          <p className="text-sm text-gray-400 mt-1">{t("login.subtitle")}</p>
        </div>

        <form onSubmit={submit} className="space-y-4">
          <div>
            <label className="block text-sm text-gray-600 mb-1">{t("login.username")}</label>
            <input className="input" value={username} onChange={(e) => setUsername(e.target.value)} required autoFocus />
          </div>
          <div>
            <label className="block text-sm text-gray-600 mb-1">{t("login.password")}</label>
            <input type="password" className="input" value={password} onChange={(e) => setPassword(e.target.value)} required />
          </div>
          {error && <div className="text-sm text-red-500 bg-red-50 rounded-lg px-3 py-2">{error}</div>}
          <button type="submit" disabled={loading} className="btn-primary w-full">
            <LogIn size={16} />
            {loading ? t("login.submitting") : t("login.submit")}
          </button>
        </form>

        {showActivate && !licDone && (
          <form onSubmit={submitLicense} className="mt-6 pt-5 border-t border-gray-100 space-y-3">
            <div className="flex items-center gap-2 text-sm font-medium text-gray-700">
              <KeyRound size={16} className="text-amber-500" />
              {t("login.licenseNeeded")}
            </div>
            <p className="text-xs text-gray-400">
              {lic?.message || t("login.licenseNeededHint")}
            </p>

            {/* The vendor needs this to cut a key bound to this machine, so
                it is shown before anything is entered, not after. */}
            {lic?.fingerprint && (
              <div>
                <label className="block text-xs text-gray-500 mb-1">{t("login.fingerprint")}</label>
                <div className="flex items-center gap-2">
                  <code className="flex-1 text-[11px] bg-gray-50 rounded-lg px-2 py-1.5 break-all" dir="ltr">
                    {lic.fingerprint}
                  </code>
                  <button
                    type="button"
                    className="btn-ghost btn-icon"
                    title={t("common.copy")}
                    onClick={() => navigator.clipboard?.writeText(lic.fingerprint)}
                  >
                    <Copy size={16} />
                  </button>
                </div>
              </div>
            )}

            <div>
              <label className="block text-xs text-gray-500 mb-1">{t("login.licenseKey")}</label>
              <textarea
                className="input-area text-[11px] font-mono"
                rows={3}
                dir="ltr"
                placeholder="NETCIP1...."
                value={licKey}
                onChange={(e) => setLicKey(e.target.value)}
              />
              <div className="hint">{t("login.licenseKeyHint")}</div>
            </div>

            {licError && <div className="text-sm text-red-500 bg-red-50 rounded-lg px-3 py-2">{licError}</div>}
            <button
              type="submit"
              disabled={licSaving || !licKey.trim() || !username || !password}
              className="btn-secondary w-full"
            >
              {licSaving ? t("login.submitting") : t("login.activate")}
            </button>
          </form>
        )}

        {licDone && (
          <div className="mt-6 pt-5 border-t border-gray-100 text-sm text-emerald-600 bg-emerald-50 rounded-lg px-3 py-2">
            {t("login.licenseActivated")}
          </div>
        )}
      </div>
    </div>
  );
}
