import React, { useEffect, useState } from "react";
import { Lock, ShieldCheck, AlertTriangle } from "lucide-react";
import { fetchTlsState, checkTlsDns, enableTls, disableTls } from "../api/client.js";
import { useLanguage } from "../context/LanguageContext.jsx";

/**
 * «دامنه و SSL» - gives this panel its own hostname and an HTTPS certificate.
 *
 * Two reasons it exists, in order of weight:
 *   1. A Telegram Mini App only ever loads over https, and the page then has
 *      to call THIS panel's API - a browser will not let an https page talk
 *      to http://<ip>. So a central https server cannot stand in for it.
 *   2. Until this, every reseller typed their panel password into a
 *      plain-HTTP login page.
 *
 * The DNS check is its own button and runs before anything is changed. A
 * failed certificate issuance is rate-limited to five per hostname per week,
 * and its error ("challenge failed") says nothing about the actual cause,
 * which is almost always a record that was never made or has not propagated.
 * One lookup turns that into a sentence the operator can act on.
 */
export default function PanelTlsCard() {
  const { t } = useLanguage();
  const [state, setState] = useState(null);
  const [domain, setDomain] = useState("");
  const [email, setEmail] = useState("");
  const [dns, setDns] = useState(null);
  const [checking, setChecking] = useState(false);
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState(null);

  const load = () =>
    fetchTlsState()
      .then((r) => {
        setState(r.data);
        setDomain(r.data.domain || "");
        setEmail(r.data.email || "");
      })
      .catch(() => {});

  useEffect(() => {
    load();
  }, []);

  const runCheck = async () => {
    setChecking(true);
    setDns(null);
    setMsg(null);
    try {
      const res = await checkTlsDns(domain);
      setDns(res.data);
    } catch (err) {
      setMsg({ type: "err", text: err?.response?.data?.detail || t("settings.msgSaveError") });
    } finally {
      setChecking(false);
    }
  };

  const submit = async (force) => {
    setSaving(true);
    setMsg(null);
    try {
      const res = await enableTls(domain, email, force);
      setMsg({ type: "ok", text: res.data.message });
      load();
    } catch (err) {
      setMsg({ type: "err", text: err?.response?.data?.detail || t("settings.msgSaveError") });
    } finally {
      setSaving(false);
    }
  };

  const turnOff = async () => {
    if (!window.confirm(t("settings.tlsDisableConfirm"))) return;
    setSaving(true);
    setMsg(null);
    try {
      const res = await disableTls();
      setMsg({ type: "ok", text: res.data.message });
      load();
    } catch (err) {
      setMsg({ type: "err", text: err?.response?.data?.detail || t("settings.msgSaveError") });
    } finally {
      setSaving(false);
    }
  };

  if (!state) return null;

  return (
    <div className="card mb-4">
      <div className="flex items-center gap-2 mb-1">
        {state.enabled ? (
          <ShieldCheck size={18} className="text-emerald-600" />
        ) : (
          <Lock size={18} className="text-brand-600" />
        )}
        <h3 className="font-bold text-gray-700 dark:text-gray-300">{t("settings.tlsTitle")}</h3>
      </div>
      <p className="text-xs text-gray-400 mb-4">{t("settings.tlsHint")}</p>

      {state.enabled && (
        <div className="text-sm rounded-lg px-3 py-2 mb-4 text-emerald-700 bg-emerald-50">
          {t("settings.tlsActive", { domain: state.domain, port: state.fallback_http_port })}
        </div>
      )}

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div>
          <label className="block text-sm text-gray-600 mb-1">{t("settings.tlsDomain")}</label>
          <input
            className="input"
            dir="ltr"
            placeholder="panel.example.com"
            value={domain}
            onChange={(e) => setDomain(e.target.value)}
          />
          <div className="hint">{t("settings.tlsDomainHint")}</div>
        </div>
        <div>
          <label className="block text-sm text-gray-600 mb-1">{t("settings.tlsEmail")}</label>
          <input
            className="input"
            dir="ltr"
            type="email"
            placeholder="you@example.com"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
          <div className="hint">{t("settings.tlsEmailHint")}</div>
        </div>
      </div>

      {dns && (
        <div
          className={`text-sm rounded-lg px-3 py-2 mt-4 flex gap-2 ${
            dns.ok ? "text-emerald-700 bg-emerald-50" : "text-amber-700 bg-amber-50"
          }`}
        >
          {!dns.ok && <AlertTriangle size={16} className="shrink-0 mt-0.5" />}
          <span>{dns.reason}</span>
        </div>
      )}

      {msg && (
        <div
          className={`text-sm rounded-lg px-3 py-2 mt-4 whitespace-pre-line ${
            msg.type === "ok" ? "text-emerald-600 bg-emerald-50" : "text-red-500 bg-red-50"
          }`}
        >
          {msg.text}
        </div>
      )}

      <div className="flex flex-wrap gap-2 mt-4">
        <button type="button" className="btn-secondary" disabled={!domain || checking} onClick={runCheck}>
          {checking ? t("common.loading") : t("settings.tlsCheckDns")}
        </button>
        <button
          type="button"
          className="btn-primary"
          disabled={!domain || saving}
          onClick={() => submit(false)}
        >
          {saving ? t("settings.saving") : t("settings.tlsEnable")}
        </button>
        {/* Only offered once the check has actually disagreed - an override
            you can reach without first being told why you might need it is
            just a second button people press at random. */}
        {dns && !dns.ok && (
          <button type="button" className="btn-secondary" disabled={saving} onClick={() => submit(true)}>
            {t("settings.tlsForce")}
          </button>
        )}
        {state.enabled && (
          <button type="button" className="btn-secondary text-red-500" disabled={saving} onClick={turnOff}>
            {t("settings.tlsDisable")}
          </button>
        )}
      </div>
    </div>
  );
}
